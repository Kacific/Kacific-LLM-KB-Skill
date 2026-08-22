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
kb.py cache            inspect or clear the local, gitignored TTL lookup cache
kb.py feedback         append a usage/rating/miss record, or report gap/ROT/conflict findings (--commit
                       reconciles them into the tracking project's KB Findings section)
kb.py coordination-audit  close opt-in coordination tasks whose tracked KB findings have all cleared
                       (--commit closes them; default is a read-only dry-run)
kb.py sync             drift-detect the managed repos against the recorded aggregate
kb.py prescan          scan the manifest's seed sources into ranked pointer candidates (--commit stages
                       them for review; keepers land via the normal store gate)
kb.py export           (Phase 1b)
```

Stdlib-first Python. Copy `config.example.toml` to `config.toml` (gitignored) and fill in the locations for
your deployment.

## Closing a coordination task automatically (`coordination-audit`)

`kb.py feedback` closes its own `[KB-*]` finding tasks in the KB Findings section by re-discovery, but it is
section-scoped by design and never touches the `[Build]`/`[Chip]`/`[KB]` coordination tasks that sit in the
tracking project's default section. A coordination task that merely *tracks* a batch of findings (for example
"clear the KB-ROT backlog") therefore has to be closed by a human once its findings clear. Wording such a
task "awaiting auto-verify" is a category error: nothing was watching it.

`coordination-audit` is the opt-in mechanism for that case. A coordination task opts in by putting a
machine-readable anchor on the **first non-empty line** of its notes:

```
closes-when-cleared: KB-ROT-OUTDATED, KB-ROT-REDUNDANT
```

On each run the tool closes the task once **no open `[KB-*]` finding task carrying any of those ids remains**
in the KB Findings section, leaving a comment that names the cleared ids as evidence. Ids may be comma- or
whitespace-separated. Default behaviour is unchanged: a task with no anchor is never touched, and coordination
tasks without one still close by human judgement on landing.

Safe by construction: it never routes through the `feedback` reconcile, never touches a `[KB-*]` finding task,
never closes on an unreadable findings section, never closes on an unrecognised declared id or one that has no
finding in the section at all, and re-checks each task's live state immediately before writing (the shared PAT
means a peer session may have closed it already). It reuses the existing `[tracking]` config; no new keys.
