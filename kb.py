#!/usr/bin/env python3
"""kb.py: the Kacific LLM KB manager CLI.

One stdlib-first tool. Generic and parameterised: it carries no site-specific identifiers. All deployment
specifics (repo paths, the tracking workspace, the PAT location) come from config.toml, never from this file.

Subcommands:
  store     validate a nugget against the schema and the anti-hallucination gate, then write it (or refuse)
  index     walk the KB data repos and rebuild the aggregate registry, then derive per-audience slices
  answer    answer a query using only stored nuggets, with grounding, citation, and confidence
  rot       hygiene sweep: flag Redundant / Outdated (verified > 30 days) / Trivial; emit a report
  sync      git-fetch each managed repo, diff SHA and per-nugget body hash, report drift (1b)
  prescan   one-time extraction of existing repos and KBs to seed the registry (secrets-safe)
  export    render a neutral bundle (Markdown/HTML) for an export target (1b)
  feedback  append a usage/rating/miss record; optionally raise a tracking task (1b)

Run one-off by hand, or schedule sync/rot/index on the NUC via the existing cron house pattern.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# tomllib is imported lazily inside load_config so the core commands (store, index, answer, rot) run on any
# Python 3. Only config-dependent commands need Python 3.11+ (stdlib tomllib) or the tomli backport.

SCHEMA_VERSION = 1
VALID_DOMAINS = {"it", "ot", "shared", "company"}
VALID_TYPES = {"how-to", "troubleshooting", "faq", "known-issue", "reference", "fact", "glossary"}
VALID_STATUS = {"draft", "published", "needs-update", "archived", "retired"}
ROT_OUTDATED_DAYS = 30
READER_STALE_DAYS = 90
TRIVIAL_BODY_CHARS = 40  # only near-empty stubs; a normal short nugget is legitimate, not trivial
MISS_RESPONSE = "I cannot find this information in the current knowledge base."

# A small stop list so query matching keys off meaningful terms, not filler. Deliberately tiny and stdlib.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were", "be", "with",
    "how", "what", "when", "where", "which", "who", "do", "does", "did", "can", "i", "we", "you", "it",
    "this", "that", "at", "by", "from", "as", "my", "our",
}


# --- config + manifest ------------------------------------------------------

def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            raise SystemExit(
                "reading TOML needs Python 3.11+ (stdlib tomllib) or the tomli backport; "
                "run kb.py with a newer interpreter, e.g. /opt/homebrew/bin/python3"
            )
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(path: str = "config.toml") -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    return _load_toml(p)


def load_manifest(config: dict) -> dict:
    """Resolve the managed-repo manifest named by config [repos].manifest.

    Returns {managed, audiences, cache_dir, manifest_path}. The clone cache defaults to
    <manifest dir>/cache/repos (gitignored in the control home) unless [repos].workspace overrides it.
    """
    repos_cfg = config.get("repos", {})
    manifest_path = repos_cfg.get("manifest")
    if not manifest_path:
        raise SystemExit("config is missing [repos].manifest (the path to repos.toml)")
    mp = Path(manifest_path).expanduser()
    if not mp.exists():
        raise SystemExit(f"manifest not found at [repos].manifest: {mp}")
    manifest = _load_toml(mp)
    workspace = repos_cfg.get("workspace")
    cache_dir = Path(workspace).expanduser() if workspace else mp.parent / "cache" / "repos"
    return {
        "managed": manifest.get("managed", {}),
        "audiences": manifest.get("audiences", {}),
        "cache_dir": cache_dir,
        "manifest_path": mp,
    }


# --- frontmatter ------------------------------------------------------------

_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a minimal YAML frontmatter block (flat keys, inline [a, b] lists) plus the body.

    Deliberately a tiny parser for the controlled schema so we stay stdlib-only. If the schema ever needs
    nested YAML, swap this for a real parser behind the same signature.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    meta: dict = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            meta[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        elif raw in {"null", "~", ""}:
            meta[key] = None
        else:
            meta[key] = raw.strip('"').strip("'")
    return meta, body


def _as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _iso_date(value) -> datetime | None:
    if not value or value == "unverified":
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --- validation: schema + anti-hallucination + voice ------------------------

def validate_entry(meta: dict, body: str) -> list[str]:
    errors: list[str] = []
    required = ["id", "title", "domain", "type", "status", "owner_gid", "provenance_type"]
    for field in required:
        if not meta.get(field):
            errors.append(f"missing required field: {field}")

    if meta.get("domain") and meta["domain"] not in VALID_DOMAINS:
        errors.append(f"invalid domain: {meta['domain']}")
    if meta.get("type") and meta["type"] not in VALID_TYPES:
        errors.append(f"invalid type: {meta['type']}")
    if meta.get("status") and meta["status"] not in VALID_STATUS:
        errors.append(f"invalid status: {meta['status']}")

    # The anti-hallucination gate: a reference that resolves, or a named attestation.
    prov = meta.get("provenance_type")
    if prov == "reference":
        if not meta.get("source"):
            errors.append("provenance_type reference requires a non-empty source")
    elif prov == "attestation":
        if not meta.get("attested_by") or not meta.get("attested_on"):
            errors.append("provenance_type attestation requires attested_by and attested_on")
    elif prov:
        errors.append(f"invalid provenance_type: {prov} (must be reference or attestation)")

    # Voice gate: no em-dashes anywhere in the human-readable content.
    if "—" in body or "—" in str(meta.get("title", "")):
        errors.append("em-dash found; use a comma, semicolon, parentheses, or full stop")

    return errors


# --- content hash (body only, excluding manager-managed fields) -------------

def body_hash(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


# --- git + clone cache (manifest-driven index/sync) -------------------------

def _git(args: list[str], cwd=None, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run git without ever raising the argv (which can carry a remote URL); callers check returncode.

    stdin is closed so a credential prompt fails fast rather than hanging a cron run, and the exception is
    scrubbed to a synthetic failed result so a host or token can never surface in a traceback.
    """
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=False, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=timeout,
        )
    except FileNotFoundError:
        raise SystemExit("git not found on PATH")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", args[0] if args else "git"], 124, "", "timed out")


def _refresh_clone(url: str, dest: Path) -> tuple[str | None, str]:
    """Clone-if-absent, fetch, and hard-reset the tool-owned cache to the remote default branch.

    Returns (head_sha, status). A non-'ok' status means the repo was unreachable; the caller records it and
    carries on with the other repos rather than aborting the whole run (source-health over fail-fast).
    """
    dest = Path(dest)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _git(["clone", "--quiet", url, str(dest)]).returncode != 0:
            return None, "unreachable (clone failed)"
    if _git(["fetch", "--prune", "--quiet", "origin"], cwd=str(dest)).returncode != 0:
        return None, "unreachable (fetch failed)"
    _git(["remote", "set-head", "origin", "-a"], cwd=str(dest))  # point origin/HEAD at the remote default
    if _git(["reset", "--hard", "--quiet", "origin/HEAD"], cwd=str(dest)).returncode != 0:
        return None, "checkout failed"
    head = _git(["rev-parse", "HEAD"], cwd=str(dest))
    if head.returncode != 0:
        return None, "rev-parse failed"
    return head.stdout.strip(), "ok"


# --- nugget loading (shared by index, answer, rot) --------------------------

def _load_nuggets(root: Path) -> list[dict]:
    """Walk a KB repo and return every nugget as {meta, body, path}. Index from content, never filename.

    A file is a nugget only if its frontmatter carries an id. README/registry mirrors, the house docs, and
    the sources/ binary store are skipped, so they never masquerade as nuggets.
    """
    nuggets: list[dict] = []
    for md in sorted(root.rglob("*.md")):
        if md.name in {"README.md", "registry.md", "AGENTS.md", "CLAUDE.md"} or "sources" in md.parts:
            continue
        meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
        if not meta.get("id"):
            continue
        nuggets.append({"meta": meta, "body": body, "path": md.relative_to(root)})
    return nuggets


def _registry_entry(n: dict) -> dict:
    m = n["meta"]
    return {
        "id": m["id"],
        "title": m.get("title", ""),
        "domain": m.get("domain"),
        "type": m.get("type"),
        "status": m.get("status"),
        "owner_gid": m.get("owner_gid"),
        "owner_name": m.get("owner_name"),
        "source_document": m.get("source"),
        "confidence_score": m.get("confidence"),
        "last_verified": m.get("verified"),
        "path": str(n["path"]),
        "content_hash": body_hash(n["body"]),
    }


def build_aggregate(manifest: dict) -> dict:
    """Walk every managed repo's refreshed clone and build the full cross-audience aggregate registry.

    Records each repo's resolved HEAD sha (the drift baseline sync diffs against) and tags every entry with
    its source_repo. Audience-scoped slicing (deriving each repo's published slice from this aggregate) is a
    separate later chunk; this is the private full index only.
    """
    cache_dir = manifest["cache_dir"]
    repos_out: dict = {}
    entries: list = []
    for key, spec in sorted(manifest["managed"].items()):
        url, audience = spec.get("url"), spec.get("audience")
        head, status = _refresh_clone(url, cache_dir / key)
        repos_out[key] = {"url": url, "audience": audience, "head_sha": head, "status": status}
        if status != "ok":
            continue
        for n in _load_nuggets(cache_dir / key):
            entry = _registry_entry(n)
            entry["source_repo"] = key
            entries.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": _now_iso(),
        "repos": repos_out,
        "entries": entries,
    }


# --- retrieval (grounding-only, keyword match over stored nuggets) ----------

def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", str(text).lower()) if len(t) > 1 and t not in _STOPWORDS]


def _nugget_terms(n: dict) -> dict:
    """Weighted term frequencies for a nugget: title counts most, then tags, then body."""
    m = n["meta"]
    terms: dict = {}
    weighted = ((3, m.get("title", "")), (2, " ".join(_as_list(m.get("tags")))), (1, n["body"]))
    for weight, field in weighted:
        for t in _tokens(field):
            terms[t] = terms.get(t, 0) + weight
    return terms


def _score(query_terms: list[str], n: dict) -> int:
    terms = _nugget_terms(n)
    return sum(terms.get(qt, 0) for qt in query_terms)


def _rank(query: str, nuggets: list[dict]) -> list[tuple[int, dict]]:
    q = _tokens(query)
    scored = [(_score(q, n), n) for n in nuggets]
    scored = [(s, n) for s, n in scored if s > 0]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


# --- interaction log (usage / ratings / misses; never a KB nugget) ----------

def _log_interaction(record: dict, log_path: str = "logs/interactions.jsonl") -> None:
    record = {"ts": _now_iso(), **record}
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --- subcommands ------------------------------------------------------------

def cmd_store(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    errors = validate_entry(meta, body)
    if errors:
        print("REFUSED. This nugget was not stored:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if args.into:
        dest_dir = Path(args.into) / (meta.get("domain") or "shared")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{meta['id']}.md"
        dest.write_text(text, encoding="utf-8")
        print(f"OK: stored nugget '{meta['id']}' at {dest} (passes schema + anti-hallucination gate).")
        print("Next: run `kb.py index` on the repo to refresh the registry.")
    else:
        print(f"OK: nugget '{meta['id']}' passes the schema and the anti-hallucination gate.")
        print("Validate-only (no --into given). Pass --into <repo> to write it into the KB.")
    return 0


def cmd_index(args) -> int:
    if getattr(args, "manifest", False):
        manifest = load_manifest(load_config(args.config))
        agg = build_aggregate(manifest)
        out_path = (
            Path(args.out).expanduser() if args.out
            else manifest["manifest_path"].parent / "registry-aggregate.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(agg, indent=2) + "\n", encoding="utf-8")
        ok = [k for k, r in agg["repos"].items() if r["status"] == "ok"]
        print(f"OK: aggregate written to {out_path} "
              f"({len(agg['entries'])} entries across {len(ok)}/{len(agg['repos'])} managed repos).")
        for key, r in sorted(agg["repos"].items()):
            if r["status"] != "ok":
                print(f"  WARN: repo '{key}': {r['status']}", file=sys.stderr)
        return 0

    if not args.repo:
        print("index: give a repo path, or --manifest to build the cross-repo aggregate.", file=sys.stderr)
        return 2
    root = Path(args.repo)
    entries = [_registry_entry(n) for n in _load_nuggets(root)]
    out = {"schema_version": SCHEMA_VERSION, "generated_utc": _now_iso(), "entries": entries}
    payload = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"OK: wrote {len(entries)} entries to {args.out}")
    else:
        print(payload)
    return 0


def cmd_answer(args) -> int:
    root = Path(args.repo)
    nuggets = _load_nuggets(root)
    ranked = _rank(args.query, nuggets)

    if not ranked:
        if not args.no_log:
            _log_interaction({"query": args.query, "hit": False, "kind": "gap"})
        if args.format == "json":
            print(json.dumps({"query": args.query, "found": False, "answer": MISS_RESPONSE}, indent=2))
        else:
            print(MISS_RESPONSE)
        return 0

    top_score = ranked[0][0]
    winners = [n for s, n in ranked if s == top_score]
    conflict = len(winners) > 1
    now = datetime.now(timezone.utc)

    if not args.no_log:
        _log_interaction({
            "query": args.query, "hit": True,
            "cited": [n["meta"]["id"] for n in winners], "conflict": conflict,
        })

    def _notes(n: dict) -> list[str]:
        notes = []
        m = n["meta"]
        verified = _iso_date(m.get("verified"))
        if verified is None or (now - verified).days > READER_STALE_DAYS:
            notes.append("Note: this document has not been audited recently.")
        if m.get("status") and m["status"] != "published":
            notes.append(f"Note: this nugget's status is '{m['status']}', not published.")
        return notes

    def _cite(n: dict) -> str:
        m = n["meta"]
        return m.get("source") or (f"attested by {m.get('attested_by')}" if m.get("attested_by") else m["id"])

    if args.format == "json":
        payload = {
            "query": args.query,
            "found": True,
            "conflict": conflict,
            "nuggets": [{
                "id": n["meta"]["id"],
                "title": n["meta"].get("title", ""),
                "answer": n["body"].strip(),
                "source_document": _cite(n),
                "confidence_score": n["meta"].get("confidence"),
                "last_updated": n["meta"].get("verified"),
                "status": n["meta"].get("status"),
                "notes": _notes(n),
            } for n in winners],
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Human format: lead with the answer, cite every nugget, surface conflicts, list sources at the end.
    if conflict:
        print("Conflicting nuggets match this query; a human editor should resolve which is authoritative.\n")
    for n in winners:
        m = n["meta"]
        print(n["body"].strip())
        print(f"\n[Source: {m['id']} | {_cite(n)}]")
        for note in _notes(n):
            print(note)
        print()
    print("Sources Verified:")
    for n in winners:
        m = n["meta"]
        print(f"  - {m['id']}: {m.get('title', '')} (verified {m.get('verified', 'unverified')})")
    return 0


def cmd_rot(args) -> int:
    root = Path(args.repo)
    nuggets = _load_nuggets(root)
    now = datetime.now(timezone.utc)

    by_id: dict = {}
    by_source: dict = {}
    superseded: set = set()
    for n in nuggets:
        m = n["meta"]
        by_id.setdefault(m["id"], []).append(n)
        if m.get("source"):
            by_source.setdefault(m["source"], []).append(n)
        for s in _as_list(m.get("supersedes")):
            superseded.add(s)

    flags: list[dict] = []
    for n in nuggets:
        m = n["meta"]
        reasons = []
        verified = _iso_date(m.get("verified"))
        if verified is None:
            reasons.append("Outdated (never verified)")
        elif (now - verified).days > ROT_OUTDATED_DAYS:
            reasons.append(f"Outdated (verified {(now - verified).days} days ago)")
        if len(by_id[m["id"]]) > 1:
            reasons.append("Redundant (duplicate id)")
        if m.get("source") and len(by_source[m["source"]]) > 1:
            reasons.append("Redundant (shares source with another nugget)")
        if m["id"] in superseded:
            reasons.append("Redundant (superseded by another nugget)")
        if m.get("status") in {"archived", "retired"}:
            reasons.append(f"Redundant (status {m['status']})")
        if len(n["body"].strip()) < TRIVIAL_BODY_CHARS:
            reasons.append("Trivial (near-empty body; owner confirms)")
        if reasons:
            flags.append({
                "id": m["id"], "path": str(n["path"]),
                "owner": m.get("owner_name") or m.get("owner_gid") or "(unassigned)",
                "reasons": reasons,
            })

    if not flags:
        print(f"rot: clean. {len(nuggets)} nuggets, none flagged.")
        return 0

    by_owner: dict = {}
    for f in flags:
        by_owner.setdefault(f["owner"], []).append(f)

    print(f"rot: {len(flags)} of {len(nuggets)} nuggets flagged, grouped by owner.\n")
    for owner in sorted(by_owner):
        print(f"owner: {owner}")
        for f in sorted(by_owner[owner], key=lambda x: x["id"]):
            print(f"  - {f['id']} ({f['path']}): {'; '.join(f['reasons'])}")
        print()
    # Phase 1b raises these as Asana tasks per the audit-family contract; 1a only reports.
    return 0


def _stub(name):
    def _run(args):
        print(f"{name}: not yet implemented (Phase 1b)", file=sys.stderr)
        return 0
    return _run


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kb.py", description="Kacific LLM KB manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("store", help="validate and store a nugget")
    sp.add_argument("file")
    sp.add_argument("--into", help="KB repo root to write the nugget into (by domain); omit to validate only")
    sp.set_defaults(func=cmd_store)

    sp = sub.add_parser("index", help="rebuild the registry from a repo, or --manifest for the aggregate")
    sp.add_argument("repo", nargs="?", help="single KB repo root to index; omit when using --manifest")
    sp.add_argument("--manifest", action="store_true",
                    help="build the cross-repo aggregate from config [repos].manifest into the control home")
    sp.add_argument("--config", default="config.toml", help="path to config.toml (used with --manifest)")
    sp.add_argument("--out",
                    help="output path; default stdout (single repo) or <manifest dir>/registry-aggregate.json")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("answer", help="answer a query from stored nuggets")
    sp.add_argument("query")
    sp.add_argument("--repo", default=".", help="KB repo root to search (default: current dir)")
    sp.add_argument("--format", choices=["human", "json"], default="human")
    sp.add_argument("--no-log", action="store_true", help="do not append to the interaction log")
    sp.set_defaults(func=cmd_answer)

    sp = sub.add_parser("rot", help="hygiene sweep")
    sp.add_argument("--repo", default=".", help="KB repo root to sweep (default: current dir)")
    sp.set_defaults(func=cmd_rot)

    for name in ("sync", "prescan", "export", "feedback"):
        sp = sub.add_parser(name, help=f"{name} (Phase 1b)")
        sp.set_defaults(func=_stub(name))

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
