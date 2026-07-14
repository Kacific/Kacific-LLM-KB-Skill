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
domain: it | ot | shared | company
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
