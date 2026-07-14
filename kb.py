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
MISS_RESPONSE = "I cannot find this information in the current knowledge base."


# --- config -----------------------------------------------------------------

def load_config(path: str = "config.toml") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            raise SystemExit(
                "config.toml needs Python 3.11+ (stdlib tomllib) or the tomli backport; "
                "run kb.py with a newer interpreter, e.g. /opt/homebrew/bin/python3"
            )
    with p.open("rb") as fh:
        return tomllib.load(fh)


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
    print(f"OK: nugget '{meta['id']}' passes the schema and the anti-hallucination gate.")
    # TODO(1a): write into the correct audience repo by domain/audience, then trigger index.
    return 0


def cmd_index(args) -> int:
    root = Path(args.repo)
    entries = []
    for md in sorted(root.rglob("*.md")):
        if md.name in {"README.md", "registry.md"} or "sources" in md.parts:
            continue
        meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
        if not meta.get("id"):
            continue  # not a nugget (index from content, never from filename)
        entries.append({
            "id": meta["id"],
            "title": meta.get("title", ""),
            "domain": meta.get("domain"),
            "type": meta.get("type"),
            "status": meta.get("status"),
            "owner_gid": meta.get("owner_gid"),
            "source_document": meta.get("source"),
            "confidence_score": meta.get("confidence"),
            "last_verified": meta.get("verified"),
            "path": str(md.relative_to(root)),
            "content_hash": body_hash(body),
        })
    out = {"schema_version": SCHEMA_VERSION, "generated_utc": _now_iso(), "entries": entries}
    print(json.dumps(out, indent=2))
    # TODO(1a): write registry-aggregate.json privately, then derive audience slices per repo.
    return 0


def cmd_answer(args) -> int:
    # TODO(1a): retrieve matching nuggets, apply the answer contract (grounding, citation, confidence,
    # conflict surfacing, 90-day footnote). On no match, emit MISS_RESPONSE and log the gap.
    print(MISS_RESPONSE)
    return 0


def cmd_rot(args) -> int:
    # TODO(1a): walk the registry, flag Outdated (verified > 30d), Redundant (dup/superseded), Trivial;
    # emit a report keyed to each owner. 1b raises the Asana tasks.
    print("rot: not yet implemented", file=sys.stderr)
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
    sp.set_defaults(func=cmd_store)

    sp = sub.add_parser("index", help="rebuild the registry from a repo")
    sp.add_argument("repo")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("answer", help="answer a query from stored nuggets")
    sp.add_argument("query")
    sp.set_defaults(func=cmd_answer)

    sp = sub.add_parser("rot", help="hygiene sweep")
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
