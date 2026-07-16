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

    def __init__(self, sections=None, tasks=None, fields=None, settings=None):
        self._sections = [dict(s) for s in (sections or [])]
        self._tasks = [dict(t) for t in (tasks or [])]
        self._fields = [dict(f) for f in (fields or [])]
        self._settings = [dict(s) for s in (settings or [])]
        self.writes = []
        self._counter = 900000

    def _mint(self):
        self._counter += 1
        return str(self._counter)

    def get_all(self, path, params=None):
        if path.startswith("/projects/") and path.endswith("/sections"):
            return [dict(s) for s in self._sections]
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
        if path.startswith("/tasks/") and "completed" in body:
            gid = path.split("/")[2]
            for t in self._tasks:
                if t.get("gid") == gid:
                    t["completed"] = body["completed"]
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
