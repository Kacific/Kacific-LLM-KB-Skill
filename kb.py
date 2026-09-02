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
  prescan   one-time seed: scan the manifest's seed-source repos into ranked pointer candidates plus a
            captured-vs-gap report; --commit stages them for human review (secrets-safe by name)
  export    render a neutral, ACL-aware bundle (docs + bundle.json) for an export target (SharePoint /
            Confluence / Box); --manifest produces one leak-safe bundle per audience slice
  feedback  append a usage/rating/miss record, or report gap/ROT/conflict findings; --commit reconciles
            them into the tracking project as Asana tasks (create/no-op/verify-clear/reopen) (1b)
  cache     inspect or clear the local, gitignored TTL lookup cache (git-fetch reuse; Asana lookups later)

Run one-off by hand, or schedule sync/rot/index on the NUC via the existing cron house pattern.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
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
VALID_DOMAINS = {"technical", "commercial", "admin", "finance", "hr", "shared", "company"}
VALID_TYPES = {"how-to", "troubleshooting", "faq", "known-issue", "reference", "fact", "glossary"}
VALID_STATUS = {"draft", "published", "needs-update", "archived", "retired"}
ROT_OUTDATED_DAYS = 30
READER_STALE_DAYS = 90
# A nugget cited by a real query within USAGE_WINDOW_DAYS is "in active use", so its ROT-OUTDATED flag is
# held back: chasing a re-verification nobody is waiting on is what turned a batch of seeded nuggets into 108
# same-day findings. The hold is not unconditional though: past ROT_HARD_CEILING_DAYS the nugget is flagged
# regardless of use, so a popular-but-wrong fact still resurfaces for a human re-check. `verified` itself is
# never touched by use; usage only extends the window, it is not a substitute for human verification.
USAGE_WINDOW_DAYS = 90
ROT_HARD_CEILING_DAYS = 180
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

    Returns {managed, audiences, seed_sources, cache_dir, cache_root, manifest_path}. The clone cache (cache_dir) defaults to
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
        "seed_sources": manifest.get("seed_sources", {}),
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
    "provenance_type", "source", "attested_by", "attested_by_name", "attested_on", "confidence",
    "verified", "verified_by", "verified_by_name",
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


def _person(gid, name) -> str:
    """Render an identity for a human: `Name (gid)` where both exist, else whichever there is.

    One helper rather than a copy per surface. A gid identifies the person to the estate and a name is
    what a reader can act on, and the two display sites for an attestation (the answer citation and the
    exported doc's provenance line) were each printing a bare 16-digit number at the reader.
    """
    gid = "" if gid is None else str(gid).strip()
    name = "" if name is None else str(name).strip()
    if gid and name:
        return f"{name} ({gid})"
    return name or gid


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

    # `verified` records WHEN, `verified_by` records WHO. Only the pair is auditable: a bare date cannot
    # distinguish a person who read the body from a process that stamped it, so the two records are
    # byte-identical and the unearned one is invisible for ever. `verified_by` is OPTIONAL for now
    # (see `verify-audit`), so this checks coherence only and refuses nothing that exists today.
    # `_iso_date` is the check, not a membership test: `verified: null` parses to None and `str(None)`
    # is "None", which passes any "is it empty or the literal unverified" test while being no date at
    # all. Using the parser also makes the message true of a typo'd date rather than only of a blank.
    if meta.get("verified_by") and _iso_date(meta.get("verified")) is None:
        errors.append("verified_by is set but verified is not a date; name the date that was verified")
    # `verified_by` is a GID, which is what the report would otherwise print at a person. `owner_gid` has
    # `owner_name` and `prescan --commit` requires the pair, so a readable twin is the house pattern for
    # an identity a human reads. A name with no gid is the incoherent direction: it identifies nobody the
    # estate can route to.
    if meta.get("verified_by_name") and not meta.get("verified_by"):
        errors.append("verified_by_name is set without verified_by; the gid is what identifies the person")
    if meta.get("attested_by_name") and not meta.get("attested_by"):
        errors.append("attested_by_name is set without attested_by; the gid is what identifies the person")

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
        # `.claude/worktrees/<task>` holds a SECOND checkout of this repo, so rglob finds every nugget
        # twice and every count in every report silently doubles. The estate works worktree-per-session,
        # so one is present most of the time; on 2026-09-02 a peer's worktree made pin-audit report 264
        # rows for 132 nuggets. Excluded by PATH, not by gitignore, because rglob never consults git.
        #
        # RELATIVE to root, which is the whole trick. Testing md.parts against the ABSOLUTE path excludes
        # everything the moment the repo itself sits under such a directory, and that is the normal case
        # here: a linked worktree lives at <repo>/.claude/worktrees/<task>, so every file in it carries
        # `.claude` in its absolute parts. The first version of this filter did exactly that and emptied
        # the corpus, which showed up as seven unrelated checks reporting zero nuggets.
        try:
            rel_parts = md.relative_to(root).parts
        except ValueError:  # not under root at all; nothing sensible to say about it
            continue
        if ".claude" in rel_parts or ".git" in rel_parts:
            continue
        if md.name in {"README.md", "registry.md", "AGENTS.md", "CLAUDE.md"} or "sources" in md.parts:
            continue
        meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
        if not meta.get("id"):
            continue
        nuggets.append({"meta": meta, "body": body, "path": md.relative_to(root)})
    return nuggets


def _registry_entry(n: dict, last_used_map: dict | None = None) -> dict:
    m = n["meta"]
    last_used = (last_used_map or {}).get(m["id"])
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
        # Derived, additive, manager-owned: most recent real usage (from the interaction log), null until any
        # usage is seen. Carries the "in active use" signal to the rot sweep across hosts via the registry;
        # never a substitute for `last_verified`, which stays a human-only field.
        "last_used": last_used.strftime("%Y-%m-%dT%H:%M:%SZ") if last_used else None,
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

    # The repo column names the source repo that holds each entry's body, so a reader of a sliced mirror
    # (which carries the AllStaff base plus its own) can tell which repo to pull for a given nugget. It is
    # blank for a self-only mirror built by `index --repo`, whose entries carry no source_repo.
    lines.append("| id | title | domain | type | status | verified | repo |")
    lines.append("|---|---|---|---|---|---|---|")
    for e in sorted(entries, key=lambda e: e["id"]):
        row = (e.get("id"), e.get("title"), e.get("domain"), e.get("type"),
               e.get("status"), e.get("last_verified"), e.get("source_repo"))
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


def derive_slices(aggregate: dict, audiences: dict) -> dict:
    """Derive each managed repo's audience-scoped registry slice from the full aggregate.

    A slice is what a repo publishes to its readers: the aggregate entries they are cleared to see, per the
    manifest [audiences] map (for example Technical = [AllStaff, Technical], so the Technical repo carries the
    AllStaff base plus its own entries). Returns {repo_key: [entry, ...]}; each entry keeps its source_repo so
    a reader knows which repo holds the body (an AllStaff base entry in the Technical slice still lives in the
    AllStaff repo).

    Leak-safe by construction and DEFAULT-DENY: an entry is included only when the audience of its source_repo
    is in the reader's visible set, and a repo whose audience label is absent from the [audiences] map gets an
    empty slice, never the full aggregate. So restricted metadata can never fall into a lower-clearance slice.
    """
    repos = aggregate.get("repos", {})
    entries = aggregate.get("entries", [])
    # audience label of each source_repo key, e.g. {"allstaff": "AllStaff", "technical": "Technical"}.
    repo_audience = {key: (spec or {}).get("audience") for key, spec in repos.items()}
    slices: dict = {}
    for key, spec in repos.items():
        visible = set(audiences.get((spec or {}).get("audience"), []))
        slices[key] = [e for e in entries if repo_audience.get(e.get("source_repo")) in visible]
    return slices


def _slice_doc(audience: str, generated_utc: str, entries: list) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "audience": audience,
        "entries": entries,
    }


def _publish_slice(repo_dir: Path, audience: str, generated_utc: str, entries: list) -> tuple:
    """Commit and push a repo's registry slice (registry.json + registry.md) into its own clone, on change.

    The manager owns these two generated files, so publishing them is a manager write, not reader drift
    (both are skipped by `_load_nuggets`). Change is measured on the slice ENTRIES and audience only, never
    the generated_utc stamp, so an unchanged slice never churns a daily commit. Returns (new_head|None, note):
    'unchanged' skips the commit, 'published' pushed a new commit (new_head is its sha, so the caller can keep
    the sync baseline coherent), and any 'publish failed ...' leaves the remote untouched (for example when
    run without write access, so the read-only cron path degrades quietly rather than aborting).
    """
    registry_path = repo_dir / "registry.json"
    mirror_path = repo_dir / "registry.md"
    new_doc = _slice_doc(audience, generated_utc, entries)
    if registry_path.exists():
        try:
            existing = json.loads(registry_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = {}
        if existing.get("entries") == entries and existing.get("audience") == audience:
            return None, "unchanged"
    registry_path.write_text(json.dumps(new_doc, indent=2) + "\n", encoding="utf-8")
    mirror_path.write_text(_registry_markdown(audience, generated_utc, entries), encoding="utf-8")

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo_dir)).stdout.strip()
    if branch in ("", "HEAD"):  # detached: fall back to the remote default branch
        ref = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=str(repo_dir)).stdout.strip()
        branch = ref.split("/", 1)[-1] if ref else "main"
        _git(["checkout", "-B", branch], cwd=str(repo_dir))
    if _git(["add", "registry.json", "registry.md"], cwd=str(repo_dir)).returncode != 0:
        return None, "publish failed (add)"
    commit = _git(["-c", "user.name=kb-manager", "-c", "user.email=kb-manager@kacific.local",
                   "commit", "--quiet", "-m", f"kb: publish registry slice for {audience}"], cwd=str(repo_dir))
    if commit.returncode != 0:
        return None, "publish failed (commit)"
    if _git(["push", "--quiet", "origin", branch], cwd=str(repo_dir)).returncode != 0:
        return None, "publish failed (push; write access?)"
    head = _git(["rev-parse", "HEAD"], cwd=str(repo_dir))
    return (head.stdout.strip() if head.returncode == 0 else None), "published"


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


# --- write-path guard: worktree discipline ----------------------------------
#
# Two rules, checked at the moment a nugget is written rather than at commit time. Commit
# time is too late and, worse, it is blind: the breaches that motivated this wrote files
# and never moved HEAD, so nothing keyed on a commit could ever have seen them.
#
#   R1 REFUSES a write into a shared main checkout of a governed repo. Pure stdlib, right
#      here, with no dependency on anything outside this file. That is deliberate: R1 is
#      the half that fails closed, so nothing optional may be able to break it.
#   R2 WARNS when another live session is working in the destination worktree. It needs
#      the machine's session store, which no clone carries, so it is optional by
#      construction and silent when absent.
#
# Scope is narrow on purpose. This is not a git hook, it cannot block a commit or a push,
# and `store` has no automated callers (the one scripted caller writes to a temp dir that
# is not a git repo at all). An estate-wide hook that failed closed took out 136 hooks
# across 65 clones once already; the blast radius here is one interactive command.

# Emitted byte-identical into every governed repo's AGENTS.md from the repo-standards
# template, so its presence is a reliable machine signal that a repo opts into these rules.
MANAGED_BLOCK_MARKER = "kacific:concurrency-coordination"
# 2 is argparse's own usage-error code, so a caller keying on it could not tell "refused,
# go and make a worktree" from "you mistyped a flag". 1 is already the schema refusal.
GUARD_REFUSED_SHARED_CHECKOUT = 4
GUARD_REFUSED_INDETERMINATE = 5
_GUARD_GIT_TIMEOUT = 5

# git reads its own environment before it reads the filesystem, so an inherited GIT_DIR or
# GIT_COMMON_DIR silently redirects every question the guard asks and the answer comes back
# describing a DIFFERENT repository. That is not exotic: git exports GIT_DIR into hooks,
# `rebase --exec`, `bisect run`, `submodule foreach`, and the shell you are dropped into
# mid-rebase. Left unscrubbed it turned R1 from fail-closed into fail-OPEN, demonstrated by
# writing into a governed main checkout with one variable set.
_GUARD_GIT_ENV_STRIP = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _guard_git(args: list, cwd: str):
    """Run git for the guard. Returns stripped stdout, or None for any failure at all.

    The environment is scrubbed of every git-steering variable first, see above. Every
    exception is swallowed rather than propagated, and the caller reads None as
    could-not-determine. Note the exception is never stringified: a subprocess error
    embeds its full argv, which is how paths and credentials reach a transcript.
    """
    env = {k: v for k, v in os.environ.items() if k not in _GUARD_GIT_ENV_STRIP}
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                           timeout=_GUARD_GIT_TIMEOUT, stdin=subprocess.DEVNULL, env=env)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def _nearest_existing_dir(path) -> str:
    """The closest existing ancestor of `path`. The destination directory is created by
    the write itself, so the guard has to stand somewhere real to ask git anything."""
    p = os.path.realpath(str(path))
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return ""
        p = parent
    return p


def _repo_governance(root: str):
    """True governed, False not governed, None COULD NOT LOOK.

    The three must stay distinct. Collapsing could-not-look into not-governed is how a
    repo with an unreadable AGENTS.md quietly stops being policed, and this function had
    exactly that defect: a bare `except OSError` swallowed a permission error and returned
    the same value as a repo that simply has no managed block.

    AGENTS.md is usually a symlink to CLAUDE.md, so both are consulted and either
    satisfies it. A DANGLING symlink is could-not-look, not absent: something meant to be
    there and is not resolvable, which is a fact about the repo rather than about its
    governance.
    """
    unreadable = False
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(root, name)
        try:
            if p.is_symlink() and not p.exists():
                unreadable = True
                continue
            if MANAGED_BLOCK_MARKER in p.read_text(encoding="utf-8"):
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError, which is NOT an OSError and used to
            # escape as a traceback in the user's face on a single non-UTF-8 byte.
            unreadable = True
    return None if unreadable else False


def _checkout_kind(cwd: str):
    """"main", "worktree", or None for could-not-determine.

    A main checkout has its git dir EQUAL to the common git dir; a linked worktree's git
    dir is .git/worktrees/<name> underneath it. Both paths are taken in absolute form from
    the same cwd so there is nothing to resolve by hand.

    `--path-format=absolute` needs git 2.31+. On an older git this returns None, which the
    caller treats as indeterminate and therefore REFUSES. An earlier version tried to be
    helpful here by falling back to the bare option and joining the relative answer onto
    the directory git was asked from. That was wrong in the one direction that matters:
    `--git-common-dir` answers relative to the repo TOP LEVEL on some versions, while the
    directory being asked from is routinely a subdirectory (the domain directory of an
    existing KB repo), so the join produced <repo>/technical/.git, which does not match the
    real git dir, and a mismatch is read as "linked worktree" and ALLOWED. A guard whose
    compatibility shim fails open is worse than one that refuses on an old git and says so.
    """
    git_dir = _guard_git(["rev-parse", "--absolute-git-dir"], cwd=cwd)
    common = _guard_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    if not git_dir or not common:
        return None
    return "main" if os.path.realpath(git_dir) == os.path.realpath(common) else "worktree"


def guard_write_destination(dest_dir) -> tuple:
    """R1. Returns (exit_code, lines): 0 to allow, non-zero to refuse.

    The decision ladder, and each rung's default matters:
      not inside any git repo       -> ALLOW  (temp dirs, the acceptance test, any scratch)
      innermost is a linked worktree-> ALLOW  (the sanctioned place to write; walk stops)
      an enclosing GOVERNED repo is
        a shared main checkout      -> REFUSE (the rule this whole guard exists for)
      no enclosing governed repo    -> ALLOW  (nobody opted in; not ours to police)
      anything indeterminate        -> REFUSE (operator ruling: ambiguity fails closed)

    "Shared main checkout" is decided by comparing the git dir with the common git dir. In
    a main checkout they are the same directory; in a linked worktree the former is
    .git/worktrees/<name> under the latter. This avoids having to identify WHICH clone is
    the declared read-only mirror, which nothing on disk marks: an incidental all-org
    mirror is by definition a main checkout, so it is covered without being named.

    INDETERMINATE IS A REAL CATEGORY AND MUST STAY ONE. Everything that means "could not
    look" refuses: no resolvable parent directory, git absent from PATH, a governance file
    that exists but will not read, a git too old to answer the worktree question, or a
    destination inside a .git directory. Each of those used to return the same value as a
    clean allow, which is how a guard reports a confident all-clear about a question it
    never managed to ask.
    """
    def indeterminate(why, root=None):
        lines = ["REFUSED. The guard could not determine whether this destination is safe,",
                 "and ambiguity refuses here rather than guessing.",
                 "  destination: %s" % os.path.realpath(str(dest_dir)),
                 "  reason:      %s" % why]
        if root:
            lines.append("  repo:        %s" % root)
        return GUARD_REFUSED_INDETERMINATE, lines

    base = _nearest_existing_dir(dest_dir)
    if not base:
        return indeterminate("no existing parent directory of the destination could be resolved")
    if shutil.which("git") is None:
        # Without git nothing below can be answered, and every question would return None,
        # which the old ladder read as "not a git repo" and ALLOWED. A missing tool is a
        # could-not-look, never an all-clear.
        return indeterminate("git is not on PATH, so no repository question can be answered")
    if _guard_git(["rev-parse", "--is-inside-git-dir"], cwd=base) == "true":
        return indeterminate("the destination is inside a .git directory")

    # Walk OUTWARD through enclosing repositories. The innermost answer alone is not
    # enough: a submodule or any vendored clone inside a governed checkout is its own
    # repository, carries no managed block, and used to be allowed, which let a write land
    # inside the very checkout being policed.
    #
    # The walk stops at the first LINKED WORKTREE, deliberately. Worktrees live at
    # <repo>/.claude/worktrees/<task>, so they sit inside the governed main checkout's own
    # directory tree; continuing outward from one would find the parent main checkout and
    # refuse the sanctioned destination.
    cur, seen = base, set()
    while cur:
        root = _guard_git(["rev-parse", "--show-toplevel"], cwd=cur)
        if not root:
            break  # no further enclosing repository
        root = os.path.realpath(root)
        if root in seen:
            break
        seen.add(root)

        governed = _repo_governance(root)
        if governed is None:
            return indeterminate("AGENTS.md or CLAUDE.md exists but could not be read", root)

        kind = _checkout_kind(cur)
        if kind is None:
            return indeterminate(
                "could not tell a worktree from a main checkout (git older than 2.31?)", root)
        if kind == "worktree":
            return 0, []  # sanctioned destination, and the outward walk stops here
        if governed:
            return _refuse_shared(dest_dir, root)

        parent = os.path.dirname(root)
        cur = parent if parent != root and os.path.isdir(parent) else None

    return 0, []


def _refuse_shared(dest_dir, root: str) -> tuple:
    """The refusal message. It names the path, names the rule, and prints a runnable
    command, because a guard that only says no gets routed around and the workaround is
    worse than the thing being prevented."""
    return GUARD_REFUSED_SHARED_CHECKOUT, [
        "REFUSED. This write would land in a SHARED MAIN CHECKOUT, not a worktree.",
        # Resolved, not as typed: a relative --into printed verbatim beside an absolute
        # repo line reads as though they were two unrelated places.
        "  destination: %s" % os.path.realpath(str(dest_dir)),
        "  repo:        %s" % root,
        "",
        "  Why: this repo's AGENTS.md carries the managed concurrency block, which says",
        "  never to edit in a shared main checkout. A peer's checkout can move a branch",
        "  under you mid-write, and an incidental all-org mirror is read-only and drifts.",
        "",
        "  Fix, copy and run (rename the worktree and branch if you prefer):",
        "    git -C %s fetch --prune origin main" % root,
        "    git -C %s worktree add .claude/worktrees/kb-store -b kb/store-nugget origin/main"
        % root,
        "  then re-run this store with:",
        "    --into %s" % os.path.join(root, ".claude", "worktrees", "kb-store"),
    ]


def guard_occupancy_warnings(dest_dir) -> list:
    """R2. Advisory only: returns lines to print, never blocks, never raises.

    The helper lives on the operator's machine, outside any clone, so its path arrives by
    environment variable. That is not a style choice: routing it through config.toml would
    make `store` config-dependent, and config parsing needs tomllib, which would impose a
    Python 3.11 floor on a command that deliberately runs on any Python 3.

    Unset variable means R2 is simply not configured, which is every cold clone and every
    CI run, so it stays silent. Set but unusable is a misconfiguration on a machine that
    meant to have it, so it says so.
    """
    lib = os.environ.get("KACIFIC_ESTATE_LIB")
    if not lib:
        return []
    # Loaded by explicit file path rather than by prepending an operator directory to
    # sys.path, which would shadow the stdlib for the rest of the process for any name
    # that directory ever gains.
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "kacific_worktree_guard", os.path.join(lib, "worktree_guard.py"))
        if spec is None or spec.loader is None:
            raise ImportError("no loadable worktree_guard.py")
        mod = importlib.util.module_from_spec(spec)
        if lib not in sys.path:
            sys.path.append(lib)  # appended, not prepended: the helper imports a sibling
        spec.loader.exec_module(mod)
    except Exception:
        # Deliberately ONE branch. Splitting import errors from others meant an exception
        # raised inside the helper was reported as "could not be imported", which sends
        # the reader to look at the wrong thing entirely.
        return ["NOTE: KACIFIC_ESTATE_LIB is set to %s but the occupancy helper could not" % lib,
                "      be loaded, so the live-session check did not run.",
                "      Proceeding. This is a could-not-look, not an all-clear."]
    try:
        return mod.warning_lines(str(dest_dir))
    except Exception:
        return ["NOTE: the live-session occupancy check failed and was skipped.",
                "      Proceeding. This is a could-not-look, not an all-clear."]


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
        # expanduser first: a quoted or non-shell-expanded ~ used to create a literal "~"
        # directory under the cwd and report success, which also put the destination
        # outside any repo and so outside the guard entirely.
        dest_dir = Path(os.path.expanduser(args.into)) / (meta.get("domain") or "shared")
        # The write-path guard sits here, before the mkdir, because creating the domain
        # directory is itself a write into the checkout being policed.
        code, refusal = guard_write_destination(dest_dir)
        if code:
            for line in refusal:
                print(line, file=sys.stderr)
            return code
        for line in guard_occupancy_warnings(dest_dir):
            print(line, file=sys.stderr)
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

        # Derive each repo's audience slice from the aggregate and write it to the control home for review,
        # always (even without --publish), so the slices can be eyeballed before they reach any data repo.
        slices = derive_slices(agg, manifest["audiences"])
        slices_dir = manifest["manifest_path"].parent / "slices"
        slices_dir.mkdir(parents=True, exist_ok=True)
        for key, entries in sorted(slices.items()):
            audience = (agg["repos"].get(key) or {}).get("audience") or key
            (slices_dir / f"{key}.registry.json").write_text(
                json.dumps(_slice_doc(audience, agg["generated_utc"], entries), indent=2) + "\n",
                encoding="utf-8")

        # --publish commits each CHANGED slice into its own data repo (a dev-Mac write step; the NUC cron
        # keeps its read-only posture and never passes --publish). A published slice moves that repo's HEAD,
        # so record the post-publish sha into the aggregate baseline to keep the next `sync` free of a
        # spurious "repo changed" line.
        published: dict = {}
        if getattr(args, "publish", False):
            cache_dir = manifest["cache_dir"]
            for key, entries in sorted(slices.items()):
                repo = agg["repos"].get(key) or {}
                if repo.get("status") != "ok":
                    published[key] = f"skipped ({repo.get('status')})"
                    continue
                audience = repo.get("audience") or key
                new_head, note = _publish_slice(cache_dir / key, audience, agg["generated_utc"], entries)
                published[key] = note
                if new_head:
                    agg["repos"][key]["head_sha"] = new_head

        out_path = (
            Path(args.out).expanduser() if args.out
            else manifest["manifest_path"].parent / "registry-aggregate.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(agg, indent=2) + "\n", encoding="utf-8")
        ok = [k for k, r in agg["repos"].items() if r["status"] == "ok"]
        print(f"OK: aggregate written to {out_path} "
              f"({len(agg['entries'])} entries across {len(ok)}/{len(agg['repos'])} managed repos).")
        print(f"OK: {len(slices)} audience slices written to {slices_dir}"
              + ("" if getattr(args, "publish", False) else " (review only; pass --publish to commit them)"))
        for key in sorted(published):
            print(f"  publish {key}: {published[key]}")
        for key, r in sorted(agg["repos"].items()):
            if r["status"] != "ok":
                print(f"  WARN: repo '{key}': {r['status']}", file=sys.stderr)
        return 0

    if not args.repo:
        print("index: give a repo path, or --manifest to build the cross-repo aggregate.", file=sys.stderr)
        return 2
    root = Path(args.repo)
    last_used = _last_used_from_logfile(getattr(args, "log_file", None))
    entries = [_registry_entry(n, last_used) for n in _load_nuggets(root)]
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
        if m.get("source"):
            return m["source"]
        if m.get("attested_by"):
            return f"attested by {_person(m.get('attested_by'), m.get('attested_by_name'))}"
        return m["id"]

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


def _last_used_map(records: list[dict]) -> dict:
    """Map nugget id -> most recent usage datetime, from interaction-log records.

    A nugget is "used" when it is cited in a hit answer (`_log_interaction` writes {ts, hit, cited}). The
    ROT-OUTDATED rule reads this so an in-use, still-under-ceiling nugget is left alone instead of raising a
    re-verification task nobody is waiting on. Non-hit or malformed records are ignored; a missing ts skips.
    """
    used: dict = {}
    for r in records:
        if not r.get("hit"):
            continue
        ts = _parse_iso_utc(r.get("ts"))
        if ts is None:
            continue
        for nid in r.get("cited") or []:
            prev = used.get(nid)
            if prev is None or ts > prev:
                used[nid] = ts
    return used


def _last_used_from_logfile(log_file) -> dict:
    """Build the last_used map from an optional interaction-log PATH, or {} when absent/uncollectable.

    A convenience over `_read_interaction_log` + `_last_used_map` for the `rot` and single-repo `index`
    callers, which take a `--log-file` path rather than records already in hand (as `feedback` does).
    """
    if not log_file:
        return {}
    records, collected = _read_interaction_log(Path(log_file))
    return _last_used_map(records) if collected else {}


def _rot_flags(nuggets: list[dict], now: datetime, last_used_map: dict | None = None) -> list[dict]:
    """Compute the Redundant / Outdated / Trivial flags for a nugget set. The single source of the ROT rules.

    Both `rot` (which reports them, grouped by owner) and `feedback` (which turns them into audit-family
    findings) call this, so the flag rules live in exactly one place. Each flag is {id, path, owner, reasons}.

    `last_used_map` (nugget id -> last-used datetime, from `_last_used_map`) softens the Outdated rule: a
    nugget in active use inside USAGE_WINDOW_DAYS is not flagged Outdated until it also crosses
    ROT_HARD_CEILING_DAYS. An empty or absent map reproduces the pre-usage behaviour exactly.
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
            # A never-verified nugget has had no human confirmation at all; use never excuses it.
            reasons.append("Outdated (never verified)")
        elif (now - verified).days > ROT_OUTDATED_DAYS:
            age = (now - verified).days
            last_used = (last_used_map or {}).get(m["id"])
            used_recently = last_used is not None and (now - last_used).days <= USAGE_WINDOW_DAYS
            if not (used_recently and age <= ROT_HARD_CEILING_DAYS):
                reasons.append(f"Outdated (verified {age} days ago)")
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
    last_used = _last_used_from_logfile(getattr(args, "log_file", None))
    flags = _rot_flags(nuggets, datetime.now(timezone.utc), last_used)

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


# Per-finding-id Background (the condition that tripped) and Expected action (the remediation), for the Asana
# notes body. Every KB task must carry a complete, self-contained notes body built BEFORE the POST
# (feedback_asana_task_completeness): a bare `[<id>] <subject>` title plus a one-line reason is a defect. The
# assignee opens the task cold and must understand, without asking, where it came from and what to do. Keep
# these deterministic (no clock, no run-specific state) so a re-create or backfill of the same finding yields a
# byte-identical body.
_FINDING_BACKGROUND = {
    _GAP_ID: "flagged as a knowledge Gap: one or more logged reader queries found no matching nugget, so the "
             "KB could not answer.",
    _CONFLICT_ID: "flagged as a Conflict: a logged query matched several nuggets that were cited together as "
                  "a tie, so the KB could not resolve to a single answer.",
    _ROT_IDS["Outdated"]: "flagged Outdated: its `verified` date is missing or older than the freshness "
                          "window, so the fact it holds may no longer be current.",
    _ROT_IDS["Redundant"]: "flagged Redundant: it duplicates an id, shares a source, is superseded by, or "
                           "carries a retired/archived status relative to another nugget.",
    _ROT_IDS["Trivial"]: "flagged Trivial: its body is near-empty, so it may hold no fact worth keeping.",
}
_FINDING_ACTION = {
    _GAP_ID: "Author a nugget that answers this query and store it through the gate (a reference or a named "
             "attestation is required), or dismiss this task if the query is out of scope or a false positive.",
    _CONFLICT_ID: "Reconcile the cited nuggets so one answers the query (retire or merge the duplicates, or "
                  "mark the authoritative one), or dismiss this task if the tie is acceptable.",
    _ROT_IDS["Outdated"]: "Re-verify the nugget and refresh its `verified` date, or retire it if it is no "
                          "longer accurate. Dismiss this task if the freshness flag is wrong.",
    _ROT_IDS["Redundant"]: "Retire, merge, or repoint the nugget so a single source of truth remains (resolve "
                           "the duplicate id, shared source, supersession, or retired status). Dismiss this "
                           "task if the flag is wrong.",
    _ROT_IDS["Trivial"]: "Expand the nugget with substantive content, or retire it if it holds no fact worth "
                         "keeping. Dismiss this task if the body is intentionally short and the owner confirms.",
}
# The close mechanism is identical for every KB finding: the audit family lets re-discovery be the judge of
# "done" (kacific-audit-governance), so a task is never hand-closed.
_FINDING_CLEARS = (
    "This task closes itself. The next `kb.py feedback` run re-discovers the finding set; once this condition "
    "is gone from live state the task is completed as verified-clear. Do not close it by hand: a task closed "
    "while its condition still exists on live state is reopened as verification-failed on the next run."
)


def _finding_evidence(finding: dict) -> str:
    """The Evidence block: the specific subject, the owner (for a ROT nugget), and the observed value/source
    the flag was read from. Labelled per family so the subject reads correctly (a query, cited ids, or a
    nugget id)."""
    fid = finding["finding_id"]
    subject = _finding_subject(finding)
    detail = str(finding.get("detail") or "").strip()
    lines: list[str] = []
    if fid == _GAP_ID:
        lines.append(f"Query: {subject}")
    elif fid == _CONFLICT_ID:
        lines.append(f"Cited nuggets: {subject}")
    else:  # a ROT finding: the subject is the nugget id, and entity is its owner
        lines.append(f"Nugget: {subject}")
        owner = str(finding.get("entity") or "").strip()
        if owner and owner != _TRIAGE:
            lines.append(f"Owner: {owner}")
    if detail:
        lines.append(f"Observed: {detail}")
    return "\n".join(lines)


def _finding_notes(finding: dict) -> str:
    """The complete, self-contained Asana notes body for one KB finding, built BEFORE the POST
    (feedback_asana_task_completeness). Four sections in order: Background (which audit raised it + the
    condition that tripped), Evidence (subject, owner, observed value/source), Expected action (remediation +
    dismiss path), and How it clears (re-discovery auto-closes it; never hand-close). Covers GAP, CONFLICT and
    the three ROT ids; an unknown id degrades to a generic background/action rather than an empty body.
    Deterministic (no clock, no run-specific state), so a steady-state re-create or backfill is byte-stable."""
    fid = finding["finding_id"]
    background = _FINDING_BACKGROUND.get(fid, "flagged by the KB health audit.")
    action = _FINDING_ACTION.get(
        fid, "Review the finding and remediate the nugget, or dismiss this task if the flag is wrong.")
    return (
        f"Background\n"
        f"Raised by the KB health audit (kb.py). This finding is {background}\n\n"
        f"Evidence\n{_finding_evidence(finding)}\n\n"
        f"Expected action\n{action}\n\n"
        f"How it clears\n{_FINDING_CLEARS}"
    )


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
    """Owned tasks currently in the KB section, {title: {gid, completed, notes}}. Section-scoped and
    OWNED_RE-filtered, so [Build]/[Chip] tasks (in the project's default section) are never even seen. No
    `completed_since` is passed, so completed tasks ARE returned and reopen can fire on a human-closed KB task.
    `notes` is read so the backfill can compare a task's live body against the compliant one and heal it only
    when it differs (idempotent)."""
    out: dict = {}
    for t in client.get_all(f"/sections/{section_gid}/tasks", {"opt_fields": "name,completed,notes"}):
        name = t.get("name", "")
        if _finding_id_from_title(name):
            out[name] = {"gid": t.get("gid"), "completed": bool(t.get("completed")),
                         "notes": t.get("notes") or ""}
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
    body = {"name": _finding_title(finding), "notes": _finding_notes(finding), "projects": [project_gid]}
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


def _backfill_task_notes(client, task: dict, finding: dict, commit: bool, counts: dict) -> None:
    """Heal one existing owned task's notes to the compliant body when they differ (idempotent). Used by the
    `--backfill-notes` path so tasks created before the complete-notes rule (or by an older tool version) get
    a full Background/Evidence/Expected action/How it clears body, without touching any foreign task. A no-op
    when the notes already match, so a re-run writes nothing; commit-gated, but the count is reported on the
    dry-run so the write can be reviewed first."""
    desired_notes = _finding_notes(finding)
    if (task.get("notes") or "") == desired_notes:
        return
    counts["notes_updated"] += 1
    if not commit:
        return
    try:
        client.put(f"/tasks/{task['gid']}", {"notes": desired_notes})
    except _AsanaError:
        counts["failed"] += 1


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


def _reconcile(client, cfg: dict, findings: list, active_ids: set, commit: bool,
               backfill_notes: bool = False) -> dict:
    """The audit-family reconcile: create / no-op / verify-clear / reopen the tool's own `[KB-*]` tasks in the
    KB section, gated by the active-family set. Re-discovery is the judge of "done": a finding still present
    keeps (or reopens) its task; a finding absent from the freshly-computed set (its family collected)
    verify-clears its task; a family NOT collected this run is left untouched.

    `backfill_notes` additionally heals the notes body of an existing owned task whose finding is still present,
    so tasks created before the complete-notes rule get a compliant body. It is idempotent (writes only when
    the notes differ) and opt-in, so a normal reconcile keeps its steady-state zero-write profile."""
    tracking = cfg.get("tracking", {}) or {}
    project_gid = str(tracking.get("project_gid") or "").strip()
    workspace_gid = str(tracking.get("workspace_gid") or "").strip()
    section_name = str(tracking.get("section_name") or "KB Findings").strip()
    field_name = str(tracking.get("verification_field") or "Verification").strip()
    if not project_gid:
        raise SystemExit("reconcile needs [tracking].project_gid in config.toml")

    counts = {"created": 0, "noop": 0, "reopened": 0, "verify_cleared": 0,
              "skipped_inactive": 0, "reordered": 0, "notes_updated": 0, "failed": 0}

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
            if backfill_notes:
                _backfill_task_notes(client, task, finding, commit, counts)
            counts["reopened"] += 1
        else:
            if backfill_notes:
                _backfill_task_notes(client, task, finding, commit, counts)
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
            # Usage softens the Outdated flag: a nugget cited by a recent hit answer is left alone until the
            # hard ceiling. The signal comes from the same interaction log this sweep already read; if the log
            # was not collected the map is empty and the rule behaves exactly as before.
            last_used = _last_used_map(records) if log_collected else {}
            findings.extend(_rot_findings(_rot_flags(nuggets, datetime.now(timezone.utc), last_used)))
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

    backfill_notes = getattr(args, "backfill_notes", False)

    # A plain dry-run stays fully offline (no PAT): it has already printed the plan above. A --backfill-notes
    # dry-run is the exception: previewing which task bodies would change needs to READ the live tasks, so it
    # resolves the PAT and runs the reconcile read-only (commit=False -> GETs only, zero writes) to report the
    # notes_updated count before a committing run applies it.
    if not args.commit and not backfill_notes:
        return 0

    # Live reconcile. Even with zero findings this must run, so a condition that has cleared (its family
    # collected) gets its task verify-cleared. The PAT is resolved here, never on the plain-dry-run path.
    config = load_config(args.config)
    pat = _resolve_tracking_pat(config)
    client = _AsanaClient(pat)
    active_ids = _active_ids(log_collected, rot_collected)
    try:
        counts = _reconcile(client, config, findings, active_ids, args.commit, backfill_notes=backfill_notes)
    except _AsanaError as exc:
        print(f"feedback: Asana reconcile failed: {exc}", file=sys.stderr)
        return 1
    mode = "reconcile" if args.commit else "reconcile (read-only preview)"
    print(f"{mode}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


# --- coordination-audit: close opt-in coordination tasks when their tracked findings clear ----------------
# A coordination task ([Build]/[Chip]/[KB] in the tracking project, NOT a [KB-*] finding) may OPT IN to
# auto-close by putting a machine-readable anchor on the FIRST non-empty line of its notes:
#     closes-when-cleared: KB-ROT-OUTDATED, KB-ROT-REDUNDANT
# It then closes automatically once no OPEN [KB-*] finding task carrying any of those ids remains in the KB
# Findings section. Default (no anchor) is unchanged: coordination tasks close by human judgement on landing.
#
# This exists because kb.py feedback reconcile is section-scoped to KB Findings and its own [KB-*] ids by a
# correctness invariant (_OWNED_RE), so it never sees a coordination task; wording one "awaiting auto-verify"
# was a category error with no mechanism behind it. This subcommand is that mechanism, kept strictly separate:
# it never routes through _reconcile, never touches a [KB-*] finding task (they carry a finding-id and are
# excluded), never closes on an unreadable findings section or an unrecognised declared id, and re-checks
# each task's live state immediately before writing (the shared asana_sc_pat means a peer may have closed it).

_CLOSES_ANCHOR_RE = re.compile(r"^\s*closes-when-cleared:\s*(.+?)\s*$", re.IGNORECASE)


def _parse_closes_anchor(notes: str) -> list[str] | None:
    """The finding-id list a coordination task declares in its `closes-when-cleared:` anchor, or None when the
    task carries no anchor. The anchor MUST be on the first non-empty line (mirroring the shepherd's
    rootcause-key precedent), so a task that merely mentions the phrase lower in its prose is not mistaken for
    an opt-in. Ids may be comma- and/or whitespace-separated; the list is de-duplicated, order preserved."""
    for line in (notes or "").splitlines():
        if not line.strip():
            continue
        m = _CLOSES_ANCHOR_RE.match(line)
        if not m:
            return None  # first non-empty line is not the anchor -> not opted in
        seen: list[str] = []
        for tok in m.group(1).replace(",", " ").split():
            if tok and tok not in seen:
                seen.append(tok)
        return seen
    return None


def _run_coordination_audit(client, project_gid: str, section_name: str, commit: bool) -> dict:
    """The core of `coordination-audit`, driven with an injected client so it is unit-testable offline exactly
    like `_reconcile`. Reads the KB Findings section and the tracking project, closes each opted-in coordination
    task whose declared finding-ids have no OPEN [KB-*] task left, and returns the counts. A READ failure
    (sections / section tasks / project tasks) raises `_AsanaError` so the caller does nothing (a missed close
    beats a false one); a per-task WRITE failure is caught and counted, never aborting the sweep."""
    counts = {"closed": 0, "would_close": 0, "still_open": 0, "skipped": 0, "failed": 0}

    section_gid = None
    for s in client.get_all(f"/projects/{project_gid}/sections", {"opt_fields": "name"}):
        if s.get("name") == section_name:
            section_gid = s.get("gid")
            break
    if not section_gid:
        print(f"coordination-audit: KB Findings section '{section_name}' not found; nothing to do.")
        return counts
    kb_tasks = _existing_kb_tasks(client, section_gid)
    all_tasks = client.get_all(f"/projects/{project_gid}/tasks", {"opt_fields": "name,completed,notes"})

    # Finding-ids that still have an OPEN [KB-*] task, and every finding-id seen at all (open or closed). A
    # declared id absent from `seen_ids` has no finding in the section, so closing on it would be vacuous.
    open_ids = {_finding_id_from_title(t) for t, m in kb_tasks.items() if not m["completed"]}
    seen_ids = {_finding_id_from_title(t) for t in kb_tasks}
    open_ids.discard(None)
    seen_ids.discard(None)

    for t in all_tasks:
        name = t.get("name", "")
        if t.get("completed") or _finding_id_from_title(name):
            continue  # closed already, or a [KB-*] finding (never a coordination task)
        declared = _parse_closes_anchor(t.get("notes") or "")
        if declared is None:
            continue  # not opted in
        gid = t.get("gid")
        unknown = [d for d in declared if d not in _OWNED_IDS]
        if unknown:
            print(f"coordination-audit: SKIP '{name}' - unrecognised declared id(s): {', '.join(unknown)}")
            counts["skipped"] += 1
            continue
        if not any(d in seen_ids for d in declared):
            print(f"coordination-audit: SKIP '{name}' - no finding for declared id(s) exists in "
                  f"'{section_name}'; not closing vacuously.")
            counts["skipped"] += 1
            continue
        remaining = sorted(d for d in declared if d in open_ids)
        if remaining:
            print(f"coordination-audit: leave open '{name}' - still-open finding id(s): {', '.join(remaining)}")
            counts["still_open"] += 1
            continue
        ids_txt = ", ".join(declared)
        if not commit:
            print(f"coordination-audit: WOULD close '{name}' - tracked findings all clear ({ids_txt}).")
            counts["would_close"] += 1
            continue
        try:
            # Compare-and-swap: the shared asana_sc_pat means a peer may have closed it since the project read.
            live = client.get(f"/tasks/{gid}", {"opt_fields": "completed"})
            if (live.get("data") or {}).get("completed"):
                print(f"coordination-audit: '{name}' already closed by a peer; skipping.")
                continue
            client.post(f"/tasks/{gid}/stories",
                        {"text": f"Auto-closed by kb.py coordination-audit: every tracked finding is cleared "
                                 f"({ids_txt}); 0 open in the {section_name} section as of {_now_iso()}. "
                                 f"Opt-in via the closes-when-cleared anchor."})
            client.put(f"/tasks/{gid}", {"completed": True})
            print(f"coordination-audit: closed '{name}' ({ids_txt}).")
            counts["closed"] += 1
        except _AsanaError as exc:
            print(f"coordination-audit: FAILED to close '{name}': {exc}", file=sys.stderr)
            counts["failed"] += 1

    return counts


def cmd_coordination_audit(args) -> int:
    """Close opt-in coordination tasks whose tracked KB findings have all cleared.

    Finds non-finding coordination tasks in the tracking project carrying a `closes-when-cleared:` anchor on
    the first line of their notes, and closes each one whose declared finding-ids no longer have any OPEN
    [KB-*] task in the KB Findings section. Read-only unless --commit. Safe by construction: it never touches a
    [KB-*] finding task, never closes on an unreadable findings section, never closes on an unrecognised
    declared id or one with no finding in the section, and re-checks each task's live state before writing."""
    config = load_config(args.config)
    tracking = config.get("tracking", {}) or {}
    project_gid = str(tracking.get("project_gid") or "").strip()
    section_name = str(tracking.get("section_name") or "KB Findings").strip()
    if not project_gid:
        print("coordination-audit: [tracking].project_gid is required.", file=sys.stderr)
        return 2

    client = _AsanaClient(_resolve_tracking_pat(config))
    try:
        counts = _run_coordination_audit(client, project_gid, section_name, args.commit)
    except _AsanaError as exc:
        print(f"coordination-audit: read failed, doing nothing: {exc}", file=sys.stderr)
        return 1
    mode = "commit" if args.commit else "dry-run"
    print(f"coordination-audit ({mode}): " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


# --- prescan: seed-source scan -> ranked pointer candidates ------------------
# One-time bulk seeder. Scans each [seed_sources] repo from the manifest and turns every matching file into
# a ranked POINTER candidate (frontmatter + source + a one-line abstract, never a content copy). Report-only
# by default; --commit stages candidates under <manifest dir>/prescan-out/ for human review, and keepers then
# enter the KB through the normal `store --into` gate. Secrets-safe by construction: files that look like
# credential stores are skipped BY NAME before any open().

_SECRET_EXACT = {"config.py", "config.ini", "config.toml", ".env", ".envrc"}
_SECRET_GLOBS = ("*.pem", "*.key", "id_rsa*", "*credential*", "*secret*")
_BOILERPLATE_STEMS = {"readme", "index", "changelog", "license", "contributing"}
_ABSTRACT_MAX_CHARS = 200
# A first sentence may overrun the budget up to this ceiling rather than be dropped. One dial, derived, so
# the budget stays the only number to tune. Past the ceiling the abstract is dropped for the pointer line.
_ABSTRACT_HARD_MAX_CHARS = 2 * _ABSTRACT_MAX_CHARS

# Tokens that cannot end an English sentence. A cut abstract reads as finished because a terminator was
# appended to it, so "does it end with a full stop" scores a fragment clean; the dangling word is the tell.
#
# This is a BACKSTOP, not the mechanism. Whole-sentence selection is what prevents a cut; measured against
# the 68 fragments the old rule actually shipped, this catches 18, because a cut lands on a content word as
# often as on a function word ("holds copied training.", "and exposes.") and no word-list can see that.
# Read a clean result as "no dangling word", never as "not truncated".
#
# Deliberately EXCLUDED, though a cut does land on them: particles and quantifiers that legitimately end a
# sentence ("this follows the existing convention rather than inventing one.", "legwork that #7 should build
# on.", "like this."). Including them cost more than it bought. On the real corpus the wider list false-flagged
# 5 of 69 complete bodies, and in a fail-safe a false positive destroys a true abstract and publishes a
# pointer line in its place, so the two extra catches were not worth three good abstracts.
_DANGLING_TAIL_WORDS = frozenset("""
a an the and or but nor if while whilst because although though since unless until whether
that which who whom whose every
of to for with within without from into onto upon via per across
against between among during throughout toward towards than as like
is are was were be been being am has have had do does did will would shall should can could may might
must however moreover therefore thus hence rather
""".split())

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_SENTENCE_START = re.compile(r"""[A-Z0-9*`\[("'_#]""")
# Tokens whose trailing dot is an abbreviation, not a sentence end. A single letter (an initial) too.
_ABBREVIATIONS = frozenset("""
e.g i.e etc vs cf approx no nos fig figs al ltd inc co corp dept est dr mr mrs ms prof jr sr st
jan feb mar apr jun jul aug sep sept oct nov dec mon tue wed thu fri sat sun
""".split())


def _split_sentences(text: str) -> list:
    """Split prose into whole sentences, conservatively: when in doubt, do NOT split.

    A missed boundary only yields a shorter abstract, which is the safe direction; a wrong boundary
    would manufacture the fragment this whole function exists to prevent. So a terminator counts only
    when it is followed by whitespace, is not the dot of a known abbreviation or an initial, and the
    next sentence opens the way a sentence opens (including Markdown emphasis, a code span or a link).
    """
    sentences: list = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        i = match.start()
        word = re.split(r"[\s(\[]", text[:i])[-1].lower().lstrip("*`\"'([")
        if word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
            continue
        rest = text[i + 1:].lstrip()
        if rest and not _SENTENCE_START.match(rest[0]):
            continue
        sentences.append(text[start:i + 1].strip())
        start = i + 1
    return [s for s in sentences if s]


def _whole_sentences_within(text: str, budget: int) -> str:
    """The longest run of whole sentences from the start of text that fits the budget. Never a fragment."""
    kept = ""
    for sentence in _split_sentences(text):
        candidate = f"{kept} {sentence}".strip() if kept else sentence
        if len(candidate) > budget:
            break
        kept = candidate
    return kept


def _looks_truncated(text: str) -> bool:
    """True when text reads as cut mid-sentence, whatever punctuation was appended to it.

    Deliberately NOT a "does it end with a terminator" check: the defect this guards against appended a
    full stop to a fragment, so a terminator check scores it clean. The dangling function word is the signal.

    Partial by nature, and a BACKSTOP rather than the mechanism: whole-sentence selection is what prevents a
    cut. Measured against the 68 fragments the old rule actually shipped, this catches 18, because a cut
    lands on a content word as often as on a function word. False means "no dangling word", never "not
    truncated". Tuned instead for no false positives on real prose, since in a fail-safe a false positive
    destroys a true abstract and publishes a pointer line in its place.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if not stripped.endswith((".", "!", "?")):
        return True
    last = re.split(r"\s+", stripped.rstrip(".!?").rstrip())[-1] if stripped.rstrip(".!?").strip() else ""
    return last.strip("*`\"')]_").lower() in _DANGLING_TAIL_WORDS


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    return low in _SECRET_EXACT or any(fnmatch.fnmatch(low, g) for g in _SECRET_GLOBS)


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(1024)
    except OSError:
        return True


def _glob_match(rel: str, patterns: list) -> bool:
    """fnmatch over a POSIX relative path; a leading **/ also matches files at the repo root."""
    for g in patterns:
        if fnmatch.fnmatch(rel, g) or (g.startswith("**/") and fnmatch.fnmatch(rel, g[3:])):
            return True
    return False


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:max_len].rstrip("-") or "item"


def _plain_voice(text: str) -> str:
    """Collapse whitespace and swap em/en dashes so generated prose passes the voice gate."""
    return " ".join(str(text).replace(chr(0x2014), ", ").replace(chr(0x2013), "-").split())


def _seed_source_url(remote: str | None, sha: str | None, relpath: str) -> str:
    """A source string for a candidate: a GitHub blob URL pinned to HEAD when derivable, else path-style."""
    m = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", remote or "")
    if m and sha:
        return f"https://github.com/{m.group(1)}/blob/{sha}/{urllib.parse.quote(relpath)}"
    base = (remote or "").rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    return f"{base}/{relpath}" if base else relpath


def _norm_source(source) -> str:
    """Normalise a source string for dedup: GitHub blob URLs reduce to repo+path ignoring the ref."""
    s = str(source or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/[^/]+/(.+)$", s)
    if m:
        return f"github:{m.group(1)}/{urllib.parse.unquote(m.group(2))}"
    m = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+)$", s)
    if m:
        return f"github:{m.group(1)}"
    return s


def _extract_title_abstract(text: str, fallback_name: str, markdown: bool) -> tuple[str, str]:
    """Title from the first heading (or the filename), abstract from the first prose paragraph.

    Index from content, never the filename: the stem is only the last-resort title. A Markdown source with
    its own frontmatter contributes its title field and is scanned by body only. Non-Markdown files fall
    back to the first comment or docstring line.
    """
    title = ""
    para: list[str] = []
    if markdown:
        fm_meta, fm_body = parse_frontmatter(text)
        if fm_meta:
            title = str(fm_meta.get("title") or "")
            text = fm_body
        in_fence = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if s.startswith("#"):
                if not title:
                    title = s.lstrip("#").strip()
                if para:
                    break
                continue
            if s:
                para.append(s)
            elif para:
                break
    else:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#!"):
                continue
            s = s.lstrip("#/ ").strip().strip('"').strip("'").strip()
            if s:
                para.append(s)
                break
    if not title:
        stem = Path(fallback_name).stem
        words = re.sub(r"[-_]+", " ", stem).strip()
        title = (words[:1].upper() + words[1:]) if words else stem
    return _plain_voice(title), _plain_voice(" ".join(para))


def _candidate_score(text: str, relpath: str, last_commit: str | None) -> tuple[int, dict]:
    """A small transparent additive score; the parts are echoed in the report so ranking is auditable."""
    words = len(text.split())
    headings = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
    parts = {
        "substance": min(words // 100, 10),
        "structure": min(2 * headings, 10),
        "recency": 0,
        "depth": -max(len(Path(relpath).parts) - 2, 0),
        "boilerplate": -5 if Path(relpath).stem.lower() in _BOILERPLATE_STEMS else 0,
    }
    commit_date = _iso_date(last_commit)
    if commit_date:
        age_days = (datetime.now(timezone.utc) - commit_date).days
        parts["recency"] = 5 if age_days <= 180 else (2 if age_days <= 730 else 0)
    return sum(parts.values()), parts


def _candidate_abstract(raw: str, relpath: str, key: str) -> str:
    """A one-line abstract that is always a COMPLETE statement, never a cut one.

    An abstract is published prose that a reader takes as true. A budget-cut of the source paragraph is
    not a shorter version of what the source says, it is a different and often false statement: cutting
    immediately before a status line publishes a resolved item as open. So the budget selects whole
    sentences, and where not even one fits, a shorter true line is preferred to a longer broken one.

    A terminator is completed only when nothing was cut, since finishing an uncut line adds no claim.
    It is NEVER appended to a cut, which is what made the old defect invisible: the body ended in a full
    stop and read finished.
    """
    raw = str(raw or "").strip()
    pointer = f"Pointer to {relpath} in the {key} seed source."
    if not raw:
        abstract = pointer
    elif len(raw) <= _ABSTRACT_MAX_CHARS:
        # Nothing is cut, so completing the sentence claims no more than the source paragraph did.
        abstract = raw if raw.endswith((".", "!", "?")) else raw + "."
    else:
        abstract = _whole_sentences_within(raw, _ABSTRACT_MAX_CHARS)
        if not abstract:
            # No whole sentence fits. Keep the first one if it merely overruns; drop it if it is far over.
            first = next(iter(_split_sentences(raw)), "")
            abstract = first if 0 < len(first) <= _ABSTRACT_HARD_MAX_CHARS else pointer
    if _looks_truncated(abstract):  # fail safe: never publish a fragment, whatever produced it
        abstract = pointer
    if len(abstract) < TRIVIAL_BODY_CHARS:  # keep a fresh candidate out of the rot sweep's trivial flag
        abstract += " See the source document for the full detail."
    return abstract


def _scan_seed_source(key: str, spec: dict, root: Path, remote: str | None, sha: str | None,
                      captured: set) -> dict:
    """Walk one seed source and return its counts plus ranked candidates. Never opens a secret-named file."""
    include = _as_list(spec.get("include")) or ["**/*.md"]
    exclude = _as_list(spec.get("exclude"))
    counts = {"candidates": 0, "already_captured": 0,
              "skipped_secret": 0, "skipped_binary": 0, "skipped_glob": 0}
    candidates: list[dict] = []
    seen_ids: dict = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if _is_secret_name(path.name):  # by name, before any open()
            counts["skipped_secret"] += 1
            continue
        if not _glob_match(rel, include) or (exclude and _glob_match(rel, exclude)):
            counts["skipped_glob"] += 1
            continue
        if _looks_binary(path):
            counts["skipped_binary"] += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            counts["skipped_binary"] += 1
            continue
        cid = f"seed-{_slugify(key, 20)}-{_slugify(rel.rsplit('.', 1)[0] if '.' in path.name else rel)}"
        dup = seen_ids.get(cid, 0) + 1
        seen_ids[cid] = dup
        if dup > 1:
            cid = f"{cid}-{dup}"
        last_commit = None
        if sha:  # the root is a git repo, so ask for the file's last commit date
            r = _git(["log", "-1", "--format=%cs", "--", rel], cwd=str(root))
            if r.returncode == 0:
                last_commit = r.stdout.strip() or None
        score, parts = _candidate_score(text, rel, last_commit)
        title, raw_abstract = _extract_title_abstract(text, path.name, path.suffix.lower() == ".md")
        source = _seed_source_url(remote, sha, rel)
        cap = _norm_source(source) in captured
        counts["already_captured" if cap else "candidates"] += 1
        candidates.append({
            "id": cid, "title": title, "abstract": _candidate_abstract(raw_abstract, rel, key),
            "relpath": rel, "source": source, "score": score, "score_parts": parts, "captured": cap,
        })
    candidates.sort(key=lambda c: (-c["score"], c["relpath"]))
    return {"counts": counts, "candidates": candidates}


# --- pin audit: does a pointer's BODY still say what its pinned SOURCE says? -------------------------
#
# A pointer nugget pins `source:` to a blob at a fixed SHA. Moving that SHA forward without regenerating the
# body leaves the KB publishing a superseded statement while pointing at a source that contradicts it. That
# state is invisible to every other check here: the staleness sweep sees a current pin, the truncation guard
# sees complete prose, and `sync` sees a body hash matching the registry it was published from. It is only
# visible by reading the body against the source, which is what this does.

# A status a document asserts about ITSELF. Compared only with its opposite number on the other side; never
# used to rank "newness". Deliberately NOT date-based: a date in a body says when someone wrote a line, not
# whether the line is still true, and ranking the two sides by date gets the answer confidently wrong. That
# is not hypothetical, it is how a 2026-09-02 review misread a stored body that was BOTH later-dated and
# stale, and nearly reported a correct repair as damage.
_STATUS_CLOSED = frozenset("""
done sent finalised finalized complete completed resolved closed shipped landed merged issued superseded
moved retired archived decommissioned cancelled canceled approved signed executed live""".split())
_STATUS_OPEN = frozenset("""
draft drafting proposed pending open todo wip outstanding planned unissued unsent blocked waiting
provisional tentative""".split())
_STATUS_LINE = re.compile(r"\b(?:status|state)\b\s*:?\*{0,2}\s*(.{0,80})", re.I)


_STATUS_NEGATORS = frozenset("not never no nor without yet awaiting pending unless".split())


def _status_tokens(text: str) -> set:
    """Status words a text asserts about itself, taken from its status/state lines only.

    Scoped to a status line rather than the whole body on purpose: prose mentions "done" and "pending" in
    passing all the time, and a whole-body scan turns every such mention into a false alarm.

    Negated words are dropped, and that is a correctness requirement rather than tidiness. "not yet issued"
    is a claim that the thing is OPEN; counting `issued` from it would put a closed token on the open side
    and cancel a real finding, which is the expensive direction for this check. The phrase is not invented:
    it is what the one nugget that shipped this defect actually said.
    """
    found: set = set()
    for claim in _STATUS_LINE.findall(str(text or "")):
        words = [w.lower() for w in re.findall(r"[a-zA-Z]+", claim)]
        for i, low in enumerate(words):
            if low not in _STATUS_CLOSED and low not in _STATUS_OPEN:
                continue
            if any(w in _STATUS_NEGATORS for w in words[max(0, i - 3):i]):
                continue
            found.add(low)
    return found


def _seed_key_of(meta: dict) -> str:
    """The [seed_sources] key prescan used for this nugget, recovered from its tags.

    Only the pointer-line fallback embeds the key ("Pointer to X in the <key> seed source"), but getting it
    wrong makes every such abstract look changed when nothing has. prescan tags a nugget with its seed key
    in snake_case (shadow_it) alongside `prescan` and the audience label.
    """
    for tag in meta.get("tags") or []:
        if tag != "prescan" and "_" in tag:
            return tag.replace("_", "-")
    return str(meta.get("domain") or "seed")


def _body_claims_are_in_source(body: str, source_text: str) -> bool:
    """True when every distinctive term the body asserts also appears in the source document.

    The regeneration comparison asks "does this body reproduce from paragraph one", which a body someone
    has since IMPROVED never does. Eight such rows sat in the report permanently, every one verified by
    hand as true of its source, and a report that is always noisy is one nobody reads. This separates a
    body that has DRIFTED from its source from one that summarises MORE of the same document than the
    generator reads.

    Distinctive terms only: the backticked identifiers and bolded phrases a body commits to. Ordinary
    prose is not compared, because paraphrase is what a good summary does and demanding literal overlap
    would put every hand-written body straight back into the report.

    Conservative by construction. One absent term keeps the row flagged, because the expensive direction
    here is clearing a real drift, not carrying a false one. A body with no distinctive terms at all
    stays flagged too: there is nothing to check, which is not the same as having checked.
    """
    terms = [t for pair in re.findall(r"`([^`\n]{4,40})`|\*\*([^*\n]{6,40})\*\*", str(body or ""))
             for t in pair if t]
    if not terms:
        return False
    src = str(source_text or "")
    return all(t in src for t in dict.fromkeys(terms))


def audit_tree_row(meta: dict, tree_names: tuple | None) -> dict:
    """Classify a DIRECTORY pin by what the directory now holds. Pure; no network, no clock.

    A `/tree/<sha>/` pointer aims at a directory, so there is no single blob to diff and the audit used to
    call it sound and move on. That is the wrong conclusion: a directory pin goes stale exactly as a blob
    pin does, by its CONTENTS changing, and nothing else in this file could see it. Two such pins existed
    when this was written and neither had ever been checked by anything; one was serving a twelve-ADR view
    of a thirty-six-ADR set, so two thirds of the decisions were invisible to every reader of the KB.

    Membership, not count. Comparing lengths alone reports nothing when one file is added and another
    removed in the same window, which is the ordinary shape of a renumber.

    `tree_names` is (names_at_pin, names_on_head); None means the listing could not be read, which is
    reported as could-not-look rather than as agreement.
    """
    if not tree_names:
        return {"id": meta.get("id"), "verdict": "unfetchable",
                "detail": "directory listing could not be read at the pin or on the default branch"}
    pinned, head = (set(tree_names[0] or ()), set(tree_names[1] or ()))
    added, gone = sorted(head - pinned), sorted(pinned - head)
    if not added and not gone:
        return {"id": meta.get("id"), "verdict": "ok",
                "detail": f"directory membership unchanged, {len(pinned)} entries"}
    bits = [f"{len(pinned)} entries at the pin, {len(head)} on the default branch"]
    if added:
        bits.append(f"{len(added)} added: " + ", ".join(added[:4]) + (" ..." if len(added) > 4 else ""))
    if gone:
        bits.append(f"{len(gone)} removed: " + ", ".join(gone[:4]) + (" ..." if len(gone) > 4 else ""))
    return {"id": meta.get("id"), "verdict": "stale-tree", "detail": "; ".join(bits)}


def audit_pin_row(meta: dict, body: str, source_text: str | None,
                  tree_names: tuple | None = None) -> dict:
    """Classify one pointer nugget against the text of its own pinned source. Pure; no network, no clock.

    Verdicts:
      stale-status the body claims the work is still open while the source says it is closed. The severe
                  one: the KB is publishing a falsehood, not merely an out-of-date phrasing. Checked for
                  EVERY pointer, hand-written or generated, because it is the failure that misleads readers.
      ok          nothing to report. For a generated body that means it still reproduces from this source;
                  for a hand-written one it means only that no status contradiction was found.
      diverged    a GENERATED body no longer reproduces from its source, with no status signal either way.
                  Not a defect on its own: a reworded source does this too. It means "a human has to read
                  this one", never "regenerate this one".
      unfetchable the pinned blob could not be read, so nothing is claimed about it.

    The regeneration comparison runs ONLY on bodies prescan generated (tag `prescan`). A hand-written body
    never reproduces from a generator, so comparing it that way reports drift on every run forever, and a
    report that is always noisy is one nobody reads. Hand-written bodies get the status check alone.

    An `ok` verdict means nothing contradicted the source. It does NOT mean the body is true, and it cannot:
    the source itself may be wrong.
    """
    parts = parse_source_url(meta.get("source", ""))
    if parts and not parts["pinned"]:
        # Real finding, and it outranks whether the fetch worked: a moving ref follows the file forward, so
        # the body can never be audited for drift against it and the pointer is not actually a pin.
        return {"id": meta.get("id"), "verdict": "unpinned-ref",
                "detail": f"source points at ref {parts['ref']!r}, not a fixed SHA"}
    if parts and parts["kind"] == "tree":
        return audit_tree_row(meta, tree_names)
    if parts is None and str(meta.get("source", "")).startswith("http"):
        return {"id": meta.get("id"), "verdict": "external-pointer",
                "detail": "points outside GitHub; not auditable here and not a defect"}
    if source_text is None:
        return {"id": meta.get("id"), "verdict": "unfetchable", "detail": "pinned blob could not be read"}
    stored = str(body or "").strip()
    stored_status, source_status = _status_tokens(stored), _status_tokens(source_text)
    closed_now = (source_status & _STATUS_CLOSED) - stored_status
    open_still = (stored_status & _STATUS_OPEN) - source_status
    if closed_now and open_still:
        return {"id": meta.get("id"), "verdict": "stale-status",
                "detail": f"body says {'/'.join(sorted(open_still))}; source says "
                          f"{'/'.join(sorted(closed_now))}"}
    if "prescan" not in (meta.get("tags") or []):
        return {"id": meta.get("id"), "verdict": "ok", "detail": "hand-written body; status-checked only"}
    # Repo-relative path from the parsed URL, fragment already stripped. Not from _norm_source, whose
    # "github:owner/repo/path" form keeps the repo segment; prescan builds the abstract from the
    # repo-relative path, so borrowing the normalised form puts an extra segment into every pointer line.
    relpath = urllib.parse.unquote(parts["path"]) if parts else str(meta.get("source", ""))
    is_md = relpath.lower().endswith(".md")
    _title, raw = _extract_title_abstract(source_text, relpath.rsplit("/", 1)[-1], is_md)
    regenerated = _candidate_abstract(raw, relpath, _seed_key_of(meta))
    if stored == regenerated.strip():
        return {"id": meta.get("id"), "verdict": "ok", "detail": ""}
    if _body_claims_are_in_source(body, source_text):
        return {"id": meta.get("id"), "verdict": "enriched",
                "detail": "body says more than the generator reads, and every claim it makes is in the source"}
    return {"id": meta.get("id"), "verdict": "diverged", "detail": "body is not what this source yields; "
                                                                  "needs a human read, not a regeneration"}


def parse_source_url(source_url: str) -> dict | None:
    """Split a GitHub blob/tree URL into its parts, or None when it is not one.

    The `#fragment` is stripped from the path and kept separately. A pointer may legitimately aim at one
    section of a file, and a fragment is not part of the path: leaving it on asks the API for a file whose
    name ends "...md#8a-section-name-with--a-double-hyphen", which 404s and reads as a dead pin. That is
    not hypothetical, it reported two sound pointers as broken provenance.
    """
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/(blob|tree)/([^/]+)/(.+)$", str(source_url or ""))
    if not m:
        return None
    owner, repo, kind, ref, path = m.groups()
    path, _, fragment = path.partition("#")
    return {"owner": owner, "repo": repo, "kind": kind, "ref": ref, "path": path,
            "fragment": fragment, "pinned": bool(re.fullmatch(r"[0-9a-f]{7,40}", ref))}


def _fetch_pinned_source(source_url: str, token: str | None) -> str | None:
    """Read a GitHub blob URL. Replaced wholesale by the tests; the only network in the pin audit."""
    parts = parse_source_url(source_url)
    if not parts or parts["kind"] != "blob" or not parts["path"]:
        return None
    owner, repo, ref, path = parts["owner"], parts["repo"], parts["ref"], parts["path"]
    url = (f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
           f"?ref={urllib.parse.quote(ref)}")
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "kb.py-pin-audit"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def _fetch_tree_names(source_url: str, token: str | None) -> tuple | None:
    """List a pinned directory's entries at the pin and on the default branch. Network; replaced by tests.

    Two calls, deliberately: the pin tells you what the pointer promised and the default branch tells you
    what a reader would find today, and the audit is the difference. Asking only one side answers nothing.

    Returns None when EITHER side is unreadable, so a permission or rate-limit failure is reported as
    could-not-look. Returning a partial pair would let a failed fetch masquerade as an emptied directory,
    which is the loudest possible false positive.
    """
    parts = parse_source_url(source_url)
    if not parts or parts["kind"] != "tree" or not parts["path"]:
        return None
    owner, repo, path = parts["owner"], parts["repo"], parts["path"]
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "kb.py-pin-audit"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def listing(ref: str):
        url = (f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
               f"?ref={urllib.parse.quote(ref)}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            return None
        if not isinstance(payload, list):  # a file, not a directory
            return None
        return sorted(e.get("name", "") for e in payload if e.get("type") == "file")

    at_pin = listing(parts["ref"])
    on_head = listing(_default_branch(owner, repo, headers))
    if at_pin is None or on_head is None:
        return None
    return (at_pin, on_head)


def _default_branch(owner: str, repo: str, headers: dict) -> str:
    """The repo's default branch, asked rather than assumed; not every repo here is on `main`."""
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")).get("default_branch") or "main"
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return "main"


def cmd_pin_audit(args) -> int:
    """Report pointer nuggets whose body no longer matches the source they pin. Report-only; never writes.

    Run it after any re-pin. The `kacific-kb` contract already asks for the body to be diffed against the
    file at the new SHA; skipping that step is what publishes a resolved item as open, so this makes the
    step runnable instead of remembered.
    """
    root = Path(args.repo).expanduser().resolve()
    if not root.is_dir():
        # A typo'd path otherwise walks nothing and reports a clean run, so "could not look" comes back
        # dressed as "nothing wrong". Exit 2 is this tool's could-not-proceed code, and the distinction
        # matters most for an audit, whose whole output is an absence of findings.
        print(f"pin-audit: no such repo directory: {root}", file=sys.stderr)
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    nuggets = _load_nuggets(root)
    rows = []
    for n in nuggets:
        meta = n["meta"]
        if meta.get("provenance_type") != "reference" or not str(meta.get("source", "")).startswith("http"):
            continue
        parts = parse_source_url(meta["source"])
        if parts and parts["kind"] == "tree" and parts["pinned"]:
            rows.append(audit_pin_row(meta, n["body"], None,
                                      tree_names=_fetch_tree_names(meta["source"], token)))
        else:
            rows.append(audit_pin_row(meta, n["body"], _fetch_pinned_source(meta["source"], token)))
    verdicts = ("ok", "stale-status", "stale-tree", "diverged", "enriched", "unpinned-ref",
                "directory-pointer",
                "external-pointer", "unfetchable")
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in verdicts}
    # The report states its own scope, because the reader cannot see it in the findings. This command takes
    # one --repo and the KB is spread over several audience repos, so a clean run here says nothing about the
    # others. That is not a hypothetical: three pointers on a moving ref sat unnoticed for a day behind a
    # clean Technical result, simply because nobody had run it against the AllStaff clone.
    scope = {"repo": root.name, "audience": root.name.rsplit("-", 1)[-1], "path": str(root),
             "nuggets_scanned": len(nuggets), "pointers_audited": len(rows), "covers": "this clone only"}
    if args.json:
        print(json.dumps({"scope": scope, "counts": counts, "rows": rows}, indent=2))
    else:
        print(f"pin-audit scope: {scope['repo']} (audience {scope['audience']}) at {scope['path']}")
        print(f"  {scope['nuggets_scanned']} nuggets scanned, {scope['pointers_audited']} of them pointers.")
        print("  THIS CLONE ONLY. Sibling audience repos are not covered; run it once per repo.")
        for verdict, label in (("stale-status", "SEVERE: body publishes an open status its source has closed"),
                               ("stale-tree", "SEVERE: a pinned DIRECTORY has gained or lost files since the pin"),
                               ("unpinned-ref", "source is not pinned to a fixed SHA, so drift is unauditable"),
                               ("diverged", "needs a human read: the body does not follow from this source"),
                               ("enriched", "hand-improved and consistent with its source; no action"),
                               ("unfetchable", "pinned blob unreadable; nothing claimed"),
                               ("directory-pointer", "sound pin, no body to compare"),
                               ("external-pointer", "outside GitHub, not auditable here")):
            hits = [r for r in rows if r["verdict"] == verdict]
            if not hits:
                continue
            print(f"\n{label}: {len(hits)}")
            for r in hits:
                print(f"  {r['id']}" + (f"\n      {r['detail']}" if r["detail"] else ""))
        # Separate "checked and matched" from "could not be checked". Reporting only the ok count made two
        # perfectly sound external pointers read as nought out of two matching, which is the same
        # misreadable-summary problem this scope line exists to fix.
        unchecked = counts["external-pointer"] + counts["unfetchable"]
        print(f"\npin-audit: {len(rows)} pointer nuggets in {scope['repo']}; {counts['ok']} match their "
              f"source, {unchecked} could not be checked here.")
        print("An 'ok' means the body matches the source it points at, never that the body is true,")
        print(f"and this result covers {scope['repo']} alone. A clean run here is not a clean KB.")
    return 0


def cmd_verify_audit(args) -> int:
    """Report nuggets asserting a `verified` date that names nobody. Report-only; never writes.

    `verified` is defined as *last human verification*, but it stores only a date. A date written after a
    person read the body and a date written by a process that read nothing are byte-identical, so the
    unearned one is invisible for ever. The hygiene sweep cannot help: `rot` flags a date for being too
    OLD, and nothing anywhere flags one for being unearned. That asymmetry is one-directional by
    construction, and it is why an over-claim is the expensive error: an under-claimed date gets the
    nugget flagged and re-read, while an over-claimed one is simply believed.

    `verified_by` closes it by recording WHO. This command reports the gap rather than refusing it,
    because every nugget predates the field; enforcement moves into `validate_entry` once the
    unattributed count is worked down (see `schema/kb-entry.md`).
    """
    root = Path(args.repo).expanduser().resolve()
    if not root.is_dir():
        print(f"verify-audit: no such repo directory: {root}", file=sys.stderr)
        return 2
    nuggets = _load_nuggets(root)
    rows = []
    for n in nuggets:
        meta = n["meta"]
        # `null`, `~` and an empty value all parse to Python None, and `str(None)` is the string "None",
        # which is neither a date nor the literal "unverified". Normalise BEFORE testing. Without this,
        # `verified_by: null` reads as attributed to someone called None, which is a false clear in the
        # one direction this command exists to close, and `verified: null` reads as a claim that was
        # never made. Both were live until an adversarial review found them.
        raw = "" if meta.get("verified") is None else str(meta["verified"]).strip()
        who = "" if meta.get("verified_by") is None else str(meta["verified_by"]).strip()
        # Print the readable twin where there is one, via the shared `_person` helper so the identity
        # format has one home rather than a copy per surface.
        who_name = "" if meta.get("verified_by_name") is None else str(meta["verified_by_name"]).strip()
        shown = _person(who, who_name)
        if raw in ("", "unverified"):
            verdict, detail = "unverified", "claims no verification, so nothing to attribute"
        elif _iso_date(raw) is None:
            # Present but not a date, so it asserts something no reader can check. Reported on its own
            # rather than folded into "unverified", which would quietly clear a typo'd claim.
            verdict, detail = "unparseable", f"verified is {raw!r}, which is not an ISO-8601 date"
        elif who:
            verdict, detail = "attributed", f"verified {raw} by {shown}"
        else:
            verdict, detail = "unattributed", f"claims verification on {raw} but names nobody"
        rows.append({"id": meta.get("id"), "verdict": verdict, "verified": raw or None,
                     "verified_by": who or None, "verified_by_name": who_name or None,
                     "detail": detail})

    counts = {v: sum(1 for r in rows if r["verdict"] == v)
              for v in ("attributed", "unattributed", "unparseable", "unverified")}
    # Same scope discipline as pin-audit: this takes one --repo and the KB spans several audience repos,
    # so a clean run here says nothing about the others.
    scope = {"repo": root.name, "path": str(root), "nuggets_scanned": len(nuggets),
             "covers": "this clone only"}
    if args.json:
        print(json.dumps({"scope": scope, "counts": counts, "rows": rows}, indent=2))
        return 0

    print(f"verify-audit scope: {scope['repo']} at {scope['path']}")
    print(f"  {scope['nuggets_scanned']} nuggets scanned. THIS CLONE ONLY; run it once per audience repo.")
    for verdict, label in (("unattributed", "asserts a verification nobody is named for"),
                           ("unparseable", "verified is set to something that is not a date")):
        hits = [r for r in rows if r["verdict"] == verdict]
        if not hits:
            continue
        print(f"\n{label}: {len(hits)}")
        for r in hits:
            print(f"  {r['id']}\n      {r['detail']}")
    print(f"\nverify-audit: {counts['attributed']} attributed, {counts['unattributed']} unattributed, "
          f"{counts['unparseable']} unparseable, {counts['unverified']} claim no verification.")
    print("An unattributed date is not evidence of anything: it cannot distinguish a person who read the")
    print("body from a process that stamped it. Set `verified_by` when you bump `verified`.")
    return 0


def cmd_prescan(args) -> int:
    """Scan the manifest's [seed_sources] into ranked pointer candidates plus a captured-vs-gap report.

    Report-only by default (zero writes beyond the tool-owned clone cache for url sources). With --commit
    it stages the non-captured candidates under <manifest dir>/prescan-out/<key>/ and writes
    prescan-report.json next to the manifest; --owner-gid/--owner-name (the reviewing human, who owns the
    draft candidates) are required for --commit. Keepers land via the normal `store --into` gate.
    """
    if args.commit and not (args.owner_gid and args.owner_name):
        print("prescan --commit: --owner-gid and --owner-name are required "
              "(the reviewing human owns the staged candidates).", file=sys.stderr)
        return 2
    config = load_config(args.config)
    manifest = load_manifest(config)
    seeds = manifest["seed_sources"]
    if args.source:
        seeds = {k: v for k, v in seeds.items() if k == args.source}
        if not seeds:
            print(f"prescan: no seed source named '{args.source}' in the manifest.", file=sys.stderr)
            return 2
    if not seeds:
        print("prescan: [seed_sources] is empty in the manifest; nothing to scan.")
        return 0

    # Dedup baseline: what the KB already points at, from the recorded aggregate registry.
    agg_path = (Path(args.aggregate).expanduser() if args.aggregate
                else manifest["manifest_path"].parent / "registry-aggregate.json")
    captured: set = set()
    if agg_path.exists():
        try:
            agg = json.loads(agg_path.read_text(encoding="utf-8"))
            captured = {_norm_source(e["source_document"])
                        for e in agg.get("entries", []) if e.get("source_document")}
        except (json.JSONDecodeError, OSError):
            print(f"prescan: WARN: unreadable aggregate at {agg_path}; dedup disabled.", file=sys.stderr)
    else:
        print(f"prescan: note: no aggregate at {agg_path}; dedup against existing nuggets disabled.")

    ttl = args.max_age if args.max_age is not None else int(config.get("cache", {}).get("fetch_ttl_seconds", 0))
    results: dict = {}
    for key, spec in sorted(seeds.items()):
        kind = spec.get("kind", "pointer")
        if kind != "pointer":
            results[key] = {"status": f"skipped (unsupported kind: {kind})"}
            continue
        if spec.get("path"):
            root = Path(spec["path"]).expanduser()
            if not root.is_dir():
                results[key] = {"status": "unreachable (path not found)"}
                continue
            remote_r = _git(["remote", "get-url", "origin"], cwd=str(root))
            remote = remote_r.stdout.strip() if remote_r.returncode == 0 else None
            sha_r = _git(["rev-parse", "HEAD"], cwd=str(root))
            sha = sha_r.stdout.strip() if sha_r.returncode == 0 else None
        elif spec.get("url"):
            remote = spec["url"]
            root = manifest["cache_root"] / "seeds" / key
            sha, status = _refresh_clone(remote, root, cache_root=manifest["cache_root"],
                                         ttl_seconds=ttl, force=args.force)
            if status != "ok":
                results[key] = {"status": status}
                continue
        else:
            results[key] = {"status": "skipped (no url or path)"}
            continue
        scan = _scan_seed_source(key, spec, root, remote, sha, captured)
        results[key] = {
            "status": "ok", "head_sha": sha, "audience": spec.get("audience"),
            "domain": spec.get("domain", "shared"),
            "covered_at_repo_level": bool(remote) and _norm_source(remote) in captured,
            **scan,
        }

    mode = "commit (staging candidates)" if args.commit else "report-only (add --commit to stage candidates)"
    print(f"prescan: {len(results)} seed source(s); mode: {mode}")
    for key in sorted(results):
        r = results[key]
        label = f" (audience: {r['audience']})" if r.get("audience") else ""
        print(f"\nsource: {key}{label}")
        if r.get("status") != "ok":
            print(f"  {r['status']}")
            continue
        if r.get("covered_at_repo_level"):
            print("  note: the KB already holds a repo-level pointer to this source.")
        emitted = [c for c in r["candidates"] if not c["captured"]]
        for cand in emitted[: args.top]:
            parts = " ".join(f"{k}={v:+d}" for k, v in sorted(cand["score_parts"].items()) if v)
            print(f"  [{cand['score']:>3}] {cand['relpath']}  ({parts or 'score 0'})")
        if len(emitted) > args.top:
            print(f"  ... and {len(emitted) - args.top} more (full list in prescan-report.json with --commit)")
        c = r["counts"]
        print(f"  counts: candidates={c['candidates']} already-captured={c['already_captured']} "
              f"skipped-secret={c['skipped_secret']} skipped-binary={c['skipped_binary']} "
              f"skipped-glob={c['skipped_glob']}")

    if not args.commit:
        return 0

    out_root = manifest["manifest_path"].parent / "prescan-out"
    written = invalid = 0
    for key in sorted(results):
        r = results[key]
        if r.get("status") != "ok":
            continue
        sub_dir = out_root / key
        if sub_dir.exists():  # idempotent per source: a re-run regenerates its staging subdir
            shutil.rmtree(sub_dir)
        sub_dir.mkdir(parents=True, exist_ok=True)
        for cand in r["candidates"]:
            if cand["captured"]:
                continue
            meta = {
                "schema_version": SCHEMA_VERSION, "id": cand["id"], "title": cand["title"],
                "domain": r["domain"], "type": "reference", "status": "draft",
                "owner_gid": args.owner_gid, "owner_name": args.owner_name,
                "provenance_type": "reference", "source": cand["source"],
                "confidence": "medium", "verified": "unverified",
                "tags": ["prescan", key] + ([r["audience"]] if r.get("audience") else []),
            }
            text = emit_frontmatter(meta, cand["abstract"] + "\n")
            errors = validate_entry(*parse_frontmatter(text))
            if errors:  # belt and braces: a candidate that fails the store gate is reported, never staged
                invalid += 1
                print(f"  WARN: candidate {cand['id']} failed validation "
                      f"({'; '.join(errors)}); not staged.", file=sys.stderr)
                continue
            (sub_dir / f"{cand['id']}.md").write_text(text, encoding="utf-8")
            written += 1
    report_path = manifest["manifest_path"].parent / "prescan-report.json"
    report = {"schema_version": SCHEMA_VERSION, "generated_utc": _now_iso(), "sources": results}
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nOK: staged {written} candidate(s) under {out_root}"
          + (f" ({invalid} failed validation, not staged)" if invalid else "") + ".")
    print(f"OK: report written to {report_path}")
    print("Next: review the staged candidates, then land keepers via `kb.py store <file> --into <repo>`.")
    return 0


# --- export: neutral, target-agnostic bundle (Markdown/HTML) ----------------
# A bundle is a self-contained directory: rendered docs under docs/, plus a machine-readable bundle.json
# manifest that any target's importer (SharePoint / Confluence / Box) maps onto its own structure. Manifest
# mode produces one bundle per audience, assembled from derive_slices, so a lower-clearance bundle can never
# carry a higher department's doc. Live API push to a specific target is a later step (see the adapter
# contract doc); this renders the neutral artefact those adapters consume.

def _reader_notes(meta: dict, now: datetime) -> list[str]:
    """Reader-facing caveats for an exported doc, mirroring the answer contract's footnotes.

    A staleness note when verified is missing or older than READER_STALE_DAYS (90), and a note when the
    status is anything other than published, so exported prose stays as honest as a served answer.
    """
    notes = []
    verified = _iso_date(meta.get("verified"))
    if verified is None or (now - verified).days > READER_STALE_DAYS:
        notes.append("Note: this document has not been audited recently.")
    if meta.get("status") and meta["status"] != "published":
        notes.append(f"Note: this document's status is '{meta['status']}', not published.")
    return notes


def _doc_provenance(meta: dict) -> str:
    """The inline provenance line for an exported doc: a source, or a named attestation."""
    if meta.get("provenance_type") == "attestation" or (not meta.get("source") and meta.get("attested_by")):
        who = _person(meta.get("attested_by"), meta.get("attested_by_name")) or "unknown"
        on = meta.get("attested_on")
        return f"Attested by: {who}" + (f" ({on})" if on else "")
    return f"Source: {meta.get('source') or meta.get('id')}"


def _render_doc_markdown(nugget: dict, now: datetime) -> str:
    """Render one nugget as a neutral, reader-facing Markdown doc: title, provenance, body, caveats.

    Frontmatter metadata rides in bundle.json, not the doc, so the doc stays clean prose any target renders
    as-is. The wrapper text is plain (no em-dash, no markdown emphasis) so it survives the minimal HTML
    converter unchanged and passes the voice gate.
    """
    m = nugget["meta"]
    lines = [f"# {m.get('title') or m.get('id')}", ""]
    lines.append(_doc_provenance(m))
    lines.append(f"Verified: {m.get('verified') or 'unverified'}")
    lines.append("")
    lines.append(nugget["body"].strip())
    notes = _reader_notes(m, now)
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines).rstrip() + "\n"


def _md_inline_code(escaped: str) -> str:
    """Wrap backtick code spans in <code> within an already-HTML-escaped line (backticks survive escaping)."""
    parts = escaped.split("`")
    if len(parts) < 3:
        return escaped
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


def _markdown_to_html(md: str) -> str:
    """A deliberately minimal, dependency-free Markdown to HTML block converter.

    Handles ATX headings, blank-line paragraphs, unordered (-/*) and ordered (1.) lists, fenced ``` code
    blocks, and backtick code spans. Every line is HTML-escaped first, so a nugget body can never inject live
    markup. Inline bold/italic are left as literal characters (a documented limitation) to avoid fragile
    regex over human prose; a target needing richer inline formatting converts from the Markdown bundle.
    """
    out: list[str] = []
    para: list[str] = []
    code_lines: list[str] = []
    in_code = False
    list_type = None  # "ul" or "ol", else None

    def flush_para():
        if para:
            out.append("<p>" + "<br>\n".join(para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    for raw in md.split("\n"):
        if raw.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + "\n".join(code_lines) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                flush_para(); flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(html.escape(raw))
            continue

        stripped = raw.strip()
        if not stripped:
            flush_para(); flush_list()
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        ul = re.match(r"^[-*]\s+(.*)$", stripped)
        ol = re.match(r"^\d+\.\s+(.*)$", stripped)
        if heading:
            flush_para(); flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_md_inline_code(html.escape(heading.group(2)))}</h{level}>")
        elif ul or ol:
            flush_para()
            want = "ul" if ul else "ol"
            if list_type != want:
                flush_list()
                out.append(f"<{want}>")
                list_type = want
            out.append(f"<li>{_md_inline_code(html.escape((ul or ol).group(1)))}</li>")
        else:
            flush_list()
            para.append(_md_inline_code(html.escape(stripped)))

    if in_code:  # unterminated fence: emit what we captured rather than drop it
        out.append("<pre><code>" + "\n".join(code_lines) + "</code></pre>")
    flush_para(); flush_list()
    return "\n".join(out) + "\n"


def _render_doc_html(nugget: dict, now: datetime) -> str:
    """Render one nugget as a minimal, self-contained HTML page for a target that ingests HTML directly."""
    m = nugget["meta"]
    title = html.escape(str(m.get("title") or m.get("id")))
    inner = _markdown_to_html(_render_doc_markdown(nugget, now))
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{title}</title>\n</head>\n<body>\n<article>\n{inner}</article>\n</body>\n</html>\n"
    )


def _bundle_manifest(audience: str, generated_utc: str, docs_meta: list, fmt: str) -> dict:
    """The neutral, machine-readable manifest a target adapter maps onto its own structure."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "audience": audience,
        "target_neutral": True,
        "format": fmt,
        "docs": docs_meta,
    }


def _assemble_bundle(audience: str, entries: list, id_to_nugget: dict, now: datetime, fmt: str) -> tuple:
    """Assemble one audience bundle from its registry entries and the loaded nugget bodies (pure, no I/O).

    Each entry is mapped to its nugget body by id and rendered; an entry with no loadable body is skipped
    defensively (it should not occur in a coherent aggregate). Returns (docs_meta, files) where docs_meta is
    the per-doc manifest metadata (registry fields + the audience acl label + relative path) and files maps a
    relative path to the rendered content. Leak-safety is upstream in derive_slices; this only renders what it
    is given.
    """
    ext = "html" if fmt == "html" else "md"
    docs_meta: list = []
    files: dict = {}
    for e in sorted(entries, key=lambda e: e.get("id") or ""):
        nugget = id_to_nugget.get(e.get("id"))
        if nugget is None:
            continue
        rel = f"docs/{_slugify(e['id'])}.{ext}"
        files[rel] = _render_doc_html(nugget, now) if fmt == "html" else _render_doc_markdown(nugget, now)
        meta = dict(e)
        meta["acl"] = audience
        meta["path"] = rel
        docs_meta.append(meta)
    return docs_meta, files


def _write_bundle(out_dir: Path, manifest: dict, files: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (out_dir / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def cmd_export(args) -> int:
    fmt = getattr(args, "format", "markdown")
    if not args.out:
        print("export: --out <dir> is required (a bundle is a directory).", file=sys.stderr)
        return 2
    out_root = Path(args.out).expanduser()
    now = datetime.now(timezone.utc)
    generated = _now_iso()

    if getattr(args, "manifest", False):
        # ACL-aware: one bundle per audience, assembled from the aggregate's per-audience slices. Cloning
        # private repos needs the same GH_TOKEN + GIT_CONFIG credential recipe as `index --manifest`; this is
        # read-only (no push).
        config = load_config(args.config)
        manifest = load_manifest(config)
        ttl = args.max_age if args.max_age is not None else int(config.get("cache", {}).get("fetch_ttl_seconds", 0))
        agg = build_aggregate(manifest, ttl_seconds=ttl, force=args.force)
        slices = derive_slices(agg, manifest["audiences"])
        # Load each source repo's nuggets once, keyed by id, so a shared base entry that appears in several
        # slices renders from a single loaded body.
        id_to_nugget: dict = {}
        cache_dir = manifest["cache_dir"]
        for key, repo in agg["repos"].items():
            if repo.get("status") != "ok":
                continue
            for n in _load_nuggets(cache_dir / key):
                id_to_nugget[n["meta"]["id"]] = n
        written = []
        for key, entries in sorted(slices.items()):
            audience = (agg["repos"].get(key) or {}).get("audience") or key
            docs_meta, files = _assemble_bundle(audience, entries, id_to_nugget, now, fmt)
            _write_bundle(out_root / key, _bundle_manifest(audience, generated, docs_meta, fmt), files)
            written.append((key, audience, len(docs_meta)))
        print(f"OK: {len(written)} audience bundles written to {out_root} (format: {fmt}).")
        for key, audience, count in written:
            print(f"  {key} ({audience}): {count} docs")
        for key, r in sorted(agg["repos"].items()):
            if r["status"] != "ok":
                print(f"  WARN: repo '{key}': {r['status']}", file=sys.stderr)
        return 0

    if not args.repo:
        print("export: give a repo path, or --manifest for ACL-aware per-audience bundles.", file=sys.stderr)
        return 2
    root = Path(args.repo)
    nuggets = _load_nuggets(root)
    id_to_nugget = {n["meta"]["id"]: n for n in nuggets}
    entries = [_registry_entry(n) for n in nuggets]
    # The audience label of a self-only repo export is the last hyphen segment of its dir name
    # (Kacific-LLM-KB-Info-AllStaff -> AllStaff), matching the index --repo mirror convention.
    audience = root.resolve().name.rsplit("-", 1)[-1]
    docs_meta, files = _assemble_bundle(audience, entries, id_to_nugget, now, fmt)
    _write_bundle(out_root, _bundle_manifest(audience, generated, docs_meta, fmt), files)
    print(f"OK: bundle written to {out_root} ({len(docs_meta)} docs, audience {audience}, format {fmt}).")
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
                    help="build the cross-repo aggregate from config [repos].manifest, then derive each "
                         "repo's audience slice into the control home (add --publish to commit the slices)")
    sp.add_argument("--config", default="config.toml", help="path to config.toml (used with --manifest)")
    sp.add_argument("--out",
                    help="output path; default stdout (single repo) or <manifest dir>/registry-aggregate.json")
    sp.add_argument("--max-age", type=int, default=None, metavar="SECONDS",
                    help="reuse a managed repo's clone without re-fetching if its last fetch is within this "
                         "window (default: config [cache].fetch_ttl_seconds, else 0 = always fetch)")
    sp.add_argument("--force", action="store_true", help="ignore the fetch cache and re-fetch every repo")
    sp.add_argument("--publish", action="store_true",
                    help="with --manifest, commit+push each changed audience slice into its data repo "
                         "(a dev-Mac write step; needs write access, so the read-only cron never passes it)")
    sp.add_argument("--log-file",
                    help="single-repo index only: interaction log to stamp each entry's derived last_used "
                         "(omit to leave last_used null)")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("answer", help="answer a query from stored nuggets")
    sp.add_argument("query")
    sp.add_argument("--repo", default=".", help="KB repo root to search (default: current dir)")
    sp.add_argument("--format", choices=["human", "json"], default="human")
    sp.add_argument("--no-log", action="store_true", help="do not append to the interaction log")
    sp.set_defaults(func=cmd_answer)

    sp = sub.add_parser("rot", help="hygiene sweep")
    sp.add_argument("--repo", default=".", help="KB repo root to sweep (default: current dir)")
    sp.add_argument("--log-file",
                    help="interaction log to read usage from; a nugget cited by a recent hit answer is held "
                         "back from the Outdated flag until the hard ceiling (omit to disable usage softening)")
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
    sp.add_argument("--backfill-notes", action="store_true",
                    help="heal existing owned KB tasks' notes to the complete Background/Evidence/Expected "
                         "action/How-it-clears body (idempotent). Reads the tracking PAT even without --commit "
                         "to preview the count; add --commit to apply the writes")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("coordination-audit",
                        help="close opt-in coordination tasks whose tracked KB findings have all cleared")
    sp.add_argument("--config", default="config.toml", help="path to config.toml")
    sp.add_argument("--commit", action="store_true",
                    help="close the tasks in Asana; default is a read-only dry-run that prints what it "
                         "would close")
    sp.set_defaults(func=cmd_coordination_audit)

    sp = sub.add_parser("pin-audit",
                        help="report pointer nuggets whose body no longer matches their pinned source")
    sp.add_argument("--repo", default=".", help="KB repo root to audit (default: current dir)")
    sp.add_argument("--json", action="store_true", help="emit the full report as JSON")
    sp.set_defaults(func=cmd_pin_audit)

    sp = sub.add_parser("verify-audit",
                        help="report nuggets asserting a verified date that names no verifier")
    sp.add_argument("--repo", default=".", help="KB repo root to audit (default: current dir)")
    sp.add_argument("--json", action="store_true", help="emit the full report as JSON")
    sp.set_defaults(func=cmd_verify_audit)

    sp = sub.add_parser("prescan",
                        help="scan the manifest's seed sources into ranked pointer candidates (secrets-safe)")
    sp.add_argument("--config", default="config.toml", help="path to config.toml")
    sp.add_argument("--source", help="limit the scan to one seed source key from the manifest")
    sp.add_argument("--aggregate",
                    help="registry-aggregate.json to dedup against (default: <manifest dir>/registry-aggregate.json)")
    sp.add_argument("--owner-gid", help="owner for staged candidates (required with --commit)")
    sp.add_argument("--owner-name", help="owner display name for staged candidates (required with --commit)")
    sp.add_argument("--top", type=int, default=20, help="ranked candidates to print per source (default 20)")
    sp.add_argument("--max-age", type=int, default=None, metavar="SECONDS",
                    help="reuse a url seed's clone without re-fetching within this window "
                         "(default: config [cache].fetch_ttl_seconds, else 0 = always fetch)")
    sp.add_argument("--force", action="store_true", help="ignore the fetch cache and re-fetch url seeds")
    sp.add_argument("--commit", action="store_true",
                    help="stage candidates under <manifest dir>/prescan-out/ and write prescan-report.json; "
                         "default is report-only")
    sp.set_defaults(func=cmd_prescan)

    sp = sub.add_parser("export",
                        help="render a neutral bundle (docs + bundle.json) for an export target; "
                             "--manifest for ACL-aware per-audience bundles")
    sp.add_argument("repo", nargs="?", help="single KB repo root to export; omit when using --manifest")
    sp.add_argument("--manifest", action="store_true",
                    help="build ACL-aware per-audience bundles from config [repos].manifest (one leak-safe "
                         "bundle per managed repo's audience slice); needs read access to the private repos")
    sp.add_argument("--config", default="config.toml", help="path to config.toml (used with --manifest)")
    sp.add_argument("--out", help="output directory for the bundle (required)")
    sp.add_argument("--format", choices=["markdown", "html"], default="markdown",
                    help="doc render format (default: markdown; html emits a minimal self-contained page)")
    sp.add_argument("--max-age", type=int, default=None, metavar="SECONDS",
                    help="reuse a managed repo's clone without re-fetching if its last fetch is within this "
                         "window (default: config [cache].fetch_ttl_seconds, else 0 = always fetch)")
    sp.add_argument("--force", action="store_true", help="ignore the fetch cache and re-fetch every repo")
    sp.set_defaults(func=cmd_export)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
