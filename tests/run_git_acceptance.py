#!/usr/bin/env python3
"""Git-backed acceptance for the manifest-driven aggregate (and, later, sync).

Builds throwaway bare "remote" repos with file:// URLs plus a temp config.toml / repos.toml, so it needs NO
network, NO credentials, and NO dependency on the real control home. It exercises `kb.py index --manifest`
end to end: clone-cache population, per-repo HEAD sha, and the aggregate shape.

Needs git on PATH and Python 3.11+ (the manifest path parses TOML via tomllib). On the dev Mac run it with
`/opt/homebrew/bin/python3 tests/run_git_acceptance.py`; CI uses a 3.12 runner.

Exit: 0 if every check passes, 1 if any fails.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_PY = REPO_ROOT / "kb.py"

# In-process access for validating staged prescan candidates against the real store gate.
sys.path.insert(0, str(REPO_ROOT))
import kb as kb_mod  # noqa: E402


def kb(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(KB_PY), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def git(*args: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr.strip()}")
    return r


def nugget(nid: str, domain: str, title: str, body: str, source: str | None = None) -> str:
    return (
        "---\n"
        "schema_version: 1\n"
        f"id: {nid}\n"
        f"title: {title}\n"
        f"domain: {domain}\n"
        "type: reference\n"
        "status: published\n"
        "owner_gid: 0000000000000000\n"
        "owner_name: Example Owner\n"
        "provenance_type: reference\n"
        f"source: {source or 'https://example.invalid/docs/' + nid}\n"
        "confidence: high\n"
        "verified: 2020-01-01\n"
        "---\n"
        f"{body}\n"
    )


def make_remote(root: Path, key: str, files: dict[str, str]) -> str:
    """Create a bare remote seeded with the given files on `main`; return its file:// URL."""
    bare = root / f"{key}.git"
    git("init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    work = root / f"{key}-work"
    git("clone", "--quiet", str(bare), str(work))
    git("-C", str(work), "config", "user.email", "test@example.invalid")
    git("-C", str(work), "config", "user.name", "Test Harness")
    git("-C", str(work), "checkout", "--quiet", "-B", "main")
    for rel, text in files.items():
        f = work / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git("-C", str(work), "add", "-A")
    git("-C", str(work), "commit", "--quiet", "-m", "seed")
    git("-C", str(work), "push", "--quiet", "origin", "main")
    git("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
    return f"file://{bare}"


def write_manifest(admin: Path, cache: Path, remotes: dict[str, tuple[str, str]],
                   cache_ttl: int | None = None, seeds: dict[str, dict] | None = None) -> Path:
    """Write repos.toml + config.toml under admin/; return the config path.

    cache_ttl, when given, adds a [cache] fetch_ttl_seconds line so the git-fetch reuse cache is exercised.
    seeds, when given, adds [seed_sources.<key>] blocks (string or list values) for prescan checks.
    """
    lines = []
    seen: list = []
    for key, (url, audience) in remotes.items():
        lines += [f"[managed.{key}]", f'url = "{url}"', f'audience = "{audience}"', ""]
        if audience not in seen:
            seen.append(audience)
    # Derive the visibility map from the remotes: AllStaff sees only itself; every other audience sees the
    # AllStaff base plus its own area (the department model). Keeps existing AllStaff/Technical tests intact
    # while letting new tests add Commercial etc. and prove cross-department exclusion.
    lines += ["[audiences]"]
    for audience in seen:
        visible = f'["AllStaff", "{audience}"]' if audience != "AllStaff" else '["AllStaff"]'
        lines += [f'{audience} = {visible}']
    lines += [""]
    for key, spec in (seeds or {}).items():
        lines += [f"[seed_sources.{key}]"]
        for k, v in spec.items():
            if isinstance(v, list):
                lines += [f"{k} = [" + ", ".join(f'"{x}"' for x in v) + "]"]
            else:
                lines += [f'{k} = "{v}"']
        lines += [""]
    (admin / "repos.toml").write_text("\n".join(lines), encoding="utf-8")
    cfg = admin / "config.toml"
    cfg_text = (
        "[repos]\n"
        f'manifest = "{admin / "repos.toml"}"\n'
        f'workspace = "{cache}"\n'
    )
    if cache_ttl is not None:
        cfg_text += f"\n[cache]\nfetch_ttl_seconds = {cache_ttl}\n"
    cfg.write_text(cfg_text, encoding="utf-8")
    return cfg


def commit_to_remote(root: Path, key: str, writes: dict | None = None, removes: list | None = None) -> None:
    """Push a follow-up commit to a remote via its existing -work clone."""
    work = root / f"{key}-work"
    for rel, text in (writes or {}).items():
        f = work / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    for rel in (removes or []):
        (work / rel).unlink()
    git("-C", str(work), "add", "-A")
    git("-C", str(work), "commit", "--quiet", "-m", "update")
    git("-C", str(work), "push", "--quiet", "origin", "main")


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def index_manifest_builds_aggregate():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        remotes = {
            "allstaff": (
                make_remote(root, "allstaff", {
                    "shared/wifi.md": nugget("shared-wifi", "shared", "Office wifi", "Join the staff SSID."),
                    "company/name.md": nugget("company-name", "company", "Company", "Example Satellite Ltd."),
                }),
                "AllStaff",
            ),
            "technical": (
                make_remote(root, "technical", {
                    "technical/vpn.md": nugget("tech-vpn", "technical", "VPN", "Use the corporate VPN profile."),
                }),
                "Technical",
            ),
        }
        cfg = write_manifest(admin, cache, {k: v for k, v in remotes.items()})
        p = kb("index", "--manifest", "--config", str(cfg))
        if p.returncode != 0:
            return False, f"rc={p.returncode} stderr={p.stderr.strip()!r}"
        agg_path = admin / "registry-aggregate.json"
        if not agg_path.exists():
            return False, "aggregate file not written"
        agg = json.loads(agg_path.read_text())
        repos = agg.get("repos", {})
        entries = agg.get("entries", [])
        ok = (
            agg.get("schema_version") == 1
            and set(repos) == {"allstaff", "technical"}
            and all(r["status"] == "ok" and r["head_sha"] and len(r["head_sha"]) == 40 for r in repos.values())
            and repos["allstaff"]["audience"] == "AllStaff" and repos["technical"]["audience"] == "Technical"
            and len(entries) == 3
            and all(e.get("source_repo") in {"allstaff", "technical"} for e in entries)
            and all(e.get("content_hash") for e in entries)
            and {e["source_repo"] for e in entries} == {"allstaff", "technical"}
        )
        detail = f"repos={list(repos)} entries={len(entries)} sources={sorted({e.get('source_repo') for e in entries})}"
        # A second run must be idempotent (clone cache reused, same head shas).
        head_first = {k: r["head_sha"] for k, r in repos.items()}
        p2 = kb("index", "--manifest", "--config", str(cfg))
        agg2 = json.loads(agg_path.read_text())
        head_second = {k: r["head_sha"] for k, r in agg2["repos"].items()}
        ok = ok and p2.returncode == 0 and head_first == head_second
        return ok, detail + f" idempotent={head_first == head_second}"


@check
def index_manifest_flags_unreachable_repo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        good = make_remote(root, "allstaff", {
            "shared/x.md": nugget("shared-x", "shared", "X", "A fact that resolves offline."),
        })
        remotes = {"allstaff": (good, "AllStaff"), "ot": (f"file://{root / 'does-not-exist.git'}", "OT")}
        cfg = write_manifest(admin, cache, remotes)
        p = kb("index", "--manifest", "--config", str(cfg))
        agg = json.loads((admin / "registry-aggregate.json").read_text())
        repos = agg["repos"]
        ok = (
            p.returncode == 0
            and repos["allstaff"]["status"] == "ok"
            and repos["ot"]["status"].startswith("unreachable")
            and "ot" in p.stderr  # warned on stderr
            and all(e["source_repo"] == "allstaff" for e in agg["entries"])
        )
        return ok, f"ot_status={repos['ot']['status']!r} entries={len(agg['entries'])}"


@check
def sync_clean_then_detects_change_add_remove():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        url_a = make_remote(root, "allstaff", {
            "shared/wifi.md": nugget("shared-wifi", "shared", "Wifi", "Join the staff SSID to get online."),
            "shared/printer.md": nugget("shared-printer", "shared", "Printer", "Use the third-floor printer."),
        })
        url_it = make_remote(root, "technical", {
            "technical/vpn.md": nugget("tech-vpn", "technical", "VPN", "Use the corporate VPN profile from the pack."),
        })
        cfg = write_manifest(admin, cache, {"allstaff": (url_a, "AllStaff"), "technical": (url_it, "Technical")})
        if kb("index", "--manifest", "--config", str(cfg)).returncode != 0:
            return False, "baseline index failed"
        clean = kb("sync", "--config", str(cfg))
        # mutate allstaff: change wifi body, add a guide, remove the printer nugget
        commit_to_remote(
            root, "allstaff",
            writes={
                "shared/wifi.md": nugget("shared-wifi", "shared", "Wifi", "Join the NEW staff SSID and sign in once."),
                "shared/guide.md": nugget("shared-guide", "shared", "Guide", "A brand new starter guide entry."),
            },
            removes=["shared/printer.md"],
        )
        drift = kb("sync", "--config", str(cfg))
        out = drift.stdout
        ok = (
            clean.returncode == 0 and "clean" in clean.stdout
            and drift.returncode == 0 and "clean" not in out
            and "repo: allstaff" in out
            and "repo changed:" in out
            and "changed: shared-wifi" in out
            and "added: shared-guide" in out
            and "removed: shared-printer" in out
            and "repo: technical" not in out  # the unchanged repo produces no drift lines
        )
        return ok, f"clean={('clean' in clean.stdout)} drift_lines={[l for l in out.splitlines() if l.startswith('  - ')]}"


@check
def sync_flags_out_of_band_invalid_and_unmanaged():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        url_a = make_remote(root, "allstaff", {
            "shared/ok.md": nugget("shared-ok", "shared", "OK", "A properly grounded and sourced fact."),
        })
        cfg = write_manifest(admin, cache, {"allstaff": (url_a, "AllStaff")})
        kb("index", "--manifest", "--config", str(cfg))
        # an out-of-band edit that bypassed the store gate: a reference nugget with no source
        bad = (
            "---\nschema_version: 1\nid: shared-bad\ntitle: Bad\ndomain: shared\ntype: reference\n"
            "status: published\nowner_gid: 0000000000000000\nprovenance_type: reference\n"
            "confidence: low\nverified: 2020-01-01\n---\nAn ungrounded claim that never passed the gate.\n"
        )
        commit_to_remote(root, "allstaff", writes={"shared/bad.md": bad})
        # a clone in the cache dir that is not in the manifest
        git("clone", "--quiet", url_a, str(cache / "rogue"))
        p = kb("sync", "--config", str(cfg))
        out = p.stdout
        ok = (
            p.returncode == 0
            and "out-of-band invalid nugget 'shared-bad'" in out
            and "unmanaged" in out and "rogue" in out
        )
        return ok, f"stdout={out.strip()[:160]!r}"


@check
def cache_ttl_skips_fetch_within_window_and_force_refetches():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        url_a = make_remote(root, "allstaff", {
            "shared/wifi.md": nugget("shared-wifi", "shared", "Wifi", "Join the staff SSID to get online."),
        })
        cfg = write_manifest(admin, cache, {"allstaff": (url_a, "AllStaff")})
        agg_path = admin / "registry-aggregate.json"

        # Baseline: fetch S0 and stamp the cache.
        if kb("index", "--manifest", "--config", str(cfg)).returncode != 0:
            return False, "baseline index failed"
        s0 = json.loads(agg_path.read_text())["repos"]["allstaff"]["head_sha"]

        # Remote moves on (S1): a new nugget appears upstream.
        commit_to_remote(root, "allstaff", writes={
            "shared/guide.md": nugget("shared-guide", "shared", "Guide", "A brand new starter guide entry."),
        })

        # Within the window: the fetch is skipped, so the aggregate still shows S0 and misses the new nugget.
        kb("index", "--manifest", "--config", str(cfg), "--max-age", "3600")
        agg_cached = json.loads(agg_path.read_text())
        s_cached = agg_cached["repos"]["allstaff"]["head_sha"]
        ids_cached = {e["id"] for e in agg_cached["entries"]}

        # --force ignores the cache: S1 and the new nugget appear.
        kb("index", "--manifest", "--config", str(cfg), "--force")
        agg_forced = json.loads(agg_path.read_text())
        s1 = agg_forced["repos"]["allstaff"]["head_sha"]
        ids_forced = {e["id"] for e in agg_forced["entries"]}

        # Remote moves again (S2); --max-age 0 must fetch (a reuse would still report S1).
        commit_to_remote(root, "allstaff", writes={
            "shared/faq.md": nugget("shared-faq", "shared", "FAQ", "Another fresh entry for the zero test."),
        })
        kb("index", "--manifest", "--config", str(cfg), "--max-age", "0")
        s2 = json.loads(agg_path.read_text())["repos"]["allstaff"]["head_sha"]

        ok = (
            bool(s0) and s_cached == s0 and "shared-guide" not in ids_cached
            and s1 != s0 and "shared-guide" in ids_forced
            and s2 != s1
        )
        detail = (f"s0={s0[:7] if s0 else s0} cached={s_cached[:7]} "
                  f"s1={s1[:7]} s2={s2[:7]} guide_cached={'shared-guide' in ids_cached}")
        return ok, detail


@check
def sync_always_fetches_ignoring_ttl():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        url_a = make_remote(root, "allstaff", {
            "shared/ok.md": nugget("shared-ok", "shared", "OK", "A properly grounded and sourced fact."),
        })
        # A huge TTL that would suppress an index fetch must NOT suppress sync's drift check.
        cfg = write_manifest(admin, cache, {"allstaff": (url_a, "AllStaff")}, cache_ttl=99999)
        if kb("index", "--manifest", "--config", str(cfg)).returncode != 0:
            return False, "baseline index failed"
        commit_to_remote(root, "allstaff", writes={
            "shared/new.md": nugget("shared-new", "shared", "New", "A change sync must catch despite the TTL."),
        })
        p = kb("sync", "--config", str(cfg))
        out = p.stdout
        ok = (
            p.returncode == 0 and "clean" not in out
            and "repo: allstaff" in out and "added: shared-new" in out
        )
        return ok, f"stdout={out.strip()[:160]!r}"


@check
def cache_subcommand_reports_and_clears():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        url_a = make_remote(root, "allstaff", {
            "shared/ok.md": nugget("shared-ok", "shared", "OK", "A properly grounded and sourced fact."),
        })
        cfg = write_manifest(admin, cache, {"allstaff": (url_a, "AllStaff")})
        if kb("index", "--manifest", "--config", str(cfg)).returncode != 0:
            return False, "index failed"
        rep = kb("cache", "--config", str(cfg))
        clr = kb("cache", "--config", str(cfg), "--clear")
        empt = kb("cache", "--config", str(cfg))
        ok = (
            rep.returncode == 0 and "git_fetch" in rep.stdout
            and clr.returncode == 0 and "cleared" in clr.stdout
            and empt.returncode == 0 and "empty" in empt.stdout
        )
        return ok, f"report_has_git_fetch={'git_fetch' in rep.stdout} cleared={'cleared' in clr.stdout} empty={'empty' in empt.stdout}"


# --- prescan (seed sources -> ranked pointer candidates) ---------------------

CANARY = "CANARY-SECRET-VALUE-XYZZY-DO-NOT-PRINT"


def _seed_dir(root: Path) -> Path:
    """A plain (non-git) path seed source with prose, a secret canary, a binary, and an id collision."""
    seed = root / "seed-src"
    guide = (
        "# Setting up the estate\n\n"
        "The first paragraph explains the setup " + chr(0x2014) + " including the awkward punctuation "
        "the voice gate must never let through into a staged candidate abstract.\n\n"
        "## Second heading\n\nMore detail follows in later sections.\n"
    )
    files = {
        "guides/setup.md": guide,
        "a/notes.md": "Some brief notes that still clear the trivial threshold comfortably.\n",
        "a-notes.md": "A different file whose slug collides with the nested notes file above.\n",
        "config.py": f"token = '{CANARY}'\n",
        "deploy-secrets.md": f"# Secrets\n\n{CANARY}\n",
    }
    for rel, text in files.items():
        f = seed / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    (seed / "broken-export.md").write_bytes(b"\x00\x01binary blob masquerading as markdown")
    return seed


@check
def prescan_dry_run_reports_and_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        seed = _seed_dir(root)
        cfg = write_manifest(admin, root / "cache", {}, seeds={"estate": {"path": str(seed)}})
        p = kb("prescan", "--config", str(cfg))
        counts_ok = ("candidates=3" in p.stdout and "skipped-secret=2" in p.stdout
                     and "skipped-binary=1" in p.stdout)
        no_canary = CANARY not in p.stdout and CANARY not in p.stderr
        no_writes = not (admin / "prescan-out").exists() and not (admin / "prescan-report.json").exists()
        ok = p.returncode == 0 and counts_ok and no_canary and no_writes
        return ok, f"rc={p.returncode} counts_ok={counts_ok} no_canary={no_canary} no_writes={no_writes}"


@check
def prescan_commit_refuses_without_owner():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        seed = _seed_dir(root)
        cfg = write_manifest(admin, root / "cache", {}, seeds={"estate": {"path": str(seed)}})
        p = kb("prescan", "--config", str(cfg), "--commit")
        ok = (p.returncode == 2 and "owner" in p.stderr
              and not (admin / "prescan-out").exists() and not (admin / "prescan-report.json").exists())
        return ok, f"rc={p.returncode} stderr={p.stderr.strip()[:100]!r}"


@check
def prescan_commit_stages_gate_clean_candidates():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        seed = _seed_dir(root)
        cfg = write_manifest(admin, root / "cache", {},
                             seeds={"estate": {"path": str(seed), "domain": "shared", "audience": "Technical"}})
        p = kb("prescan", "--config", str(cfg), "--commit",
               "--owner-gid", "0000000000000000", "--owner-name", "Example Owner")
        if p.returncode != 0:
            return False, f"rc={p.returncode} stderr={p.stderr.strip()[:160]!r}"
        staged = sorted((admin / "prescan-out" / "estate").glob("*.md"))
        names = [f.name for f in staged]
        problems = []
        titles = {}
        for f in staged:
            meta, body = kb_mod.parse_frontmatter(f.read_text(encoding="utf-8"))
            errors = kb_mod.validate_entry(meta, body)
            if errors:
                problems.append(f"{f.name}: {errors}")
            if meta.get("status") != "draft" or meta.get("provenance_type") != "reference" or not meta.get("source"):
                problems.append(f"{f.name}: wrong draft/reference/source shape")
            if chr(0x2014) in body:
                problems.append(f"{f.name}: em-dash reached the staged abstract")
            titles[f.name] = meta.get("title")
        report = json.loads((admin / "prescan-report.json").read_text())
        est = report["sources"]["estate"]
        ok = (
            len(staged) == 3
            and not problems
            and not any("config" in n or "secret" in n for n in names)
            and len(set(names)) == 3  # the a/notes.md vs a-notes.md collision got distinct ids
            and "Setting up the estate" in titles.values()
            and est["counts"]["skipped_secret"] == 2
            and est["counts"]["candidates"] == 3
        )
        return ok, f"staged={names} problems={problems[:2]} titles={sorted(titles.values())}"


@check
def prescan_url_seed_dedups_against_registry():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        seed_url = make_remote(root, "seedkb", {
            "runbooks/triage.md": "# Network triage\n\nStart from the router and work outward to hosts.\n",
            "runbooks/backups.md": "# Backup checks\n\nConfirm the nightly capture landed before changes.\n",
        })
        data_url = make_remote(root, "allstaff", {
            "shared/wifi.md": nugget("shared-wifi", "shared", "Wifi", "Join the staff SSID to get online."),
        })
        cfg = write_manifest(admin, cache, {"allstaff": (data_url, "AllStaff")},
                             seeds={"docs": {"url": seed_url}})
        owner = ("--owner-gid", "0000000000000000", "--owner-name", "Example Owner")
        if kb("index", "--manifest", "--config", str(cfg)).returncode != 0:
            return False, "baseline index failed"
        p1 = kb("prescan", "--config", str(cfg), "--commit", *owner)
        staged1 = sorted((admin / "prescan-out" / "docs").glob("*.md"))
        if p1.returncode != 0 or len(staged1) != 2:
            return False, f"first commit rc={p1.returncode} staged={len(staged1)}"
        report1 = json.loads((admin / "prescan-report.json").read_text())
        if not report1["sources"]["docs"].get("head_sha"):
            return False, "url seed did not record a head sha"
        # Land one staged candidate's source as a real nugget, re-index, and rescan: it must dedup out.
        triage = next(f for f in staged1 if "triage" in f.name)
        meta, _ = kb_mod.parse_frontmatter(triage.read_text(encoding="utf-8"))
        commit_to_remote(root, "allstaff", writes={
            "shared/triage-pointer.md": nugget("shared-triage-pointer", "shared", "Triage pointer",
                                               "Pointer to the triage runbook in the docs seed.",
                                               source=meta["source"]),
        })
        if kb("index", "--manifest", "--config", str(cfg), "--force").returncode != 0:
            return False, "re-index failed"
        p2 = kb("prescan", "--config", str(cfg), "--commit", *owner)
        staged2 = [f.name for f in sorted((admin / "prescan-out" / "docs").glob("*.md"))]
        report2 = json.loads((admin / "prescan-report.json").read_text())
        counts2 = report2["sources"]["docs"]["counts"]
        ok = (
            p2.returncode == 0
            and counts2["already_captured"] == 1 and counts2["candidates"] == 1
            and len(staged2) == 1 and "triage" not in staged2[0]
        )
        return ok, f"counts2={counts2} staged2={staged2}"


@check
def prescan_repo_level_pointer_does_not_suppress():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        seed_url = make_remote(root, "seedkb", {
            "runbooks/triage.md": "# Network triage\n\nStart from the router and work outward to hosts.\n",
        })
        data_url = make_remote(root, "allstaff", {
            "shared/kb-pointer.md": nugget("shared-kb-pointer", "shared", "Estate KB",
                                           "Whole-repo pointer to the seed knowledge base.",
                                           source=seed_url),
        })
        cfg = write_manifest(admin, cache, {"allstaff": (data_url, "AllStaff")},
                             seeds={"docs": {"url": seed_url}})
        if kb("index", "--manifest", "--config", str(cfg)).returncode != 0:
            return False, "baseline index failed"
        p = kb("prescan", "--config", str(cfg))
        ok = (p.returncode == 0 and "repo-level pointer" in p.stdout
              and "candidates=1" in p.stdout and "runbooks/triage.md" in p.stdout)
        return ok, f"rc={p.returncode} stdout={p.stdout.strip()[:200]!r}"


@check
def prescan_path_seed_skips_clone():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        make_remote(root, "seedkb", {
            "runbooks/triage.md": "# Network triage\n\nStart from the router and work outward to hosts.\n",
        })
        work = root / "seedkb-work"  # the existing local clone; prescan must not clone its own copy
        cfg = write_manifest(admin, root / "cache", {}, seeds={"local": {"path": str(work)}})
        p = kb("prescan", "--config", str(cfg), "--commit",
               "--owner-gid", "0000000000000000", "--owner-name", "Example Owner")
        report = json.loads((admin / "prescan-report.json").read_text())
        src = report["sources"]["local"]
        no_clone = not (admin / "cache" / "seeds").exists() and not (root / "cache").exists()
        ok = (p.returncode == 0 and src["status"] == "ok" and bool(src.get("head_sha"))
              and src["counts"]["candidates"] == 1 and no_clone)
        return ok, f"rc={p.returncode} head={bool(src.get('head_sha'))} no_clone={no_clone}"


@check
def index_publish_slices_are_scoped_idempotent_and_clean():
    """index --manifest --publish commits each repo its audience slice, is a no-op re-run, and sync stays clean.

    Three repos across three audiences. The Technical slice must carry the AllStaff base plus its own entry;
    the AllStaff slice must carry only AllStaff (no Technical/Commercial leak). A second publish must push no
    new commit (the slice is byte-identical bar the timestamp, which is not compared). And `sync` after a
    publish must report clean: publishing moves each repo's HEAD, but index records the post-publish sha into
    the aggregate baseline, so there is no spurious "repo changed".
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        admin = root / "admin"; admin.mkdir()
        cache = root / "cache"
        url_a = make_remote(root, "allstaff", {
            "shared/wifi.md": nugget("shared-wifi", "shared", "Wifi", "Join the staff SSID to get online."),
        })
        url_t = make_remote(root, "technical", {
            "technical/net.md": nugget("tech-net", "technical", "Net", "The core switch lives in rack 3."),
        })
        url_c = make_remote(root, "commercial", {
            "commercial/plan.md": nugget("comm-plan", "commercial", "Plan", "The GS service plan tiers."),
        })
        remotes = {"allstaff": (url_a, "AllStaff"), "technical": (url_t, "Technical"),
                   "commercial": (url_c, "Commercial")}
        cfg = write_manifest(admin, cache, remotes)

        pub = kb("index", "--manifest", "--publish", "--config", str(cfg))
        if pub.returncode != 0 or "publish technical: published" not in pub.stdout:
            return False, f"publish rc={pub.returncode} stdout={pub.stdout.strip()!r}"

        # A fresh clone of each remote proves the slice was pushed, not just written locally.
        def remote_ids(url: str, key: str) -> set:
            vdir = root / f"verify-{key}"
            git("clone", "--quiet", url, str(vdir))
            reg = json.loads((vdir / "registry.json").read_text(encoding="utf-8"))
            return {e["id"] for e in reg["entries"]}, reg.get("audience")

        tech_ids, tech_aud = remote_ids(url_t, "technical")
        all_ids, all_aud = remote_ids(url_a, "allstaff")
        tech_scoped = tech_ids == {"shared-wifi", "tech-net"} and tech_aud == "Technical"
        allstaff_scoped = all_ids == {"shared-wifi"} and all_aud == "AllStaff"  # no Technical/Commercial leak

        # Second publish is a no-op: the technical remote HEAD must not move.
        head1 = git("ls-remote", url_t, "main").stdout.split()[0]
        pub2 = kb("index", "--manifest", "--publish", "--config", str(cfg))
        head2 = git("ls-remote", url_t, "main").stdout.split()[0]
        idempotent = (pub2.returncode == 0 and head1 == head2
                      and "publish technical: unchanged" in pub2.stdout)

        # Sync after publish must be clean (no spurious "repo changed" from the manager's own slice commits).
        s = kb("sync", "--config", str(cfg))
        clean = s.returncode == 0 and "clean" in s.stdout and "repo changed" not in s.stdout

        ok = tech_scoped and allstaff_scoped and idempotent and clean
        return ok, (f"tech={tech_scoped} allstaff={allstaff_scoped} idempotent={idempotent} "
                    f"clean={clean} sync={s.stdout.strip()!r}")


def main() -> int:
    failures = 0
    for fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        print(f"  {'PASS' if ok else 'FAIL'}  {fn.__name__}    {detail}")
        if not ok:
            failures += 1
    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
