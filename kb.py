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
  feedback  append a usage/rating/miss record, or report gap/ROT/conflict findings; --commit reconciles
            them into the tracking project as Asana tasks (create/no-op/verify-clear/reopen) (1b)
  cache     inspect or clear the local, gitignored TTL lookup cache (git-fetch reuse; Asana lookups later)

Run one-off by hand, or schedule sync/rot/index on the NUC via the existing cron house pattern.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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

    Returns {managed, audiences, cache_dir, cache_root, manifest_path}. The clone cache (cache_dir) defaults to
    <manifest dir>/cache/repos unless [repos].workspace overrides it. The lookup cache root (cache_root) holds
    the keyed TTL stores under cache_root/lookups and defaults to <manifest dir>/cache (the sibling parent of
    the default clone cache) unless [cache].dir overrides it. Both are gitignored in the control home.
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
    cache_cfg = config.get("cache", {})
    cache_root = Path(cache_cfg["dir"]).expanduser() if cache_cfg.get("dir") else mp.parent / "cache"
    return {
        "managed": manifest.get("managed", {}),
        "audiences": manifest.get("audiences", {}),
        "cache_dir": cache_dir,
        "cache_root": cache_root,
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


_FM_FIELD_ORDER = [
    "schema_version", "id", "title", "domain", "type", "status", "owner_gid", "owner_name",
    "provenance_type", "source", "attested_by", "attested_on", "confidence", "verified",
    "supersedes", "related", "tags",
]


def emit_frontmatter(meta: dict, body: str) -> str:
    """Serialise a flat meta dict plus body into the nugget file format parse_frontmatter reads back.

    The inverse of parse_frontmatter for the controlled schema: known fields in schema order, unknown
    fields after them sorted, inline [a, b] lists, None as null. Values are collapsed to one line and
    quoted when they would otherwise misparse (leading bracket or quote). Round-trip contract: parsing
    the output yields the same meta (scalars normalised to strings) and the same body.
    """
    def fmt(value) -> str:
        if value is None:
            return "null"
        if isinstance(value, list):
            return "[" + ", ".join(" ".join(str(v).replace(",", " ").split()) for v in value) + "]"
        s = " ".join(str(value).split())
        if not s:
            return "null"
        if s[0] in "[\"'" or s[-1] in "]\"'":
            s = '"' + s.strip('"').strip("'") + '"'
        return s

    keys = [k for k in _FM_FIELD_ORDER if k in meta]
    keys += sorted(k for k in meta if k not in _FM_FIELD_ORDER)
    lines = ["---"] + [f"{k}: {fmt(meta[k])}" for k in keys] + ["---", ""]
    return "\n".join(lines) + body


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


# --- lookup cache (keyed JSON with a TTL; gitignored, tool-owned) -----------
# A small, generic store for external lookups so a cron run does not re-fetch unchanged data every time. It
# backs the git-fetch reuse in `index` today, and is ready for the Asana user resolution (about 24h) later.
# A missing or corrupt cache is always a miss, never fatal: the tool re-fetches and re-stamps.

def _parse_iso_utc(value) -> datetime | None:
    """Parse the exact stamp _now_iso() writes ("%Y-%m-%dT%H:%M:%SZ"), tz-pinned UTC; None if unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _cache_file(cache_root, namespace: str) -> Path:
    return Path(cache_root) / "lookups" / f"{namespace}.json"


def _cache_load(cache_root, namespace: str) -> dict:
    p = _cache_file(cache_root, namespace)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}  # a bad cache is a miss, not a crash


def _cache_get(cache_root, namespace: str, key: str, ttl_seconds: int) -> tuple[bool, object]:
    """Return (hit, value). A hit means the record exists and its age is within ttl_seconds (0 -> always miss)."""
    if ttl_seconds <= 0:
        return False, None
    rec = _cache_load(cache_root, namespace).get(key)
    if not isinstance(rec, dict):
        return False, None
    stored = _parse_iso_utc(rec.get("stored_utc"))
    if stored is None:
        return False, None
    if (datetime.now(timezone.utc) - stored).total_seconds() > ttl_seconds:
        return False, None
    return True, rec.get("value")


def _cache_set(cache_root, namespace: str, key: str, value) -> None:
    p = _cache_file(cache_root, namespace)
    p.parent.mkdir(parents=True, exist_ok=True)
    store = _cache_load(cache_root, namespace)
    store[key] = {"value": value, "stored_utc": _now_iso()}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX: no torn cache if the run dies mid-write


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


def _refresh_clone(url: str, dest: Path, *, cache_root=None, ttl_seconds: int = 0,
                   force: bool = False) -> tuple[str | None, str]:
    """Clone-if-absent, fetch, and hard-reset the tool-owned cache to the remote default branch.

    Returns (head_sha, status). A non-'ok' status means the repo was unreachable; the caller records it and
    carries on with the other repos rather than aborting the whole run (source-health over fail-fast).

    With a cache_root and a positive ttl_seconds, an existing clone whose last successful fetch is within the
    window is reused without touching the network: the working copy's current HEAD is returned and the status
    stays exactly 'ok' (so callers gating on == 'ok' are unaffected; the skip shows only as an unchanged sha).
    `sync` passes force=True so drift detection always fetches. Every real fetch re-stamps the cache.
    """
    dest = Path(dest)
    key = dest.name
    if (dest / ".git").exists() and not force and cache_root is not None:
        hit, _ = _cache_get(cache_root, "git_fetch", key, ttl_seconds)
        if hit:
            head = _git(["rev-parse", "HEAD"], cwd=str(dest))
            if head.returncode == 0:  # reuse the working copy; a failed rev-parse falls through to a refresh
                return head.stdout.strip(), "ok"
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
    head_sha = head.stdout.strip()
    if cache_root is not None:  # stamp only a successful fetch, so an unreachable run retries next time
        _cache_set(cache_root, "git_fetch", key, head_sha)
    return head_sha, "ok"


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


def _registry_markdown(name: str, generated_utc: str, entries: list) -> str:
    """Render the human-readable registry.md mirror of a data repo's registry.json.

    Same shape as the mirrors seeded in the data repos: title, the do-not-edit paragraph, the generated
    stamp, then a table of id | title | domain | type | status | verified sorted by id ("No entries yet."
    when the repo is empty). The verified column carries the nugget's last_verified date.
    """
    lines = [
        f"# Registry: {name}",
        "",
        "Human-readable mirror of `registry.json`, the audience-scoped SSOT registry slice for this repo. The KB",
        "manager regenerates both (`kb.py index`). Do not edit by hand.",
        "",
        f"Generated (UTC): {generated_utc}",
        "",
    ]
    if not entries:
        lines.append("No entries yet.")
        return "\n".join(lines) + "\n"

    def cell(value) -> str:
        return str(value or "").replace("|", "\\|")

    lines.append("| id | title | domain | type | status | verified |")
    lines.append("|---|---|---|---|---|---|")
    for e in sorted(entries, key=lambda e: e["id"]):
        row = (e.get("id"), e.get("title"), e.get("domain"), e.get("type"),
               e.get("status"), e.get("last_verified"))
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def build_aggregate(manifest: dict, *, ttl_seconds: int = 0, force: bool = False) -> dict:
    """Walk every managed repo's refreshed clone and build the full cross-audience aggregate registry.

    Records each repo's resolved HEAD sha (the drift baseline sync diffs against) and tags every entry with
    its source_repo. Audience-scoped slicing (deriving each repo's published slice from this aggregate) is a
    separate later chunk; this is the private full index only. ttl_seconds/force control the git-fetch reuse
    cache: within ttl_seconds an unchanged repo is not re-fetched (0 -> always fetch; force -> always fetch).
    """
    cache_dir = manifest["cache_dir"]
    cache_root = manifest["cache_root"]
    repos_out: dict = {}
    entries: list = []
    for key, spec in sorted(manifest["managed"].items()):
        url, audience = spec.get("url"), spec.get("audience")
        head, status = _refresh_clone(url, cache_dir / key, cache_root=cache_root,
                                      ttl_seconds=ttl_seconds, force=force)
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
        config = load_config(args.config)
        manifest = load_manifest(config)
        ttl = args.max_age if args.max_age is not None else int(config.get("cache", {}).get("fetch_ttl_seconds", 0))
        agg = build_aggregate(manifest, ttl_seconds=ttl, force=args.force)
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
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        print(f"OK: wrote {len(entries)} entries to {args.out}")
        if out_path.suffix == ".json":
            # The data repos carry a human mirror next to the JSON registry; regenerate it in the same
            # pass so the two never drift. The audience label is the last hyphen-separated segment of
            # the repo directory name (Kacific-LLM-KB-Info-AllStaff -> AllStaff).
            name = root.resolve().name.rsplit("-", 1)[-1]
            mirror = out_path.with_name("registry.md")
            mirror.write_text(_registry_markdown(name, out["generated_utc"], entries), encoding="utf-8")
            print(f"OK: wrote human mirror to {mirror}")
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


def _rot_flags(nuggets: list[dict], now: datetime) -> list[dict]:
    """Compute the Redundant / Outdated / Trivial flags for a nugget set. The single source of the ROT rules.

    Both `rot` (which reports them, grouped by owner) and `feedback` (which turns them into audit-family
    findings) call this, so the flag rules live in exactly one place. Each flag is {id, path, owner, reasons}.
    """
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
                # owner_gid is carried separately (additive) so `feedback` can assign the Asana task to a real
                # person; `owner` stays the display string so `cmd_rot` grouping and its tests are unchanged.
                "owner_gid": m.get("owner_gid"),
                "reasons": reasons,
            })
    return flags


def cmd_rot(args) -> int:
    nuggets = _load_nuggets(Path(args.repo))
    flags = _rot_flags(nuggets, datetime.now(timezone.utc))

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
    # Phase 1b `feedback` raises these as Asana tasks per the audit-family contract; `rot` only reports.
    return 0


def _short(sha) -> str:
    return sha[:8] if sha else "none"


def cmd_sync(args) -> int:
    """Reconcile the managed repos from git against the recorded aggregate; report drift, never guess.

    Refreshes each managed clone, then diffs the live HEAD sha and per-nugget body hash against the recorded
    registry-aggregate.json: repos whose HEAD moved, nuggets added / removed / changed, out-of-band edits
    that fail the provenance gate, clones in the cache not in the manifest, and repos dropped from the
    manifest. Report-only (raising these as tracking tasks is the separate feedback step); exit 0.
    """
    manifest = load_manifest(load_config(args.config))
    agg_path = (
        Path(args.aggregate).expanduser() if args.aggregate
        else manifest["manifest_path"].parent / "registry-aggregate.json"
    )
    if not agg_path.exists():
        print(f"sync: no recorded baseline at {agg_path}. Run `kb.py index --manifest` first.")
        return 0
    recorded = json.loads(agg_path.read_text(encoding="utf-8"))
    rec_repos = recorded.get("repos", {})
    rec_hash = {(e.get("source_repo"), e["id"]): e.get("content_hash") for e in recorded.get("entries", [])}

    cache_dir = manifest["cache_dir"]
    cache_root = manifest["cache_root"]
    managed_keys = set(manifest["managed"])
    report: list[tuple[str, list[str]]] = []

    for key, spec in sorted(manifest["managed"].items()):
        lines: list[str] = []
        # Drift detection must be fresh: force a fetch (ignore the TTL) but still stamp the cache, so an
        # `index` shortly after this sync can reuse the fetch.
        head, status = _refresh_clone(spec.get("url"), cache_dir / key, cache_root=cache_root, force=True)
        if status != "ok":
            report.append((key, [f"UNREACHABLE: {status}"]))
            continue
        rec_head = (rec_repos.get(key) or {}).get("head_sha")
        if rec_head != head:
            lines.append(f"repo changed: recorded {_short(rec_head)} -> live {_short(head)}")
        live: dict = {}
        for n in _load_nuggets(cache_dir / key):
            nid = n["meta"].get("id")
            live[nid] = n
            errs = validate_entry(n["meta"], n["body"])
            if errs:
                lines.append(f"out-of-band invalid nugget '{nid}' ({n['path']}): {errs[0]}")
        live_ids = set(live)
        rec_ids = {i for (r, i) in rec_hash if r == key}
        for nid in sorted(live_ids - rec_ids):
            lines.append(f"added: {nid}")
        for nid in sorted(rec_ids - live_ids):
            lines.append(f"removed: {nid}")
        for nid in sorted(live_ids & rec_ids):
            if body_hash(live[nid]["body"]) != rec_hash[(key, nid)]:
                lines.append(f"changed: {nid}")
        report.append((key, lines))

    unmanaged = []
    if cache_dir.exists():
        for child in sorted(cache_dir.iterdir()):
            if child.is_dir() and (child / ".git").exists() and child.name not in managed_keys:
                unmanaged.append(child.name)
    dropped = sorted(set(rec_repos) - managed_keys)

    if not any(lines for _, lines in report) and not unmanaged and not dropped:
        print(f"sync: clean. {len(report)} managed repos, no drift.")
        return 0

    print(f"sync: drift detected across {len(report)} managed repos.\n")
    for key, lines in report:
        if lines:
            print(f"repo: {key}")
            for line in lines:
                print(f"  - {line}")
            print()
    if unmanaged:
        print("unmanaged (a clone in the cache, not in the manifest, needs triage):")
        for name in unmanaged:
            print(f"  - {name}")
        print()
    if dropped:
        print("dropped (recorded in the aggregate but no longer in the manifest):")
        for name in dropped:
            print(f"  - {name}")
        print()
    return 0


def cmd_cache(args) -> int:
    """Inspect or clear the local, gitignored TTL lookup cache. Report-only unless --clear; exit 0.

    Clearing removes only the tool-owned regenerable lookup stores under <cache_root>/lookups; it never
    touches the clone cache, the manifest, or config.
    """
    manifest = load_manifest(load_config(args.config))
    lookups = Path(manifest["cache_root"]) / "lookups"

    if args.clear:
        if not lookups.exists():
            print(f"cache: nothing to clear at {lookups}.")
            return 0
        targets = ([lookups / f"{args.namespace}.json"] if args.namespace
                   else sorted(lookups.glob("*.json")))
        removed = 0
        for f in targets:
            if f.exists():
                f.unlink()
                removed += 1
        scope = f"namespace '{args.namespace}'" if args.namespace else "all namespaces"
        print(f"cache: cleared {scope} ({removed} store(s) removed) under {lookups}.")
        return 0

    if not lookups.exists() or not any(lookups.glob("*.json")):
        print(f"cache: empty. No lookup stores under {lookups}.")
        return 0
    print(f"cache: lookup stores under {lookups}")
    for f in sorted(lookups.glob("*.json")):
        print(f"  {f.stem}: {len(_cache_load(manifest['cache_root'], f.stem))} entries")
    return 0


# --- feedback: logged + swept signals -> audit-family findings --------------
# Owned finding-id prefixes (this tool's, per the audit-family [<id>] ownership rule; never another tool's).
_GAP_ID = "KB-GAP"
_CONFLICT_ID = "KB-CONFLICT"
_ROT_IDS = {"Outdated": "KB-ROT-OUTDATED", "Redundant": "KB-ROT-REDUNDANT", "Trivial": "KB-ROT-TRIVIAL"}
_TRIAGE = "(triage)"

# The finding-ids this tool OWNS. In the shared AI Tracking project this set is the whole mutual-exclusion
# mechanism (kacific-audit-governance): the tool only ever reads, completes, or reopens tasks whose title
# carries one of these tags, so it never touches a [Build]/[Chip] task or a sibling audit's [<id>] task.
# Additive-only: adding a new KB finding-id here is safe; renaming or reusing a sibling's prefix is not.
_OWNED_IDS = (_GAP_ID, _CONFLICT_ID, *_ROT_IDS.values())
# Anchored, with the tag alternation holding no ']', so `\] ` always closes the tag bracket and group(2) is
# the exact subject even when the subject itself contains '] ', '|', or spaces.
_OWNED_RE = re.compile(r"^\[(" + "|".join(re.escape(i) for i in _OWNED_IDS) + r")\] (.+)$")
_ASANA_NAME_MAX = 1000  # Asana caps task names near 1024; stay under so a title is never silently truncated.


def _finding_subject(finding: dict) -> str:
    """The stable key with its leading '<finding_id>:' stripped, i.e. the human-facing tail of the title."""
    key, prefix = finding["key"], finding["finding_id"] + ":"
    return key[len(prefix):] if key.startswith(prefix) else key


def _finding_title(finding: dict) -> str:
    """`[<id>] <subject>`. Title equality IS key equality (exactly one title per finding), so the reconcile
    keys on the title and never reconstructs the key. A subject long enough to risk Asana truncating the name
    (which would silently break dedup and re-create the task every run) is shortened deterministically, with a
    short hash of the full subject appended so two long subjects never collapse to the same title; the full
    text still goes in the task body."""
    fid, subject = finding["finding_id"], _finding_subject(finding)
    title = f"[{fid}] {subject}"
    if len(title) > _ASANA_NAME_MAX:
        digest = hashlib.sha1(subject.encode("utf-8")).hexdigest()[:8]
        keep = max(1, _ASANA_NAME_MAX - len(fid) - 20)  # room for "[id] ", the "… #", and the 8-char digest
        title = f"[{fid}] {subject[:keep]}… #{digest}"
    return title


def _finding_id_from_title(name: str) -> str | None:
    """The owned finding-id a task title carries, or None when the title is not one of ours (a [Build]/[Chip]
    task, or a sibling audit's [<id>]). This is what keeps the tool on its own tasks in the shared project."""
    m = _OWNED_RE.match(name or "")
    return m.group(1) if m else None


def _active_ids(log_collected: bool, rot_collected: bool) -> set:
    """The finding-id families whose surface was actually collected this run (the active-family gate). The
    verify-clear/reopen pass may only act on a task whose id is in this set; a family whose surface was NOT
    collected is left exactly as-is, never auto-cleared. An uncollected surface simply does not appear here,
    so the safe default (touch nothing) falls out for free."""
    active: set = set()
    if log_collected:
        active |= {_GAP_ID, _CONFLICT_ID}
    if rot_collected:
        active |= set(_ROT_IDS.values())
    return active


def _normalise_query(q) -> str:
    """Lowercase + collapse whitespace, so the same miss upserts one finding, not many. Stays readable."""
    return " ".join(str(q).lower().split())


def _read_interaction_log(log_path: Path) -> tuple[list[dict], bool]:
    """Return (records, collected). collected is False when the surface could not be read.

    A missing or unreadable log is 'not collected' (the active-family gate), never 'collected, empty': a
    future clear-leg must not treat absence of a log as proof that every gap task is resolved. An existing but
    empty log IS collected (zero records). A single malformed line is skipped, not fatal.
    """
    if not log_path.exists():
        return [], False
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return [], False
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records, True


def _gap_conflict_findings(records: list[dict]) -> list[dict]:
    """Aggregate the interaction log into gap and conflict findings with stable, deterministic keys."""
    gaps: dict = {}       # normalised query -> count
    conflicts: dict = {}  # sorted-id key -> {count, query, ids}
    for r in records:
        if r.get("kind") == "gap" or r.get("hit") is False:
            nq = _normalise_query(r.get("query", ""))
            if nq:
                gaps[nq] = gaps.get(nq, 0) + 1
        if r.get("conflict") is True:
            ids = sorted(str(i) for i in _as_list(r.get("cited")))
            if len(ids) > 1:
                slot = conflicts.setdefault("|".join(ids), {"count": 0, "query": r.get("query", ""), "ids": ids})
                slot["count"] += 1

    findings: list[dict] = []
    for nq, count in sorted(gaps.items()):
        findings.append({
            "finding_id": _GAP_ID, "key": f"{_GAP_ID}:{nq}", "entity": _TRIAGE,
            "detail": f"{count} miss(es) logged, no matching nugget",
        })
    for joined, slot in sorted(conflicts.items()):
        findings.append({
            "finding_id": _CONFLICT_ID, "key": f"{_CONFLICT_ID}:{joined}", "entity": _TRIAGE,
            "detail": f"{slot['count']} logged tie(s) for query '{_normalise_query(slot['query'])}'; "
                      f"cited {', '.join(slot['ids'])}",
        })
    return findings


def _rot_findings(flags: list[dict]) -> list[dict]:
    """Turn ROT flags into findings, one per (nugget, reason-category), deduped by stable key."""
    by_key: dict = {}
    for f in flags:
        for reason in f["reasons"]:
            fid = _ROT_IDS.get(reason.split()[0])  # "Outdated"/"Redundant"/"Trivial"
            if not fid:
                continue
            key = f"{fid}:{f['id']}"
            slot = by_key.setdefault(
                key, {"finding_id": fid, "entity": f["owner"], "owner_gid": f.get("owner_gid"), "details": []})
            slot["details"].append(reason)
    return [
        {"finding_id": s["finding_id"], "key": k, "entity": s["entity"], "owner_gid": s["owner_gid"],
         "detail": "; ".join(s["details"])}
        for k, s in sorted(by_key.items())
    ]


# --- Asana reconcile leg (feedback --commit) --------------------------------
#
# This is the KB manager's member of the Kacific audit family (kacific-audit-governance): read-only discovery
# (done above by the finding set), file to Asana, and let re-discovery be the judge of "done". It files into a
# section of the SHARED AI Tracking project, so `_OWNED_RE` isolation and section-scoped reads keep it off the
# [Build]/[Chip] tasks. The verify-clear/reopen pass is guarded by the active-family gate (`_active_ids`).
#
# DEFERRED, consciously (not silently dropped): the regression guard (a per-id `check_version` + a history
# JSON) from the contract. Re-discovery already reopens a human-closed but still-present finding; the guard
# only covers the narrow "a loosened check closed a task, later tightened" window, and a half-used state file
# is a premature forward-compat surface. The finding-id constants stay additive-ready for it. Multi-destination
# routing is also deferred (single destination here; the default-destination shape leaves room to add it).


def _retry_after_seconds(header_value, attempt: int) -> float:
    """Honour Asana's Retry-After (integer seconds) on a 429; fall back to a bounded exponential backoff."""
    try:
        return max(1.0, float(header_value))
    except (TypeError, ValueError):
        return float(min(2 ** attempt, 30))


def _asana_error_message(body: str) -> str:
    """Pull Asana's errors[].message out of an error body for a readable failure, without dumping the whole
    payload. Never contains the PAT (it rides in a request header, not the body)."""
    try:
        errs = json.loads(body).get("errors", [])
        return "; ".join(e.get("message", "") for e in errs if e.get("message")) or (body or "")[:200]
    except (ValueError, AttributeError):
        return (body or "")[:200]


def _resolve_tracking_pat(config: dict) -> str:
    """Resolve the tracking PAT VALUE from its configured LOCATION. First non-empty wins:
      1. [tracking.pat].token                inline value (dev; only ever in the private gitignored config)
      2. [tracking.pat].secret_file          a root-owned file path (the NUC prod location; read + stripped)
      3. [tracking.pat].macos_keychain_entry a Keychain entry name, read via `security ... -w`
    Never logs or returns the value to a printing caller; raises SystemExit with a LOCATION-only message (never
    the value) when none resolve. The Keychain subprocess argv holds only the entry name, and any subprocess
    error is scrubbed to a fresh message so no argv/trace leaks (feedback_scrub_subprocess_exceptions)."""
    pat_cfg = (config.get("tracking", {}) or {}).get("pat", {}) or {}

    token = str(pat_cfg.get("token") or "").strip()
    if token:
        return token

    secret_file = str(pat_cfg.get("secret_file") or "").strip()
    if secret_file:
        p = Path(secret_file).expanduser()
        if p.exists():
            value = p.read_text(encoding="utf-8").strip()
            if value:
                return value

    entry = str(pat_cfg.get("macos_keychain_entry") or "").strip()
    if entry:
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", entry, "-w"],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise SystemExit(f"could not read the tracking PAT from Keychain entry '{entry}'")
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()

    raise SystemExit(
        "no tracking PAT resolved: set [tracking.pat].token (dev), .secret_file (NUC prod), or "
        ".macos_keychain_entry in config.toml. The value is never stored in this repo, only its location."
    )


class _AsanaError(Exception):
    """An Asana REST call that failed after retries. A normal Exception (not SystemExit) so the reconcile can
    catch it per-task and carry on (a stale assignee, an unreachable annotation) instead of aborting the run."""


class _AsanaClient:
    """Minimal stdlib-urllib client for the Asana REST API, so the NUC needs no third-party package. The PAT
    rides in the Authorization header ONLY (never the URL/query, per the privacy rule) and is never logged.
    urllib raises HTTPError on every non-2xx, so `_request` reads the error body for Asana's message and backs
    off on 429/5xx."""

    _BASE = "https://app.asana.com/api/1.0"

    def __init__(self, pat: str, *, base: str | None = None, max_retries: int = 6):
        self._pat = pat
        self._base = base or self._BASE
        self._max_retries = max_retries

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None):
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps({"data": body}).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._pat}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")
                except Exception:  # noqa: BLE001 - a body we cannot read is not worth failing over
                    pass
                if exc.code == 429 and attempt < self._max_retries:
                    time.sleep(_retry_after_seconds(exc.headers.get("Retry-After"), attempt))
                    attempt += 1
                    continue
                if 500 <= exc.code < 600 and attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    attempt += 1
                    continue
                raise _AsanaError(f"Asana {method} {path} -> HTTP {exc.code}: {_asana_error_message(detail)}")
            except urllib.error.URLError as exc:
                if attempt < self._max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    attempt += 1
                    continue
                raise _AsanaError(f"Asana {method} {path} -> network error: {exc.reason}")

    def get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict):
        return self._request("POST", path, body=body)

    def put(self, path: str, body: dict):
        return self._request("PUT", path, body=body)

    def get_all(self, path: str, params: dict | None = None) -> list:
        """Follow Asana offset pagination, returning the concatenated data list."""
        out: list = []
        params = dict(params or {})
        params.setdefault("limit", 100)
        while True:
            page = self._request("GET", path, params=params)
            out.extend(page.get("data", []))
            offset = (page.get("next_page") or {}).get("offset")
            if not offset:
                return out
            params["offset"] = offset


def _ensure_kb_section(client, project_gid: str, section_name: str, commit: bool) -> str | None:
    """The gid of the KB findings section within the project, find-or-create by name. Dry-run looks up only and
    returns None when the section does not exist yet (so a dry-run makes zero writes)."""
    for s in client.get_all(f"/projects/{project_gid}/sections", {"opt_fields": "name"}):
        if s.get("name") == section_name:
            return s.get("gid")
    if not commit:
        return None
    created = client.post(f"/projects/{project_gid}/sections", {"name": section_name})
    return created.get("data", {}).get("gid")


def _ensure_verification_field(client, workspace_gid: str, project_gid: str, field_name: str,
                               commit: bool) -> dict | None:
    """Find the workspace-scoped enum field `field_name` and ensure it is attached to the project. Returns
    {"field_gid", "options": {option_name: option_gid}} or None when the field is absent/unusable (callers then
    degrade to a comment). KB is NOT the owning writer of this shared field (kacific-audit-governance), so it
    never CREATES the field and never adds enum options; it uses the options the owning audits already defined,
    and skips any state whose option is absent."""
    if not workspace_gid:
        return None
    field = None
    for f in client.get_all(f"/workspaces/{workspace_gid}/custom_fields",
                            {"opt_fields": "name,resource_subtype,enum_options.name"}):
        if f.get("name") == field_name and f.get("resource_subtype") == "enum":
            field = f
            break
    if not field:
        return None
    field_gid = field.get("gid")
    options = {o.get("name"): o.get("gid") for o in field.get("enum_options", []) if o.get("name")}
    settings = client.get_all(f"/projects/{project_gid}/custom_field_settings",
                              {"opt_fields": "custom_field.gid"})
    attached = any((s.get("custom_field") or {}).get("gid") == field_gid for s in settings)
    if not attached:
        if not commit:
            return {"field_gid": field_gid, "options": options}
        try:
            client.post(f"/projects/{project_gid}/addCustomFieldSetting",
                        {"custom_field": field_gid, "is_important": False})
        except _AsanaError:
            return None  # cannot attach (not a member/permission) -> degrade to comments, never abort
    return {"field_gid": field_gid, "options": options}


def _existing_kb_tasks(client, section_gid: str) -> dict:
    """Owned tasks currently in the KB section, {title: {gid, completed}}. Section-scoped and OWNED_RE-filtered,
    so [Build]/[Chip] tasks (in the project's default section) are never even seen. No `completed_since` is
    passed, so completed tasks ARE returned and reopen can fire on a human-closed KB task."""
    out: dict = {}
    for t in client.get_all(f"/sections/{section_gid}/tasks", {"opt_fields": "name,completed"}):
        name = t.get("name", "")
        if _finding_id_from_title(name):
            out[name] = {"gid": t.get("gid"), "completed": bool(t.get("completed"))}
    return out


def _apply_verification(client, task_gid: str, state: str, vfield: dict | None, commit: bool) -> None:
    """Record a verification state on a task: set the shared enum option when the field is attached and has that
    option, else degrade to a comment story. Commit-gated; a failure here never sinks the run, the task's
    completed/open state is already correct."""
    if not commit or not task_gid:
        return
    try:
        if vfield and state in (vfield.get("options") or {}):
            client.put(f"/tasks/{task_gid}", {"custom_fields": {vfield["field_gid"]: vfield["options"][state]}})
        else:
            client.post(f"/tasks/{task_gid}/stories", {"text": f"KB verification: {state}"})
    except _AsanaError:
        pass


def _create_task(client, cfg: dict, section_gid: str | None, finding: dict, vfield: dict | None,
                 commit: bool, counts: dict) -> None:
    """Create one findings task titled `[id] subject`, add it to the KB section, assign it, mark it unverified.
    Per-task resilience: a stale owner_gid (no longer a workspace member) 400s the create, so retry once
    unassigned rather than lose the finding."""
    if not commit:
        counts["created"] += 1
        return
    tracking = cfg.get("tracking", {}) or {}
    project_gid = str(tracking.get("project_gid") or "").strip()
    default_assignee = str(tracking.get("default_assignee") or "").strip()
    owner_gid = finding.get("owner_gid")
    assignee = str(owner_gid).strip() if owner_gid else default_assignee
    body = {"name": _finding_title(finding), "notes": finding.get("detail", ""), "projects": [project_gid]}
    if assignee:
        body["assignee"] = assignee
    try:
        created = client.post("/tasks", body)
    except _AsanaError:
        if not assignee:
            counts["failed"] += 1
            return
        body.pop("assignee", None)
        try:
            created = client.post("/tasks", body)
        except _AsanaError:
            counts["failed"] += 1
            return
    task_gid = created.get("data", {}).get("gid")
    if section_gid and task_gid:
        try:
            client.post(f"/sections/{section_gid}/addTask", {"task": task_gid})
        except _AsanaError:
            pass  # landed in the project's default section; not fatal
    _apply_verification(client, task_gid, "unverified", vfield, commit)
    counts["created"] += 1


def _reorder_section_by_entity(client, section_gid: str, findings: list, counts: dict) -> None:
    """Leave the KB section grouped by entity then key (the same order the dry-run prints). No-op when already
    ordered. The section is KB-only, so this never disturbs [Build]/[Chip] ordering; it moves only owned open
    tasks that are still desired."""
    ordered = sorted(findings, key=lambda f: (str(f["entity"]), f["key"]))
    desired_titles = [_finding_title(f) for f in ordered]
    tasks = client.get_all(f"/sections/{section_gid}/tasks", {"opt_fields": "name,completed"})
    by_title = {t.get("name"): t.get("gid") for t in tasks
                if not t.get("completed") and _finding_id_from_title(t.get("name", ""))}
    current = [t.get("name") for t in tasks
               if not t.get("completed") and _finding_id_from_title(t.get("name", ""))]
    target = [tt for tt in desired_titles if tt in by_title]
    if current == target:
        return
    prev_gid = None
    for tt in target:
        gid = by_title[tt]
        if prev_gid is not None:
            client.post(f"/sections/{section_gid}/addTask", {"task": gid, "insert_after": prev_gid})
        prev_gid = gid
    counts["reordered"] += 1


def _reconcile(client, cfg: dict, findings: list, active_ids: set, commit: bool) -> dict:
    """The audit-family reconcile: create / no-op / verify-clear / reopen the tool's own `[KB-*]` tasks in the
    KB section, gated by the active-family set. Re-discovery is the judge of "done": a finding still present
    keeps (or reopens) its task; a finding absent from the freshly-computed set (its family collected)
    verify-clears its task; a family NOT collected this run is left untouched."""
    tracking = cfg.get("tracking", {}) or {}
    project_gid = str(tracking.get("project_gid") or "").strip()
    workspace_gid = str(tracking.get("workspace_gid") or "").strip()
    section_name = str(tracking.get("section_name") or "KB Findings").strip()
    field_name = str(tracking.get("verification_field") or "Verification").strip()
    if not project_gid:
        raise SystemExit("reconcile needs [tracking].project_gid in config.toml")

    counts = {"created": 0, "noop": 0, "reopened": 0, "verify_cleared": 0,
              "skipped_inactive": 0, "reordered": 0, "failed": 0}

    section_gid = _ensure_kb_section(client, project_gid, section_name, commit)
    vfield = _ensure_verification_field(client, workspace_gid, project_gid, field_name, commit)
    existing = _existing_kb_tasks(client, section_gid) if section_gid else {}
    desired = {_finding_title(f): f for f in findings}

    # Pass A: create the missing, reopen a human-closed but still-present finding, no-op an already-open one.
    for title, finding in desired.items():
        task = existing.get(title)
        if task is None:
            _create_task(client, cfg, section_gid, finding, vfield, commit, counts)
        elif task["completed"]:
            # Its family is active by construction (we only computed the finding because we collected its
            # surface), but gate defensively anyway.
            if finding["finding_id"] not in active_ids:
                counts["skipped_inactive"] += 1
                continue
            if commit:
                try:
                    client.put(f"/tasks/{task['gid']}", {"completed": False})
                except _AsanaError:
                    counts["failed"] += 1
                    continue
                _apply_verification(client, task["gid"], "verification-failed", vfield, commit)
            counts["reopened"] += 1
        else:
            counts["noop"] += 1

    # Pass B: verify-clear an owned task whose condition is gone, but only for a family that was collected.
    for name, task in existing.items():
        if name in desired:
            continue
        if _finding_id_from_title(name) not in active_ids:
            counts["skipped_inactive"] += 1  # uncollected family -> leave exactly as-is, never auto-clear
            continue
        if task["completed"]:
            continue  # already clear
        if commit:
            try:
                client.put(f"/tasks/{task['gid']}", {"completed": True})
            except _AsanaError:
                counts["failed"] += 1
                continue
            _apply_verification(client, task["gid"], "verified-clear", vfield, commit)
        counts["verify_cleared"] += 1

    if commit and section_gid:
        _reorder_section_by_entity(client, section_gid, findings, counts)
    return counts


def cmd_feedback(args) -> int:
    """Append a signal record, or report logged + swept signals as an audit-family raise plan (Asana deferred).

    `--log` appends one usage/rating/miss record to the interaction log. The default collects the gap +
    conflict signals from that log and the ROT flags from an optional repo sweep, computes the finding set
    (owned KB-* ids, stable keys, grouped by entity), and prints the raise plan. Without `--commit` that is
    all it does (dry-run, fully offline, no PAT). With `--commit` it then reconciles the plan into the KB
    Findings section of the tracking project: create / no-op / verify-clear / reopen the tool's own `[KB-*]`
    tasks, gated by the active-family set, per the kacific-audit-governance contract.
    """
    log_path = Path(args.log_file)

    if args.log:
        if not args.query:
            print("feedback --log: --query is required.", file=sys.stderr)
            return 2
        kind = args.kind or "gap"
        record: dict = {"query": args.query, "kind": kind}
        if kind in {"gap", "miss"}:  # a miss is a gap signal for the aggregator
            record["kind"] = "gap"
            record["hit"] = False
        if args.rating:
            record["rating"] = args.rating
        if args.nugget:
            record["nugget"] = args.nugget
        _log_interaction(record, str(log_path))
        print(f"feedback: appended a '{record['kind']}' record for '{args.query}' to {log_path}.")
        return 0

    notes: list[str] = []
    findings: list[dict] = []

    records, log_collected = _read_interaction_log(log_path)
    if log_collected:
        findings.extend(_gap_conflict_findings(records))
    else:
        notes.append(f"gap+conflict family skipped (log not collected: {log_path})")

    rot_collected = False
    if not args.repo:
        notes.append("ROT family skipped (no --repo swept)")
    else:
        try:
            nuggets = _load_nuggets(Path(args.repo))
        except OSError:
            nuggets = []
        if nuggets:
            rot_collected = True
            findings.extend(_rot_findings(_rot_flags(nuggets, datetime.now(timezone.utc))))
        else:
            # A mis-pointed or empty --repo reads identically to "collected, found nothing", which would let
            # the reconcile auto-clear every ROT task. Treat zero nuggets as NOT collected (the active-family
            # gate): prefer a missed clear (a stale task the next good run closes) over a false clear.
            notes.append(f"ROT family skipped (no nuggets read at {args.repo}; treated as not collected)")

    # Report the plan grouped by entity, whether or not we then commit (owner for ROT; (triage) for
    # gaps + conflicts). No-op when already ordered.
    if findings:
        by_entity: dict = {}
        for f in findings:
            by_entity.setdefault(f["entity"], []).append(f)
        n_ent = len(by_entity)
        mode = "committing to Asana" if args.commit else \
            "dry-run; add --commit and a resolvable tracking PAT in config to raise these"
        print(f"feedback: {len(findings)} finding(s) across {n_ent} "
              f"{'entity' if n_ent == 1 else 'entities'} ({mode}).\n")
        for entity in sorted(by_entity):
            print(f"entity: {entity}")
            for f in sorted(by_entity[entity], key=lambda x: x["key"]):
                print(f"  - [{f['finding_id']}] {f['key']}")
                print(f"      {f['detail']}")
            print()
        if notes:
            print("notes (a surface not collected is left as-is, never auto-cleared):")
            for n in notes:
                print(f"  - {n}")
            print()
    else:
        line = "feedback: no findings."
        if notes:
            line += " " + " ".join(f"[{n}]" for n in notes)
        print(line)

    if not args.commit:
        return 0

    # Live reconcile. Even with zero findings this must run, so a condition that has cleared (its family
    # collected) gets its task verify-cleared. The PAT is resolved here, never on the dry-run path.
    config = load_config(args.config)
    pat = _resolve_tracking_pat(config)
    client = _AsanaClient(pat)
    active_ids = _active_ids(log_collected, rot_collected)
    try:
        counts = _reconcile(client, config, findings, active_ids, True)
    except _AsanaError as exc:
        print(f"feedback: Asana reconcile failed: {exc}", file=sys.stderr)
        return 1
    print("reconcile: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
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
    sp.add_argument("--max-age", type=int, default=None, metavar="SECONDS",
                    help="reuse a managed repo's clone without re-fetching if its last fetch is within this "
                         "window (default: config [cache].fetch_ttl_seconds, else 0 = always fetch)")
    sp.add_argument("--force", action="store_true", help="ignore the fetch cache and re-fetch every repo")
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

    sp = sub.add_parser("sync", help="drift-detect managed repos against the recorded aggregate")
    sp.add_argument("--config", default="config.toml", help="path to config.toml")
    sp.add_argument("--aggregate",
                    help="path to registry-aggregate.json (default: <manifest dir>/registry-aggregate.json)")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("cache", help="inspect or clear the local TTL lookup cache")
    sp.add_argument("--config", default="config.toml", help="path to config.toml")
    sp.add_argument("--clear", action="store_true", help="remove cached lookup stores (regenerated on demand)")
    sp.add_argument("--namespace", help="limit --clear to one namespace (e.g. git_fetch)")
    sp.set_defaults(func=cmd_cache)

    sp = sub.add_parser("feedback",
                        help="append a usage/rating/miss record, or report gap/ROT/conflict findings to raise")
    sp.add_argument("--repo", help="KB repo root to sweep for ROT findings (omit to skip the ROT family)")
    sp.add_argument("--log-file", default="logs/interactions.jsonl",
                    help="interaction log to read gap/conflict signals from (default: logs/interactions.jsonl)")
    sp.add_argument("--log", action="store_true",
                    help="append one usage/rating/miss record instead of reporting (needs --query)")
    sp.add_argument("--kind", choices=["gap", "miss", "rating"], help="record kind for --log (default: gap)")
    sp.add_argument("--query", help="the query text for --log")
    sp.add_argument("--nugget", help="nugget id the --log record refers to (optional)")
    sp.add_argument("--rating", choices=["helpful", "unhelpful"], help="rating for a --log --kind rating record")
    sp.add_argument("--commit", action="store_true",
                    help="reconcile the finding set into Asana (create/no-op/verify-clear/reopen); "
                         "default is an offline dry-run that only prints the plan")
    sp.add_argument("--config", default="config.toml", help="path to config.toml (used with --commit)")
    sp.set_defaults(func=cmd_feedback)

    for name in ("prescan", "export"):
        sp = sub.add_parser(name, help=f"{name} (Phase 1b)")
        sp.set_defaults(func=_stub(name))

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
