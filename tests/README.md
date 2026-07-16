# kb.py acceptance harness

Reproducible acceptance for the Phase 1a core of `kb.py`. Stdlib only, no third-party test runner, no
network, deterministic.

## Run

    python3 tests/run_acceptance.py

Exit code 0 means every check passed; a non-zero code means at least one failed, and the failing checks are
listed. Any Python 3 works: the core commands import `tomllib` lazily, so the harness never needs 3.11+.

## What it covers

- **store**: valid reference and valid attestation nuggets pass the gate; a reference with no source, an
  em-dash in the body, and a missing required field are each refused (exit 1, `REFUSED`).
- **store --into**: writes the nugget to `<repo>/<domain>/<id>.md`.
- **index**: emits a registry whose entries each carry a content hash and a path, one per fixture nugget.
- **answer**: a matching query returns the nugget body with a `[Source: ...]` citation; an unmatched query
  returns exactly the miss phrase (borrowed from `kb.MISS_RESPONSE`, so the test cannot drift from the code).
- **rot**: flags the shared-source pair as Redundant and the stale nuggets as Outdated, grouped by owner; a
  fresh single-nugget repo reports clean.
- **feedback (finding set)**: gap + conflict signals from the interaction log and ROT flags from a `--repo`
  sweep become findings with owned `KB-*` ids and stable keys; the active-family gate reports a skipped
  surface rather than a false-empty one; `--log` appends a well-formed record.
- **feedback reconcile (`--commit`)**: driven in-process against a `FakeAsanaClient` (dict-backed, records
  every write). Covers create (with owner/triage assignee, filed into the KB section), no-op (zero writes),
  verify-clear, reopen, the active-family gate (an uncollected family is left untouched), isolation (a
  `[Build]`/`[Chip]`/sibling `[L017]` task is never matched or written), dry-run (zero writes), the
  Verification enum set vs comment-degrade, `owner_gid` threading, PAT resolution order + error (and the PAT
  never appearing in an error message), and the title round-trip for keys containing `] `, `|`, or an
  over-length subject.

## Live-only checks (NUC, not in this harness)

Live Asana writes cannot run in the offline harness. Run these once by hand on the NUC (Python 3.13.5) with a
real `[tracking.pat]` in `config.toml`, against the KB Findings section of the tracking project:

- `kb.py feedback --repo <repo> --commit` creates the expected tasks; an immediate second run is a clean
  **zero-write no-op** (steady state).
- A human-closed KB task whose condition still exists is **reopened** on the next `--commit` run.
- The shared `Verification` field attaches to the tracking project (or, if absent, the run degrades to a task
  comment without aborting).
- 429 backoff is exercised opportunistically under load.

**Operational note:** the interaction log (`logs/interactions.jsonl`) must be **append-only, not rotated or
truncated between reconcile runs**. A gap that is absent from a truncated log looks resolved and would be
verify-cleared.

## Fixtures

`tests/fixtures/kb/` holds a small, generic KB (fictional content, placeholder owner, no real identifier).
The adversarial refusal cases and the fresh rot-clean case are generated at runtime in a temp directory, so
no deliberately-invalid or stale-dated file sits in the tree (and nothing here carries an em-dash for a
future voice hook to trip on).
