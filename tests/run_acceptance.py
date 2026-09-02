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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_PY = REPO_ROOT / "kb.py"
FIXTURE_KB = REPO_ROOT / "tests" / "fixtures" / "kb"

# Borrow the exact miss phrase from the code so the assertion can never drift from kb.py.
sys.path.insert(0, str(REPO_ROOT))
import kb  # noqa: E402

MISS = kb.MISS_RESPONSE

VALID_REFERENCE = FIXTURE_KB / "technical" / "password-rotation-a.md"
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
        dest = Path(d) / "technical" / "it-password-rotation-standard.md"
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
def index_out_json_writes_md_mirror():
    # --out to a .json path must also regenerate the sibling registry.md human mirror: header, do-not-edit
    # paragraph, generated stamp, and the id|title|domain|type|status|verified table sorted by id.
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "registry.json"
        p = run("index", str(FIXTURE_KB), "--out", str(out))
        mirror = out.with_name("registry.md")
        if p.returncode != 0 or not out.exists() or not mirror.exists():
            return False, f"rc={p.returncode} json={out.exists()} md={mirror.exists()} stderr={p.stderr.strip()!r}"
        entries = json.loads(out.read_text(encoding="utf-8"))["entries"]
        text = mirror.read_text(encoding="utf-8")
    ids = sorted(e["id"] for e in entries)
    rows_in_order = all(
        text.index(f"| {a} |") < text.index(f"| {b} |") for a, b in zip(ids, ids[1:])
    ) if all(f"| {i} |" in text for i in ids) else False
    ok = (
        text.startswith("# Registry: ")
        and "Do not edit by hand." in text
        and "Generated (UTC): " in text
        and "| id | title | domain | type | status | verified |" in text
        and rows_in_order
    )
    return ok, f"n={len(entries)} rows_in_order={rows_in_order}"


@check
def index_out_non_json_skips_md_mirror():
    # A non-.json --out (someone redirecting the payload elsewhere) must not spray a registry.md next to it.
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "registry.txt"
        p = run("index", str(FIXTURE_KB), "--out", str(out))
        out_exists = out.exists()
        mirror_exists = (Path(d) / "registry.md").exists()
    ok = p.returncode == 0 and out_exists and not mirror_exists
    return ok, f"rc={p.returncode} out_exists={out_exists} mirror_exists={mirror_exists}"


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
def last_used_map_takes_latest_hit_only():
    # Per-nugget last_used is the latest ts across HIT records citing it; misses and bad/absent ts are ignored.
    recs = [
        {"ts": "2026-01-01T00:00:00Z", "hit": True, "cited": ["x"]},
        {"ts": "2026-03-01T00:00:00Z", "hit": True, "cited": ["x", "y"]},
        {"ts": "2026-06-01T00:00:00Z", "hit": False, "cited": ["x"]},  # a miss is not usage
        {"ts": "not-a-date", "hit": True, "cited": ["z"]},             # unparseable ts skipped
        {"hit": True, "cited": ["w"]},                                 # missing ts skipped
    ]
    m = kb._last_used_map(recs)
    ok = (m.get("x") == kb._parse_iso_utc("2026-03-01T00:00:00Z")
          and m.get("y") == kb._parse_iso_utc("2026-03-01T00:00:00Z")
          and "z" not in m and "w" not in m)
    return ok, f"keys={sorted(m)} x={m.get('x')}"


@check
def rot_last_used_extends_and_ceiling():
    # The Outdated rule: a nugget verified past the window is left alone only while it is in active use AND
    # under the hard ceiling. Never-verified and past-ceiling are flagged regardless of use.
    now = _NOW

    def nug(nid, days_verified):
        v = "unverified" if days_verified is None else (now - timedelta(days=days_verified)).strftime("%Y-%m-%d")
        return _nugget_dict(id=nid, verified=v)

    nuggets = [
        nug("used-fresh", 60),          # verified 60d, used 5d ago -> NOT flagged (in use, under ceiling)
        nug("used-stale-usage", 60),    # verified 60d, last used 200d ago -> flagged (not in use)
        nug("unused", 60),              # verified 60d, never used -> flagged
        nug("used-over-ceiling", 200),  # verified 200d (> ceiling), used 5d ago -> flagged (over ceiling)
        nug("never-verified", None),    # never verified, used 5d ago -> flagged (use never excuses)
        nug("still-fresh", 10),         # verified 10d (< window), unused -> NOT flagged (still fresh)
    ]
    last_used = {
        "used-fresh": now - timedelta(days=5),
        "used-stale-usage": now - timedelta(days=200),
        "used-over-ceiling": now - timedelta(days=5),
        "never-verified": now - timedelta(days=5),
    }
    flags = kb._rot_flags(nuggets, now, last_used)
    outdated = {f["id"] for f in flags if any(r.startswith("Outdated") for r in f["reasons"])}
    expect = {"used-stale-usage", "unused", "used-over-ceiling", "never-verified"}
    ok = outdated == expect
    return ok, f"flagged={sorted(outdated)} expected={sorted(expect)}"


@check
def rot_empty_last_used_matches_legacy():
    # Regression guard: no map, or an empty map, reproduces the pre-usage behaviour exactly.
    now = _NOW
    n = _nugget_dict(id="a", verified=(now - timedelta(days=60)).strftime("%Y-%m-%d"))

    def is_out(flags):
        return any(any(r.startswith("Outdated") for r in f["reasons"]) for f in flags)

    ok = is_out(kb._rot_flags([n], now)) and is_out(kb._rot_flags([n], now, {}))
    return ok, f"none_map_and_empty_map_both_flag={ok}"


@check
def feedback_usage_suppresses_outdated_under_ceiling():
    # End-to-end through the feedback sweep: a stale-but-recently-cited nugget is held back; drop the usage
    # record and the same nugget is flagged KB-ROT-OUTDATED.
    now = datetime.now(timezone.utc)
    vdate = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "kb"
        _write(repo / "shared" / "used-note.md", _nugget(id="used-note", title="Used note", verified=vdate))
        log = Path(d) / "interactions.jsonl"
        log.write_text(json.dumps(
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "hit": True, "cited": ["used-note"]}) + "\n",
            encoding="utf-8")
        p_used = run("feedback", "--repo", str(repo), "--log-file", str(log))
        log.write_text("", encoding="utf-8")  # collected but empty -> not in use
        p_unused = run("feedback", "--repo", str(repo), "--log-file", str(log))
    suppressed = p_used.returncode == 0 and "used-note" not in p_used.stdout
    flagged = "KB-ROT-OUTDATED" in p_unused.stdout and "used-note" in p_unused.stdout
    ok = suppressed and flagged
    return ok, f"suppressed_when_used={suppressed} flagged_when_unused={flagged}"


@check
def index_stamps_last_used_from_log():
    # Single-repo index stamps a derived last_used from the log when given one, and leaves it null otherwise.
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "kb"
        _write(repo / "shared" / "n1.md", _nugget(id="n1", title="N one", verified="2026-01-01"))
        log = Path(d) / "interactions.jsonl"
        log.write_text(json.dumps(
            {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "hit": True, "cited": ["n1"]}) + "\n", encoding="utf-8")
        out_with = Path(d) / "with.json"
        run("index", str(repo), "--log-file", str(log), "--out", str(out_with))
        out_without = Path(d) / "without.json"
        run("index", str(repo), "--out", str(out_without))
        e_with = json.loads(out_with.read_text())["entries"][0]
        e_without = json.loads(out_without.read_text())["entries"][0]
    ok = (e_with.get("last_used") is not None
          and "last_used" in e_without and e_without.get("last_used") is None)
    return ok, f"with={e_with.get('last_used')} without_has_null_field={'last_used' in e_without}"


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


# --- Asana reconcile leg (in-process, with a fake client) -------------------
#
# Live Asana writes cannot be unit-tested, so `_reconcile` is driven in-process against a dict-backed fake
# that records every POST/PUT and serves canned reads. The live-only checks (a real --commit into the KB
# Findings section, the steady-state no-op second run, reopen on a human-closed task, the Verification attach)
# are documented in tests/README.md and run on the NUC with a real [tracking.pat]; they are not in this
# offline harness.

class FakeAsanaClient:
    """Dict-backed stand-in for kb._AsanaClient. Reads return canned fixtures; every mutating call is recorded
    in .writes as (method, path, body) so a check can assert exactly what was (or was not) written."""

    def __init__(self, sections=None, tasks=None, fields=None, settings=None, project_tasks=None):
        self._sections = [dict(s) for s in (sections or [])]
        self._tasks = [dict(t) for t in (tasks or [])]
        self._fields = [dict(f) for f in (fields or [])]
        self._settings = [dict(s) for s in (settings or [])]
        # `_project_tasks` backs a whole-project read (`/projects/{gid}/tasks`) as distinct from a
        # section-scoped read (`/sections/{gid}/tasks`); the coordination audit reads the former to see
        # [Build]/[Chip]/[KB] tasks that never appear in the KB Findings section.
        self._project_tasks = [dict(t) for t in (project_tasks or [])]
        self.writes = []
        self._counter = 900000

    def _mint(self):
        self._counter += 1
        return str(self._counter)

    def get_all(self, path, params=None):
        if path.startswith("/projects/") and path.endswith("/sections"):
            return [dict(s) for s in self._sections]
        if path.startswith("/projects/") and path.endswith("/tasks"):
            return [dict(t) for t in self._project_tasks]
        if "custom_field_settings" in path:
            return [dict(s) for s in self._settings]
        if "custom_fields" in path:
            return [dict(f) for f in self._fields]
        if path.startswith("/sections/") and path.endswith("/tasks"):
            return [dict(t) for t in self._tasks]
        return []

    def get(self, path, params=None):
        return {"data": {}}

    def post(self, path, body):
        self.writes.append(("POST", path, dict(body)))
        if path.startswith("/projects/") and path.endswith("/sections"):
            gid = self._mint()
            self._sections.append({"gid": gid, "name": body.get("name")})
            return {"data": {"gid": gid}}
        if path == "/tasks":
            gid = self._mint()
            self._tasks.append({"gid": gid, "name": body.get("name"), "completed": False})
            return {"data": {"gid": gid}}
        return {"data": {"gid": None}}

    def put(self, path, body):
        self.writes.append(("PUT", path, dict(body)))
        if path.startswith("/tasks/"):
            gid = path.split("/")[2]
            for t in self._tasks:
                if t.get("gid") == gid:
                    if "completed" in body:
                        t["completed"] = body["completed"]
                    if "notes" in body:
                        t["notes"] = body["notes"]
        return {"data": {}}


def _finding(fid, subject, entity="(triage)", owner_gid=None, detail="detail"):
    return {"finding_id": fid, "key": f"{fid}:{subject}", "entity": entity,
            "owner_gid": owner_gid, "detail": detail}


def _cfg(**tracking):
    base = {"project_gid": "PROJ", "workspace_gid": "WS", "section_name": "KB Findings",
            "verification_field": "Verification", "default_assignee": "DA"}
    base.update(tracking)
    return {"tracking": {**base, "pat": {"token": "unused-by-reconcile"}}}


_KB_SECTION = [{"gid": "SEC", "name": "KB Findings"}]
_ALL_ACTIVE = {"KB-GAP", "KB-CONFLICT", "KB-ROT-OUTDATED", "KB-ROT-REDUNDANT", "KB-ROT-TRIVIAL"}


@check
def reconcile_create_assigns_and_files_to_section():
    fake = FakeAsanaClient(sections=list(_KB_SECTION))
    findings = [_finding("KB-ROT-OUTDATED", "nug-1", entity="Owner A", owner_gid="111"),
                _finding("KB-GAP", "reset vpn")]
    counts = kb._reconcile(fake, _cfg(), findings, set(_ALL_ACTIVE), True)
    creates = [w for w in fake.writes if w[0] == "POST" and w[1] == "/tasks"]
    rot = next((w for w in creates if w[2]["name"].startswith("[KB-ROT-OUTDATED]")), None)
    gap = next((w for w in creates if w[2]["name"].startswith("[KB-GAP]")), None)
    ok = (counts["created"] == 2 and counts["failed"] == 0
          and rot is not None and rot[2].get("assignee") == "111"      # ROT -> the nugget owner
          and gap is not None and gap[2].get("assignee") == "DA"       # triage -> the default assignee
          and any(w[1].endswith("/addTask") for w in fake.writes))
    return ok, f"counts={counts}"


@check
def reconcile_noop_when_open_task_matches():
    title = kb._finding_title(_finding("KB-GAP", "reset vpn"))
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T1", "name": title, "completed": False}])
    counts = kb._reconcile(fake, _cfg(), [_finding("KB-GAP", "reset vpn")], {"KB-GAP", "KB-CONFLICT"}, True)
    ok = counts["noop"] == 1 and counts["created"] == 0 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def reconcile_verify_clears_absent_finding():
    title = kb._finding_title(_finding("KB-GAP", "old query"))
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T9", "name": title, "completed": False}])
    counts = kb._reconcile(fake, _cfg(), [], {"KB-GAP", "KB-CONFLICT"}, True)
    puts = [w for w in fake.writes if w[0] == "PUT" and w[1] == "/tasks/T9"]
    ok = counts["verify_cleared"] == 1 and any(w[2].get("completed") is True for w in puts)
    return ok, f"counts={counts} writes={fake.writes}"


@check
def reconcile_reopens_human_closed_still_present():
    f = _finding("KB-ROT-REDUNDANT", "dup-nug", entity="Owner A", owner_gid="111")
    title = kb._finding_title(f)
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T5", "name": title, "completed": True}])
    counts = kb._reconcile(fake, _cfg(), [f], {"KB-ROT-REDUNDANT"}, True)
    puts = [w for w in fake.writes if w[0] == "PUT" and w[1] == "/tasks/T5"]
    ok = counts["reopened"] == 1 and any(w[2].get("completed") is False for w in puts)
    return ok, f"counts={counts} writes={fake.writes}"


@check
def reconcile_gate_skips_uncollected_family():
    # An open ROT task, but ROT was NOT collected this run (only the log families are active). It must be left
    # exactly as-is, never verify-cleared (the active-family gate).
    title = kb._finding_title(_finding("KB-ROT-OUTDATED", "nug-x", entity="Owner A"))
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T7", "name": title, "completed": False}])
    counts = kb._reconcile(fake, _cfg(), [], {"KB-GAP", "KB-CONFLICT"}, True)
    touched = [w for w in fake.writes if "/tasks/T7" in w[1]]
    ok = counts["skipped_inactive"] >= 1 and touched == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def reconcile_isolation_ignores_foreign_tasks():
    kbtitle = kb._finding_title(_finding("KB-GAP", "q"))
    fake = FakeAsanaClient(
        sections=list(_KB_SECTION),
        tasks=[{"gid": "B1", "name": "[Build] scaffold", "completed": False},
               {"gid": "C1", "name": "[Chip] rollout", "completed": False},
               {"gid": "L1", "name": "[OTHER-TOOL] a sibling audit task", "completed": False},
               {"gid": "K1", "name": kbtitle, "completed": False}])
    counts = kb._reconcile(fake, _cfg(), [], {"KB-GAP", "KB-CONFLICT"}, True)
    foreign = [w for w in fake.writes if any(g in w[1] for g in ("/B1", "/C1", "/L1"))]
    kb_put = [w for w in fake.writes if w[0] == "PUT" and w[1] == "/tasks/K1"]
    ok = foreign == [] and counts["verify_cleared"] == 1 and len(kb_put) >= 1
    return ok, f"counts={counts} writes={fake.writes}"


@check
def reconcile_dry_run_makes_zero_writes():
    fake = FakeAsanaClient(sections=list(_KB_SECTION))
    findings = [_finding("KB-GAP", "reset vpn"),
                _finding("KB-ROT-TRIVIAL", "nug-2", entity="Owner A", owner_gid="111")]
    counts = kb._reconcile(fake, _cfg(), findings, {"KB-GAP", "KB-CONFLICT", "KB-ROT-TRIVIAL"}, False)
    ok = counts["created"] == 2 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def finding_notes_body_has_four_sections():
    # Every emitted task must carry a complete, self-contained notes body (feedback_asana_task_completeness):
    # Background + Evidence + Expected action + How it clears, for every owned family (GAP, CONFLICT, ROT).
    cases = [
        _finding("KB-ROT-REDUNDANT", "seed-inventory-worklog", entity="Owner A", detail="Redundant (status retired)"),
        _finding("KB-GAP", "reset the vpn", detail="3 miss(es) logged, no matching nugget"),
        _finding("KB-CONFLICT", "nug-a|nug-b", detail="2 logged tie(s) for query 'x'; cited nug-a, nug-b"),
    ]
    headers = ("Background", "Evidence", "Expected action", "How it clears")
    ok = True
    detail = ""
    for f in cases:
        body = kb._finding_notes(f)
        has_headers = all(h in body for h in headers)
        # Background names the raising tool; Evidence carries the subject + observed value; How-it-clears names
        # the re-discovery close so the assignee never hand-closes.
        grounded = ("kb.py" in body and kb._finding_subject(f) in body and f["detail"] in body
                    and "verified-clear" in body and "Do not close it by hand" in body)
        if not (has_headers and grounded):
            ok = False
            detail = f"{f['finding_id']} headers={has_headers} grounded={grounded}\n---\n{body}"
            break
    return ok, detail


@check
def create_task_body_carries_complete_notes():
    # The create path (not just the helper) must POST the complete body, in place of the old one-line detail.
    fake = FakeAsanaClient(sections=list(_KB_SECTION))
    f = _finding("KB-ROT-REDUNDANT", "seed-inventory-worklog", entity="Owner A", owner_gid="111",
                 detail="Redundant (status retired)")
    kb._reconcile(fake, _cfg(), [f], {"KB-ROT-REDUNDANT"}, True)
    create = next((w for w in fake.writes if w[0] == "POST" and w[1] == "/tasks"), None)
    notes = (create[2].get("notes") if create else "") or ""
    ok = (create is not None
          and all(h in notes for h in ("Background", "Evidence", "Expected action", "How it clears"))
          and notes == kb._finding_notes(f))       # exactly the compliant body, nothing thinner
    return ok, f"notes={notes!r}"


@check
def backfill_notes_heals_thin_task_idempotently():
    # A task created before the complete-notes rule carries a thin body. --backfill-notes heals it to the
    # compliant body with exactly one notes PUT; a second run writes nothing (idempotent).
    f = _finding("KB-ROT-REDUNDANT", "seed-inventory-worklog", entity="Owner A", owner_gid="111",
                 detail="Redundant (status retired)")
    title = kb._finding_title(f)
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T1", "name": title, "completed": False,
                                   "notes": "Redundant (status retired)"}])
    counts = kb._reconcile(fake, _cfg(), [f], {"KB-ROT-REDUNDANT"}, True, backfill_notes=True)
    notes_puts = [w for w in fake.writes if w[0] == "PUT" and w[1] == "/tasks/T1" and "notes" in w[2]]
    healed = notes_puts and notes_puts[0][2]["notes"] == kb._finding_notes(f)
    # Idempotent re-run: notes now match, so zero writes.
    fake2 = FakeAsanaClient(sections=list(_KB_SECTION),
                            tasks=[{"gid": "T1", "name": title, "completed": False,
                                    "notes": kb._finding_notes(f)}])
    counts2 = kb._reconcile(fake2, _cfg(), [f], {"KB-ROT-REDUNDANT"}, True, backfill_notes=True)
    ok = (counts["notes_updated"] == 1 and len(notes_puts) == 1 and healed
          and counts2["notes_updated"] == 0 and fake2.writes == [])
    return ok, f"counts={counts} counts2={counts2} writes2={fake2.writes}"


@check
def backfill_notes_off_by_default_keeps_steady_state():
    # Without --backfill-notes a normal reconcile must NOT rewrite a matching task's notes, even a thin one:
    # the audit family's steady-state run writes nothing.
    f = _finding("KB-GAP", "reset vpn", detail="1 miss(es) logged, no matching nugget")
    title = kb._finding_title(f)
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T1", "name": title, "completed": False, "notes": "thin"}])
    counts = kb._reconcile(fake, _cfg(), [f], {"KB-GAP", "KB-CONFLICT"}, True)  # backfill_notes defaults False
    ok = counts["noop"] == 1 and counts["notes_updated"] == 0 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def backfill_notes_dry_run_previews_without_writing():
    # A --backfill-notes preview (commit=False) reads live tasks and reports notes_updated, but writes nothing.
    f = _finding("KB-ROT-TRIVIAL", "nug-2", entity="Owner A", owner_gid="111", detail="Trivial (near-empty body)")
    title = kb._finding_title(f)
    fake = FakeAsanaClient(sections=list(_KB_SECTION),
                           tasks=[{"gid": "T1", "name": title, "completed": False, "notes": "thin"}])
    counts = kb._reconcile(fake, _cfg(), [f], {"KB-ROT-TRIVIAL"}, False, backfill_notes=True)
    ok = counts["notes_updated"] == 1 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def reconcile_sets_enum_or_degrades_to_comment():
    vfields = [{"gid": "VF", "name": "Verification", "resource_subtype": "enum",
                "enum_options": [{"gid": "o1", "name": "unverified"}]}]
    settings = [{"custom_field": {"gid": "VF"}}]
    fake_field = FakeAsanaClient(sections=list(_KB_SECTION), fields=vfields, settings=settings)
    kb._reconcile(fake_field, _cfg(), [_finding("KB-GAP", "q1")], {"KB-GAP", "KB-CONFLICT"}, True)
    enum_set = any(w[0] == "PUT" and isinstance(w[2].get("custom_fields"), dict) for w in fake_field.writes)
    fake_none = FakeAsanaClient(sections=list(_KB_SECTION))
    kb._reconcile(fake_none, _cfg(), [_finding("KB-GAP", "q2")], {"KB-GAP", "KB-CONFLICT"}, True)
    commented = any(w[0] == "POST" and w[1].endswith("/stories") for w in fake_none.writes)
    ok = enum_set and commented
    return ok, f"enum_set={enum_set} commented={commented}"


# --- coordination-audit leg (opt-in auto-close of coordination tasks) -------
#
# A [Build]/[Chip]/[KB] coordination task may opt into auto-close with a `closes-when-cleared:` anchor on the
# first line of its notes. `_run_coordination_audit` closes it once its declared KB finding-ids have no OPEN
# [KB-*] task left in the KB Findings section. Driven here with the same FakeAsanaClient; the KB findings live
# in the section (`tasks=`), the coordination tasks in the project read (`project_tasks=`).

def _kbfind(fid, subject, completed):
    """A [KB-*] finding task fixture as it sits in the KB Findings section."""
    return {"gid": f"F-{fid}-{subject}", "name": kb._finding_title(_finding(fid, subject)),
            "completed": completed}


_COORD_ZERO = {"closed": 0, "would_close": 0, "still_open": 0, "skipped": 0, "failed": 0}


@check
def coord_closes_when_all_tracked_findings_cleared():
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True), _kbfind("KB-ROT-REDUNDANT", "nug-b", True)]
    coord = [{"gid": "CO1", "name": "[KB] clear KB-ROT", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED, KB-ROT-REDUNDANT\nrest of the body"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    closed_put = [w for w in fake.writes if w[0] == "PUT" and w[1] == "/tasks/CO1"
                  and w[2].get("completed") is True]
    story = [w for w in fake.writes if w[0] == "POST" and w[1] == "/tasks/CO1/stories"]
    ok = counts["closed"] == 1 and len(closed_put) == 1 and len(story) == 1
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_leaves_open_when_a_tracked_finding_still_open():
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True), _kbfind("KB-ROT-REDUNDANT", "nug-b", False)]
    coord = [{"gid": "CO2", "name": "[KB] clear KB-ROT", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED, KB-ROT-REDUNDANT"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    ok = counts["closed"] == 0 and counts["still_open"] == 1 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_ignores_task_without_first_line_anchor():
    # The phrase appears, but NOT on the first non-empty line, so the task is not opted in.
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True)]
    coord = [{"gid": "CO3", "name": "[Build] some feature", "completed": False,
              "notes": "Background\ncloses-when-cleared: KB-ROT-OUTDATED"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    ok = counts == _COORD_ZERO and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_skips_unknown_declared_id():
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True)]
    coord = [{"gid": "CO4", "name": "[KB] mistyped", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED, NOT-A-REAL-ID"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    ok = counts["skipped"] == 1 and counts["closed"] == 0 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_skips_declared_id_with_no_finding_in_section():
    # A valid KB id, but NO finding of it exists in the section (open or closed): never close vacuously.
    coord = [{"gid": "CO5", "name": "[KB] premature", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=[], project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    ok = counts["skipped"] == 1 and counts["closed"] == 0 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_dry_run_makes_zero_writes():
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True)]
    coord = [{"gid": "CO6", "name": "[KB] clear", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", False)
    ok = counts["would_close"] == 1 and counts["closed"] == 0 and fake.writes == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_isolation_never_touches_kb_finding_tasks():
    # A [KB-*] finding task also appears in the project read (findings are project members). Even if it carried
    # an anchor, it must never be closed by this path; only the real coordination task is acted on.
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True)]
    find_in_project = {"gid": "FIND1", "name": kb._finding_title(_finding("KB-ROT-OUTDATED", "nug-a")),
                       "completed": False, "notes": "closes-when-cleared: KB-ROT-OUTDATED"}
    coord = [find_in_project,
             {"gid": "CO7", "name": "[KB] clear KB-ROT", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED"}]
    fake = FakeAsanaClient(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    touched_find = [w for w in fake.writes if "/tasks/FIND1" in w[1]]
    closed_coord = [w for w in fake.writes if w[0] == "PUT" and w[1] == "/tasks/CO7"]
    ok = touched_find == [] and counts["closed"] == 1 and len(closed_coord) == 1
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_peer_already_closed_is_a_noop():
    # Compare-and-swap: the project read saw the task open, but a peer closed it before our write (the shared
    # asana_sc_pat makes this the normal race). The live re-GET reports it closed, so we make no write.
    findings = [_kbfind("KB-ROT-OUTDATED", "nug-a", True)]
    coord = [{"gid": "CO8", "name": "[KB] clear", "completed": False,
              "notes": "closes-when-cleared: KB-ROT-OUTDATED"}]

    class PeerClosed(FakeAsanaClient):
        def get(self, path, params=None):
            if path == "/tasks/CO8":
                return {"data": {"completed": True}}
            return super().get(path, params)

    fake = PeerClosed(sections=list(_KB_SECTION), tasks=findings, project_tasks=coord)
    counts = kb._run_coordination_audit(fake, "PROJ", "KB Findings", True)
    writes_to_task = [w for w in fake.writes if w[1].startswith("/tasks/CO8")]
    ok = counts["closed"] == 0 and writes_to_task == []
    return ok, f"counts={counts} writes={fake.writes}"


@check
def coord_parse_anchor_variants():
    # First-line only, case-insensitive key, comma and/or whitespace separated, de-duplicated.
    p = kb._parse_closes_anchor
    ok = (p("closes-when-cleared: KB-ROT-OUTDATED, KB-ROT-REDUNDANT") == ["KB-ROT-OUTDATED", "KB-ROT-REDUNDANT"]
          and p("Closes-When-Cleared:  KB-GAP   KB-GAP") == ["KB-GAP"]
          and p("Background\ncloses-when-cleared: KB-GAP") is None
          and p("no anchor at all") is None
          and p("") is None)
    return ok, "anchor parse variants"


@check
def rot_findings_carry_owner_gid():
    now = datetime.now(timezone.utc)
    flags = kb._rot_flags(kb._load_nuggets(FIXTURE_KB), now)
    findings = kb._rot_findings(flags)
    ok = (bool(flags) and all("owner_gid" in f for f in flags)
          and bool(findings) and all("owner_gid" in f for f in findings))
    return ok, f"flags={len(flags)} findings={len(findings)}"


@check
def resolve_tracking_pat_order_and_error():
    inline = kb._resolve_tracking_pat({"tracking": {"pat": {"token": "INLINE"}}})
    with tempfile.TemporaryDirectory() as d:
        sf = Path(d) / "pat"
        sf.write_text("FILETOKEN\n", encoding="utf-8")
        fromfile = kb._resolve_tracking_pat({"tracking": {"pat": {"secret_file": str(sf)}}})
        both = kb._resolve_tracking_pat({"tracking": {"pat": {"token": "INLINE", "secret_file": str(sf)}}})
    raised = False
    try:
        kb._resolve_tracking_pat({"tracking": {"pat": {}}})
    except SystemExit:
        raised = True
    ok = inline == "INLINE" and fromfile == "FILETOKEN" and both == "INLINE" and raised
    return ok, f"inline={inline!r} file={fromfile!r} both={both!r} raised={raised}"


@check
def asana_client_never_leaks_pat_in_errors():
    # A failing request must not carry the PAT into the error message (it rides in a header, never the URL).
    client = kb._AsanaClient("SENTINELTOKEN", base="http://127.0.0.1:9", max_retries=0)
    msg = ""
    try:
        client.get("/users/me")
    except kb._AsanaError as exc:
        msg = str(exc)
    ok = msg != "" and "SENTINELTOKEN" not in msg
    return ok, f"msg={msg!r}"


@check
def finding_title_roundtrip_and_isolation():
    cases = [_finding("KB-GAP", "how do i fix [thing] please"),
             _finding("KB-CONFLICT", "a-id|b-id"),
             _finding("KB-ROT-OUTDATED", "some-nugget-id")]
    rt = all(kb._finding_id_from_title(kb._finding_title(f)) == f["finding_id"] for f in cases)
    foreign = all(kb._finding_id_from_title(t) is None
                  for t in ("[Build] scaffold", "[Chip] rollout", "[OTHER-TOOL] a sibling task", "plain text"))
    big = _finding("KB-GAP", "x" * 2000)
    bigtitle = kb._finding_title(big)
    over = len(bigtitle) <= kb._ASANA_NAME_MAX and kb._finding_id_from_title(bigtitle) == "KB-GAP"
    return (rt and foreign and over), f"rt={rt} foreign={foreign} over={over} biglen={len(bigtitle)}"


@check
def emit_frontmatter_round_trips():
    meta = {
        "schema_version": "1", "id": "round-trip-note", "title": "A [bracketed] title: with a colon",
        "domain": "shared", "type": "reference", "status": "draft",
        "owner_gid": "0000000000000000", "owner_name": "Example Owner",
        "provenance_type": "reference", "source": "https://example.invalid/doc.md",
        "confidence": "medium", "verified": "unverified", "tags": ["prescan", "example"],
        "zz_custom": None,
    }
    body = "A round-trip body comfortably longer than the trivial threshold, kept on one paragraph.\n"
    text = kb.emit_frontmatter(meta, body)
    meta2, body2 = kb.parse_frontmatter(text)
    same_meta = all(meta2.get(k) == v for k, v in meta.items())
    errors = kb.validate_entry(meta2, body2)
    ok = same_meta and body2 == body and not errors
    return ok, f"same_meta={same_meta} body_ok={body2 == body} errors={errors}"


@check
def prescan_secret_names_skip_by_name():
    flagged = ["config.py", "CONFIG.TOML", "config.ini", ".env", ".envrc", "server.pem",
               "id_rsa", "id_rsa.pub", "api-credentials.json", "deploy-secrets.md", "private.key"]
    readable = ["notes.md", "config.example.toml", "config.py.sample", "monkey.md", "keyboard-map.md"]
    bad = [n for n in flagged if not kb._is_secret_name(n)]
    good = [n for n in readable if kb._is_secret_name(n)]
    return not bad and not good, f"missed={bad} overblocked={good}"


@check
def prescan_source_url_and_dedup_norm():
    sha_a, sha_b = "a" * 40, "b" * 40
    https = kb._seed_source_url("https://github.com/example/repo.git", sha_a, "docs/guide.md")
    ssh = kb._seed_source_url("git@github.com:example/repo.git", sha_a, "docs/guide.md")
    blob = f"https://github.com/example/repo/blob/{sha_a}/docs/guide.md"
    plain = kb._seed_source_url("file:///tmp/seed.git", sha_a, "docs/guide.md")
    bare = kb._seed_source_url(None, None, "docs/guide.md")
    forms = https == blob and ssh == blob and plain == "file:///tmp/seed/docs/guide.md" and bare == "docs/guide.md"
    # Dedup normalisation: the pinned ref is ignored, repo roots match across https/ssh, file != root.
    ref_free = kb._norm_source(blob) == kb._norm_source(blob.replace(sha_a, sha_b))
    roots = kb._norm_source("https://github.com/example/repo.git") == kb._norm_source("git@github.com:example/repo.git")
    distinct = kb._norm_source(blob) != kb._norm_source("https://github.com/example/repo.git")
    return forms and ref_free and roots and distinct, f"forms={forms} ref_free={ref_free} roots={roots} distinct={distinct}"


@check
def prescan_abstract_never_cuts_mid_sentence():
    """The abstract of an over-budget paragraph is whole sentences, never a cut with a stop bolted on.

    The regression case is the one that shipped: a two-sentence paragraph whose second sentence straddles
    the budget. The old rule cut at the last word inside 200 chars, stripped the trailing punctuation and
    appended a full stop, publishing "...read these files first rather." as a finished statement.
    """
    raw = ("Working knowledge base for supporting the Kacific IT team on design, planning and "
           "configuration. This is the durable memory for the work: follow-up agents and sessions read "
           "these files first rather than inferring an estate fact from a repository.")
    out = kb._candidate_abstract(raw, "README.md", "shadow-it")
    # What the old rule produced, rebuilt here so the assertion pins the DEFECT and not just today's output.
    old = raw[:kb._ABSTRACT_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:.") + "."
    fixed = out != old and not kb._looks_truncated(out)
    whole = out == "Working knowledge base for supporting the Kacific IT team on design, planning and configuration."
    # The guard must be discriminating: it has to REJECT the old output, or it proves nothing.
    catches_old = kb._looks_truncated(old)
    return fixed and whole and catches_old, f"fixed={fixed} whole={whole} catches_old={catches_old} out={out!r}"


@check
def prescan_abstract_completes_only_an_uncut_line():
    """A terminator may finish a paragraph that fitted whole; it is never bolted onto a cut one."""
    short = "Provenance manifest for the internal guides and induction materials"
    completed = kb._candidate_abstract(short, "SOURCE.md", "shadow-it") == short + "."
    # Over budget with no sentence boundary anywhere: dropping to the pointer beats publishing a fragment.
    runon = "word " * 200
    dropped = kb._candidate_abstract(runon, "docs/notes.md", "seedkey").startswith("Pointer to docs/notes.md")
    # Empty prose keeps the deterministic pointer line.
    empty = kb._candidate_abstract("", "docs/empty.md", "seedkey").startswith("Pointer to docs/empty.md")
    return completed and dropped and empty, f"completed={completed} dropped={dropped} empty={empty}"


@check
def prescan_looks_truncated_catches_dangling_words_not_missing_stops():
    """The check must catch a dangling function word, since the defect always ended in a full stop."""
    fragments = ["Sessions read these files first rather.", "One of the per-tool ops repos run by the scheduler, sibling to.",
                 "Inspects the helpdesk board, writes a.", "The legacy Jupiter2 SRS training set. Per the.",
                 "Harvests live configurations and aggregates them with.", "A trailing clause with no terminator"]
    complete = ["Index of trusted sources. Cite these paths instead of relying on memory.",
                "Internal operational tooling for the edge routers at the primary hub.",
                "Pointer to docs/guide.md in the shadow-it seed source.",
                "Runs on demand plus a daily cron.", "Is it reachable? Yes."]
    missed = [f for f in fragments if not kb._looks_truncated(f)]
    overblocked = [c for c in complete if kb._looks_truncated(c)]
    # A terminator-only check would score every fragment above as clean; prove this one does not.
    naive_would_pass = [f for f in fragments if f.endswith((".", "!", "?"))]
    return not missed and not overblocked and len(naive_would_pass) >= 5, \
        f"missed={missed} overblocked={overblocked} naive_clean={len(naive_would_pass)}"


@check
def prescan_guard_allows_sentence_final_particles_and_quantifiers():
    """A word that can end a sentence must not be treated as dangling, or the fail-safe eats true abstracts.

    Found on the real corpus, not imagined: the first draft of the word list carried on, one, this and such,
    so three otherwise perfect abstracts were rejected and published as pointer lines instead. In a fail-safe
    a false positive is the expensive direction, since it destroys prose the source really does support.
    """
    endings = [
        "This repo has no docs/adr/ tree, so this follows the existing convention rather than inventing one.",
        "This page is a signpost to legwork already done that the reverse-engineering task should build on.",
        "Nothing else in the pipeline depends on it, so the safe move is to turn the scheduled job off.",
        "The rendered bundle is generated, never hand-written; do not edit it directly, generate it like this.",
        "Two sites remain on the legacy path and the migration plan covers both.",
    ]
    wrongly_flagged = [e for e in endings if kb._looks_truncated(e)]
    # The genuinely-dangling tails the brief named must still be caught, or this test has merely gone blind.
    still_caught = [f for f in ("Sessions read these files first rather.", "One of the ops repos, sibling to.",
                                "Inspects the board, writes a.", "Aggregates them with.", "Per the.")
                    if kb._looks_truncated(f)]
    return not wrongly_flagged and len(still_caught) == 5, \
        f"wrongly_flagged={wrongly_flagged} still_caught={len(still_caught)} of 5"


@check
def prescan_sentence_split_holds_on_estate_prose():
    """Conservative splitting: abbreviations, file extensions and version numbers are not sentence ends."""
    cases = {
        "Read config.example.toml first. Then run the tool.": 2,
        "Source of truth is location_of() in assets.py; this doc is a pointer.": 1,
        "Watermark: 2026-07-11. Status: v1, initial consolidation.": 2,
        "Use e.g. the audit cert, not a PAT.": 1,
        "Example Satellites Ltd. holds the licence.": 1,
        "See ../README.md. Nothing binary is copied into git.": 2,
    }
    bad = {t: (len(kb._split_sentences(t)), n) for t, n in cases.items() if len(kb._split_sentences(t)) != n}
    return not bad, f"mismatches={bad}"


# --- audience slicing -------------------------------------------------------

def _slice_aggregate() -> dict:
    """A synthetic cross-audience aggregate: one entry sourced from each of four department repos."""
    def e(nid, repo):
        return {"id": nid, "title": nid, "domain": repo, "status": "published", "source_repo": repo}
    return {
        "repos": {
            "allstaff": {"audience": "AllStaff"},
            "technical": {"audience": "Technical"},
            "commercial": {"audience": "Commercial"},
            "hr": {"audience": "HR"},
        },
        "entries": [e("a-base", "allstaff"), e("t-net", "technical"),
                    e("c-plan", "commercial"), e("h-leave", "hr")],
    }


_AUDIENCES = {
    "AllStaff": ["AllStaff"],
    "Technical": ["AllStaff", "Technical"],
    "Commercial": ["AllStaff", "Commercial"],
    "HR": ["AllStaff", "HR"],
}


@check
def slice_dept_gets_base_plus_own():
    slices = kb.derive_slices(_slice_aggregate(), _AUDIENCES)
    ids = {k: sorted(x["id"] for x in v) for k, v in slices.items()}
    ok = (ids["technical"] == ["a-base", "t-net"]
          and ids["commercial"] == ["a-base", "c-plan"]
          and ids["hr"] == ["a-base", "h-leave"]
          and ids["allstaff"] == ["a-base"])
    return ok, f"ids={ids}"


@check
def slice_never_leaks_across_departments():
    slices = kb.derive_slices(_slice_aggregate(), _AUDIENCES)
    ids = {k: {x["id"] for x in v} for k, v in slices.items()}
    # AllStaff sees no department entry; Technical sees no Commercial/HR; Commercial sees no Technical/HR.
    allstaff_clean = ids["allstaff"] == {"a-base"}
    technical_clean = not ({"c-plan", "h-leave"} & ids["technical"])
    commercial_clean = not ({"t-net", "h-leave"} & ids["commercial"])
    ok = allstaff_clean and technical_clean and commercial_clean
    return ok, f"allstaff={allstaff_clean} technical={technical_clean} commercial={commercial_clean} ids={ids}"


@check
def slice_default_deny_for_unmapped_audience():
    # A repo whose audience label is absent from the [audiences] map must get an empty slice, never the full
    # aggregate; leaving one out of the map must never silently publish everything.
    slices = kb.derive_slices(_slice_aggregate(), {"AllStaff": ["AllStaff"]})
    ok = slices["allstaff"] == [{"id": "a-base", "title": "a-base", "domain": "allstaff",
                                 "status": "published", "source_repo": "allstaff"}] \
        and slices["technical"] == [] and slices["commercial"] == [] and slices["hr"] == []
    return ok, f"technical={slices['technical']} commercial={slices['commercial']} hr={slices['hr']}"


@check
def slice_entries_carry_source_repo():
    slices = kb.derive_slices(_slice_aggregate(), _AUDIENCES)
    ok = all("source_repo" in e for v in slices.values() for e in v)
    return ok, f"all_have_source_repo={ok}"


# --- export: neutral bundle -------------------------------------------------

def _nugget_dict(**meta) -> dict:
    """A {meta, body, path} nugget for the pure export renderers (no file, no store gate)."""
    body = meta.pop("_body", "A neutral body line, comfortably human.")
    base = {"id": "x-note", "title": "X note", "status": "published",
            "provenance_type": "attestation", "attested_by": "Example Engineer",
            "attested_on": "2020-01-01", "verified": "2020-01-01"}
    base.update(meta)
    return {"meta": base, "body": body, "path": Path("x-note.md")}


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@check
def export_single_repo_bundle_matches_nuggets():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bundle"
        p = run("export", str(FIXTURE_KB), "--out", str(out))
        nuggets = kb._load_nuggets(FIXTURE_KB)
        manifest = json.loads((out / "bundle.json").read_text()) if (out / "bundle.json").exists() else {}
        docs = sorted((out / "docs").glob("*.md")) if (out / "docs").exists() else []
        ok = (p.returncode == 0 and len(docs) == len(nuggets)
              and len(manifest.get("docs", [])) == len(nuggets)
              and manifest.get("target_neutral") is True and manifest.get("format") == "markdown"
              and all(dm.get("path", "").startswith("docs/") and dm.get("acl") for dm in manifest["docs"]))
    return ok, f"rc={p.returncode} docs={len(docs)} nuggets={len(nuggets)} manifest_docs={len(manifest.get('docs', []))}"


@check
def export_markdown_carries_body_provenance_and_staleness():
    md = kb._render_doc_markdown(
        _nugget_dict(title="VPN reset", provenance_type="reference", source="runbooks/vpn.md",
                     attested_by=None, attested_on=None, verified="2020-01-01",
                     _body="Steps to reset the VPN token."),
        _NOW)
    ok = ("# VPN reset" in md and "Source: runbooks/vpn.md" in md
          and "Steps to reset the VPN token." in md
          and "Note: this document has not been audited recently." in md)
    return ok, f"md={md!r}"


@check
def export_html_escapes_and_converts_blocks():
    html_out = kb._render_doc_html(
        _nugget_dict(title="Danger", _body="<script>alert(1)</script>\n\n# Sub\n\n- one\n- two"),
        _NOW)
    escaped = "&lt;script&gt;" in html_out and "<script>" not in html_out
    converted = "<h1>" in html_out and "<li>one</li>" in html_out and "<li>two</li>" in html_out
    return escaped and converted, f"escaped={escaped} converted={converted}"


@check
def export_acl_bundles_are_scoped_no_leak():
    agg = _slice_aggregate()
    slices = kb.derive_slices(agg, _AUDIENCES)
    id_to_nugget = {e["id"]: _nugget_dict(id=e["id"], title=e["id"]) for e in agg["entries"]}
    per_audience = {}
    for key, entries in slices.items():
        audience = agg["repos"][key]["audience"]
        docs_meta, files = kb._assemble_bundle(audience, entries, id_to_nugget, _NOW, "markdown")
        per_audience[key] = ({dm["id"] for dm in docs_meta},
                             {dm["acl"] for dm in docs_meta},
                             set(files.keys()))
    tech_ids, tech_acl, tech_files = per_audience["technical"]
    allstaff_ids = per_audience["allstaff"][0]
    ok = (tech_ids == {"a-base", "t-net"} and allstaff_ids == {"a-base"}
          and "t-net" not in allstaff_ids and tech_acl == {"Technical"}
          and tech_files == {"docs/a-base.md", "docs/t-net.md"})
    return ok, f"technical={tech_ids} allstaff={allstaff_ids} acl={tech_acl}"


@check
def export_rendered_bundle_has_no_emdash():
    # Build the em-dash from its code point so this source file stays em-dash-clean itself.
    emdash = chr(0x2014)
    md = kb._render_doc_markdown(_nugget_dict(), _NOW)
    html_out = kb._render_doc_html(_nugget_dict(status="draft"), _NOW)
    ok = emdash not in md and emdash not in html_out
    return ok, f"md_clean={emdash not in md} html_clean={emdash not in html_out}"


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
