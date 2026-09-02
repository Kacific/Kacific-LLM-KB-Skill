# KB entry schema (the format SSOT)

Every knowledge nugget is a Markdown file with a YAML frontmatter block, then a Markdown body. The
frontmatter is machine-parseable by any agent (not Claude-specific), renders in SharePoint, Confluence, and
Box, and is diffable in git. This file is the single source of truth for that format. It is generic on
purpose: it carries no site-specific identifiers.

## Frontmatter fields

```yaml
---
schema_version: 1              # integer; bump only for a breaking change (see forward-compatibility below)
id: <stable-kebab-slug>        # unique within the KB; never reused
title: <human title>           # searchable, plain language, no internal jargon
domain: technical | commercial | admin | finance | hr | shared | company
type: how-to | troubleshooting | faq | known-issue | reference | fact | glossary
status: draft | published | needs-update | archived | retired
owner_gid: <asana-user-gid>    # a real, routable owner (see ownership)
owner_name: <display name>
provenance_type: reference | attestation   # MANDATORY, one or the other
source: <path | url>           # for provenance_type: reference. A stored-doc path, an SSOT path, or a URL
attested_by: <asana-user-gid>  # for provenance_type: attestation. The person who vouched
attested_on: <YYYY-MM-DD>      # for attestation, ISO-8601 date (UTC)
confidence: low | medium | high
verified: <YYYY-MM-DD | unverified>   # last human verification; ISO-8601 UTC
verified_by: <asana-user-gid>  # OPTIONAL for now: the person who did that verification. See below
supersedes: <id | null>        # the id this nugget replaces, if any
related: [<id>, ...]           # companion nuggets, by id (relative links)
tags: [<tag>, ...]
---
```

## The anti-hallucination gate (non-negotiable)

A nugget is only valid if it carries EITHER:
- `provenance_type: reference` with a non-empty `source` that resolves (a stored doc under `sources/`, an
  SSOT path, or a URL), OR
- `provenance_type: attestation` with `attested_by` and `attested_on` set (a named person vouching, which is
  how tribal knowledge enters).

An entry with neither is refused at store time, not written. The manager never originates its own unsourced
facts.

## Pointer entries

A pointer entry indexes a document whose single source of truth lives elsewhere. It is a normal nugget
through the same gate: `provenance_type: reference` with `source` set to the document's URL or path,
pinned to the source repo's HEAD at scan time where derivable (a GitHub blob URL). The body is a one-line
abstract only; a pointer never copies source content.

`kb.py prescan` generates pointer candidates in bulk from the manifest's seed sources. Candidates are
`status: draft` with the reviewing human as owner, and they enter the KB only through the normal
`store --into` gate after review. A repo-root pointer records that a repo is covered at repo level; it does
not preclude finer per-file pointers into the same repo.

## Ownership

`owner_gid` is a real, routable identity (an active user in the tracking workspace), not free text, so the
periodic review loop can assign a re-verification task to a real person. When the owner is not determinable
from the source at ingest time, ask, do not guess.

## Lifecycle (status)

`draft -> published -> needs-update -> archived -> retired`. A retired nugget is kept for a grace window
before removal so that old searches still land on it.

## Voice

All prose (title, body, and any human-readable field) is written for humans, not as AI output. British and
Pacific English spelling. No em-dashes anywhere; use commas, semicolons, parentheses, or full stops. Plain
cadence, no AI tells.

## Timestamps

`verified`, `attested_on`, and any stored timestamp are ISO-8601 UTC off the live clock. Human-facing
displays may localise, but stored and compared values are UTC.

## Forward-compatibility

The frontmatter is a persisted contract read by `kb.py` and by non-Claude agents. Within a `schema_version`,
changes are additive only. A rename goes add, double-write, flip readers, then drop, and bumps
`schema_version` only when a reader-breaking change is unavoidable.

## The registry (derived, not stored in frontmatter)

The manager computes, per nugget, a content hash (over the body only, excluding manager-managed fields such
as `verified`, so the manager's own writes never look like drift), the source repo, and the last-seen commit
SHA. These live in the registry, not in the nugget frontmatter.

`last_used` is another derived, registry-only field: the most recent time a real query cited the nugget in a
hit answer (from the interaction log), or `null` until any usage is seen. It carries the "in active use"
signal to the hygiene sweep, which holds back the Outdated flag for a nugget used inside the usage window
until a hard ceiling age. It is never a substitute for `verified`: usage is not human confirmation, so
`verified` stays a human-only field and a nugget past the ceiling is flagged regardless of use.

## `verified_by`: who did the verifying, and why a bare date could not say

`verified` records WHEN. On its own it cannot record WHETHER. A date written after a person read the body
against its source, and a date written by a process that read nothing, are **byte-identical**, so the
unearned one is indistinguishable from the earned one for ever.

The hygiene sweep cannot close that gap, and it is worth being precise about why. `rot` flags a date for
being too OLD. Nothing anywhere flags a date for being unearned, because there is nothing in the record to
flag. The error detection is therefore **one-directional by construction**:

- an **under-claimed** date (older than the truth) gets the nugget flagged and re-read, so it self-corrects;
- an **over-claimed** date (newer than the truth) suppresses the flag for `ROT_OUTDATED_DAYS` and is simply
  believed, so it does not.

Only one of the two errors corrects itself, which is why every tie breaks toward leaving the field alone,
and why the fix is to record the verifier rather than to write better guidance about when to bump.

`verified_by` names the person. It is **optional for now**, deliberately: every nugget that exists predates
the field, and nobody can honestly backfill who verified them, so requiring it immediately would force
either a mass grandfather or a mass re-read. The staged path is additive-first:

1. **Now.** The field is optional. `kb.py verify-audit` reports every nugget claiming a `verified` date
   while naming nobody. `validate_entry` checks coherence only (`verified_by` set with no date is refused)
   and refuses nothing that exists today.
2. **Next.** New and re-verified nuggets carry `verified_by`, so the unattributed count falls as real
   verifications happen rather than through a migration.
3. **Then.** Once `verify-audit` reports a workable remainder, `validate_entry` starts refusing a dated
   `verified` with no `verified_by`, and this section records that the flip has happened.

Until step 3, read an unattributed `verified` date as what it is: a claim with nobody behind it.

Each data repo's published `registry.json` is its **audience slice**, not a self-only listing: the manager
derives it from the private cross-audience aggregate per the manifest `[audiences]` map, so a repo carries the
all-staff base plus its own area (for example Technical = AllStaff + Technical) and never any entry a lower-
clearance reader may not see. Every slice entry keeps `source_repo` so a reader knows which repo holds the
body of a base entry it did not originate. The slice is regenerated and published by `kb.py index --manifest
--publish` (a write step); the standalone `kb.py index <repo>` path still produces a self-only listing for
local inspection.

## Export bundle (neutral, ACL-aware)

`kb.py export` renders a neutral, target-agnostic bundle for a document platform (SharePoint, Confluence,
Box): each nugget becomes a reader-facing doc under `docs/` (Markdown, or HTML with `--format html`) carrying
its provenance line and the same staleness caveat the answer contract adds, and a machine-readable
`bundle.json` manifest lists every doc with its metadata plus an `acl` label. `export --manifest` produces one
bundle per audience, assembled from the same audience slices, so a lower-clearance bundle never carries a
higher department's doc. The bundle format and the per-target mapping live in
[`docs/export-adapter-contract.md`](../docs/export-adapter-contract.md).
