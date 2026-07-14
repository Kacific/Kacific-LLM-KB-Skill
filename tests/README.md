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
- **stub boundary**: `sync` still reports "not yet implemented (Phase 1b)".

## Fixtures

`tests/fixtures/kb/` holds a small, generic KB (fictional content, placeholder owner, no real identifier).
The adversarial refusal cases and the fresh rot-clean case are generated at runtime in a temp directory, so
no deliberately-invalid or stale-dated file sits in the tree (and nothing here carries an em-dash for a
future voice hook to trip on).
