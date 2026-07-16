#!/usr/bin/env python3
"""Reproducible acceptance harness for the Phase 1a core of kb.py.

Stdlib only: no third-party test runner, no network, deterministic. It drives kb.py as a subprocess (so the
real argparse surface and exit codes are exercised) and asserts the store gate, index, answer, and rot
behaviour. The committed KB under tests/fixtures/kb/ covers the flagged cases; the adversarial refusals and
the fresh rot-clean case are generated at runtime in a temp directory, so no deliberately-invalid or
stale-dated file sits in the tree.

Run:  python3 tests/run_acceptance.py
Exit: 0 if every check passes, 1 if any fails (the failures are listed).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_PY = REPO_ROOT / "kb.py"
FIXTURE_KB = REPO_ROOT / "tests" / "fixtures" / "kb"

# Borrow the exact miss phrase from the code so the assertion can never drift from kb.py.
sys.path.insert(0, str(REPO_ROOT))
import kb  # noqa: E402

MISS = kb.MISS_RESPONSE

VALID_REFERENCE = FIXTURE_KB / "it" / "password-rotation-a.md"
VALID_ATTESTATION = FIXTURE_KB / "shared" / "vpn-reset.md"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(KB_PY), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- fixtures generated at runtime (never committed) ------------------------

def _nugget(**fields: str) -> str:
    """Build a minimal valid nugget, then let the caller override or drop fields."""
    base = {
        "schema_version": "1", "id": "runtime-note", "title": "Runtime note",
        "domain": "shared", "type": "fact", "status": "published",
        "owner_gid": "0000000000000000", "owner_name": "Example Owner",
        "provenance_type": "attestation", "attested_by": "Example Engineer",
        "attested_on": "2020-01-01", "confidence": "high", "verified": "2020-01-01",
    }
    base.update({k: v for k, v in fields.items() if v is not None})
    for k, v in fields.items():
        if v is None:
            base.pop(k, None)
    body = fields.get("_body", "A runtime nugget body comfortably longer than the trivial threshold.")
    lines = [f"{k}: {v}" for k, v in base.items() if k != "_body"]
    return "---\n" + "\n".join(lines) + "\n---\n" + body + "\n"


# --- checks ------------------------------------------------------------------

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def store_valid_reference():
    p = run("store", str(VALID_REFERENCE))
    ok = p.returncode == 0 and "passes" in p.stdout
    return ok, f"rc={p.returncode} stdout={p.stdout.strip()!r}"


@check
def store_valid_attestation():
    p = run("store", str(VALID_ATTESTATION))
    ok = p.returncode == 0 and "passes" in p.stdout
    return ok, f"rc={p.returncode} stdout={p.stdout.strip()!r}"


@check
def store_refuse_unsourced_reference():
    with tempfile.TemporaryDirectory() as d:
        f = _write(Path(d) / "bad.md", _nugget(provenance_type="reference", source=None))
        p = run("store", str(f))
    ok = p.returncode == 1 and "REFUSED" in p.stderr and "source" in p.stderr
    return ok, f"rc={p.returncode} stderr={p.stderr.strip()!r}"


@check
def store_refuse_emdash():
    with tempfile.TemporaryDirectory() as d:
        # Build the em-dash from its code point so this source file stays em-dash-clean itself.
        emdash_body = "A body with an em dash " + chr(0x2014) + " which the voice gate must refuse."
        f = _write(Path(d) / "emdash.md", _nugget(_body=emdash_body))
        p = run("store", str(f))
    ok = p.returncode == 1 and "REFUSED" in p.stderr and "em-dash" in p.stderr
    return ok, f"rc={p.returncode} stderr={p.stderr.strip()!r}"


@check
def store_refuse_missing_required():
    with tempfile.TemporaryDirectory() as d:
        f = _write(Path(d) / "noowner.md", _nugget(owner_gid=None))
        p = run("store", str(f))
    ok = p.returncode == 1 and "REFUSED" in p.stderr and "owner_gid" in p.stderr
    return ok, f"rc={p.returncode} stderr={p.stderr.strip()!r}"


@check
def store_into_writes_by_domain():
    with tempfile.TemporaryDirectory() as d:
        p = run("store", str(VALID_REFERENCE), "--into", d)
        dest = Path(d) / "it" / "it-password-rotation-standard.md"
        ok = p.returncode == 0 and dest.exists()
        return ok, f"rc={p.returncode} exists={dest.exists()} stdout={p.stdout.strip()!r}"


@check
def index_emits_registry():
    p = run("index", str(FIXTURE_KB))
    if p.returncode != 0:
        return False, f"rc={p.returncode} stderr={p.stderr.strip()!r}"
    try:
        obj = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        return False, f"stdout is not JSON: {e}"
    entries = obj.get("entries", [])
    ok = (
        obj.get("schema_version") == 1
        and len(entries) == 3
        and all(e.get("content_hash") and e.get("path") and e.get("id") for e in entries)
    )
    return ok, f"schema={obj.get('schema_version')} n={len(entries)}"


@check
def answer_hit_cites_source():
    p = run("answer", "reset the vpn client", "--repo", str(FIXTURE_KB), "--no-log")
    ok = p.returncode == 0 and "[Source:" in p.stdout and "shared-vpn-reset" in p.stdout
    return ok, f"rc={p.returncode} stdout={p.stdout.strip()[:120]!r}"


@check
def answer_miss_exact_phrase():
    p = run("answer", "capital of france", "--repo", str(FIXTURE_KB), "--no-log")
    ok = p.returncode == 0 and p.stdout.strip() == MISS
    return ok, f"rc={p.returncode} stdout={p.stdout.strip()!r}"


@check
def rot_flags_redundant_and_outdated():
    p = run("rot", "--repo", str(FIXTURE_KB))
    out = p.stdout
    ok = (
        p.returncode == 0
        and "Redundant" in out
        and "Outdated" in out
        and "it-password-rotation-standard" in out
        and "it-password-rotation-duplicate" in out
        and "rot: clean" not in out
    )
    return ok, f"rc={p.returncode} stdout={out.strip()[:160]!r}"


@check
def rot_clean_on_fresh_repo():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as d:
        _write(
            Path(d) / "shared" / "fresh.md",
            _nugget(id="shared-fresh-note", title="Fresh note", verified=today, attested_on=today),
        )
        p = run("rot", "--repo", d)
    ok = p.returncode == 0 and "rot: clean" in p.stdout
    return ok, f"rc={p.returncode} stdout={p.stdout.strip()!r}"


@check
def sync_requires_manifest():
    # With no config.toml in cwd, sync has no [repos].manifest to resolve and must say so, not guess.
    p = run("sync")
    ok = p.returncode != 0 and "manifest" in p.stderr.lower()
    return ok, f"rc={p.returncode} stderr={p.stderr.strip()!r}"


@check
def cache_primitive_ttl():
    # Exercise the lookup-cache primitive directly (no git, no network): a fresh set hits within a live TTL,
    # a zero TTL always misses, a stamp far in the past misses, and a corrupt store is a miss not a crash.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        kb._cache_set(root, "ns", "k", {"v": 1})
        hit_fresh, val = kb._cache_get(root, "ns", "k", 3600)
        miss_zero, _ = kb._cache_get(root, "ns", "k", 0)

        # Hand-write a 2020 stamp so the age exceeds any finite TTL, deterministically (no sleep).
        store_path = kb._cache_file(root, "ns")
        store_path.write_text(
            json.dumps({"k": {"value": {"v": 1}, "stored_utc": "2020-01-01T00:00:00Z"}}), encoding="utf-8"
        )
        hit_stale, _ = kb._cache_get(root, "ns", "k", 3600)

        # A corrupt store must degrade to a miss, never raise.
        store_path.write_text("{not json", encoding="utf-8")
        try:
            hit_corrupt, _ = kb._cache_get(root, "ns", "k", 3600)
            crashed = False
        except Exception:
            hit_corrupt, crashed = True, True

    ok = hit_fresh and val == {"v": 1} and (not miss_zero) and (not hit_stale) and (not hit_corrupt) and not crashed
    return ok, f"fresh={hit_fresh} zero_miss={not miss_zero} stale_miss={not hit_stale} corrupt_miss={not hit_corrupt}"


@check
def feedback_findings_from_log_and_sweep():
    # A synthetic log (two mixed-case gap misses, one other gap, one conflict) plus a sweep of the fixture KB
    # (known Outdated/Redundant nuggets) must yield the right findings with stable keys and [<id>] tags.
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "interactions.jsonl"
        log.write_text("\n".join([
            json.dumps({"query": "reset the VPN", "hit": False, "kind": "gap"}),
            json.dumps({"query": "reset the vpn", "hit": False, "kind": "gap"}),
            json.dumps({"query": "capital of france", "hit": False, "kind": "gap"}),
            json.dumps({"query": "vpn thing", "hit": True, "cited": ["b-id", "a-id"], "conflict": True}),
        ]) + "\n", encoding="utf-8")
        p = run("feedback", "--repo", str(FIXTURE_KB), "--log-file", str(log))
    out = p.stdout
    ok = (
        p.returncode == 0
        and "KB-GAP:reset the vpn" in out           # the two mixed-case misses collapse to one stable key
        and "KB-GAP:capital of france" in out
        and "KB-CONFLICT:a-id|b-id" in out           # cited ids sorted into a deterministic key
        and "[KB-GAP]" in out and "[KB-CONFLICT]" in out
        and "KB-ROT-" in out and "it-password-rotation-standard" in out
        and "entity: (triage)" in out
    )
    return ok, f"rc={p.returncode} out={out.strip()[:200]!r}"


@check
def feedback_active_family_gate():
    # A non-existent log and no --repo: both families are 'skipped' (not silently empty), and no findings.
    with tempfile.TemporaryDirectory() as d:
        p = run("feedback", "--log-file", str(Path(d) / "nope.jsonl"))
    out = p.stdout
    ok = (
        p.returncode == 0 and "no findings" in out
        and "gap+conflict family skipped" in out and "ROT family skipped" in out
    )
    return ok, f"rc={p.returncode} out={out.strip()!r}"


@check
def feedback_log_append():
    # --log appends a well-formed UTC-stamped record; a second append never overwrites the first.
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "interactions.jsonl"
        p1 = run("feedback", "--log", "--kind", "rating", "--query", "reset vpn",
                 "--rating", "helpful", "--nugget", "shared-vpn-reset", "--log-file", str(log))
        p2 = run("feedback", "--log", "--kind", "miss", "--query", "capital of france", "--log-file", str(log))
        recs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = (
        p1.returncode == 0 and p2.returncode == 0 and len(recs) == 2
        and recs[0].get("rating") == "helpful" and recs[0].get("ts")
        and recs[1].get("hit") is False and recs[1].get("kind") == "gap"
    )
    return ok, f"rc1={p1.returncode} rc2={p2.returncode} n={len(recs)}"


@check
def feedback_steady_state():
    # An existing-but-empty log (collected, zero records) + a clean repo -> no findings, no skip notes.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "interactions.jsonl"
        log.write_text("", encoding="utf-8")  # exists but empty: collected, not skipped
        repo = Path(d) / "kb"
        _write(repo / "shared" / "fresh.md",
               _nugget(id="shared-fresh-note", title="Fresh note", verified=today, attested_on=today))
        p = run("feedback", "--repo", str(repo), "--log-file", str(log))
    out = p.stdout
    ok = p.returncode == 0 and "no findings" in out and "skipped" not in out
    return ok, f"rc={p.returncode} out={out.strip()!r}"


def main() -> int:
    failures = 0
    for fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:  # a check that blows up is a failure, not a crash
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {fn.__name__}    {detail}")
        if not ok:
            failures += 1
    total = len(CHECKS)
    print(f"\n{total - failures}/{total} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
