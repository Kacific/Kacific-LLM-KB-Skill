# Kacific LLM KB manager (control plane)

This repository is the public, generic machinery of the Kacific LLM knowledge-base manager. It holds the
CLI (`kb.py`), the entry schema (`schema/kb-entry.md`), a config template, and the agent onboarding seed. It
carries no internal identifiers; every deployment specific (repo paths, the tracking workspace, credential
locations) lives in a private control home and in a gitignored `config.toml`.

## What the manager is

One agent that other agents (Claude or otherwise) and humans consult for facts about the company and its
work. It stores knowledge once, with provenance, and serves it back. It is a control plane over existing
sources of truth, not a second copy of them.

## Use this KB through the manager, and no other path

- To READ: refresh your local copy first (`git fetch --prune && git pull --ff-only`), then read the registry
  and the nuggets, or ask the manager.
- To ADD or CHANGE knowledge: go through the manager (the `kacific-kb` skill or `kb.py store`). Never edit a
  nugget by hand. Direct edits are detected as drift and flagged.
- Every fact must carry a reference or a named human attestation. The manager refuses to store anything it
  cannot back.

## kb.py

```
kb.py store <file>     validate a nugget against the schema and the anti-hallucination gate, then store it
kb.py index <repo>     rebuild the registry from a repo
kb.py answer <query>   answer from stored nuggets only, with grounding and citation
kb.py rot              hygiene sweep for redundant, outdated, or trivial nuggets
kb.py sync|prescan|export|feedback   (Phase 1b and operations)
```

Stdlib-first Python. Copy `config.example.toml` to `config.toml` (gitignored) and fill in the locations for
your deployment.
