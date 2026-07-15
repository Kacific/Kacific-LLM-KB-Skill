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


def nugget(nid: str, domain: str, title: str, body: str) -> str:
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
        "source: https://example.invalid/docs/" + nid + "\n"
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


def write_manifest(admin: Path, cache: Path, remotes: dict[str, tuple[str, str]]) -> Path:
    """Write repos.toml + config.toml under admin/; return the config path."""
    lines = []
    for key, (url, audience) in remotes.items():
        lines += [f"[managed.{key}]", f'url = "{url}"', f'audience = "{audience}"', ""]
    lines += ["[audiences]", 'AllStaff = ["AllStaff"]', 'IT = ["AllStaff", "IT"]', ""]
    (admin / "repos.toml").write_text("\n".join(lines), encoding="utf-8")
    cfg = admin / "config.toml"
    cfg.write_text(
        "[repos]\n"
        f'manifest = "{admin / "repos.toml"}"\n'
        f'workspace = "{cache}"\n',
        encoding="utf-8",
    )
    return cfg


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
            "it": (
                make_remote(root, "it", {
                    "it/vpn.md": nugget("it-vpn", "it", "VPN", "Use the corporate VPN profile."),
                }),
                "IT",
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
            and set(repos) == {"allstaff", "it"}
            and all(r["status"] == "ok" and r["head_sha"] and len(r["head_sha"]) == 40 for r in repos.values())
            and repos["allstaff"]["audience"] == "AllStaff" and repos["it"]["audience"] == "IT"
            and len(entries) == 3
            and all(e.get("source_repo") in {"allstaff", "it"} for e in entries)
            and all(e.get("content_hash") for e in entries)
            and {e["source_repo"] for e in entries} == {"allstaff", "it"}
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
