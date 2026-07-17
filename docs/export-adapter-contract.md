# Export adapter contract

`kb.py export` renders a **neutral bundle**: a self-contained directory that any document platform can
ingest, without kb.py knowing anything platform-specific. A thin per-target adapter maps the bundle onto a
target (SharePoint, Confluence, Box). This file is the contract between the two: the bundle an adapter can
rely on, and how each target maps it. It is generic on purpose and carries no site-specific identifiers.

Live API push to a specific target is deliberately out of scope here (it needs per-target authentication and
a network client, which sit outside the stdlib-first, low-maintenance envelope). kb.py produces the neutral
artefact; a target import runs as a later, separately-authenticated step. The "Live push (later)" column
below is where that slots in.

## Producing a bundle

- Single repo: `kb.py export <repo> --out <dir>` writes one bundle for that repo's cleared view.
- ACL-aware, all audiences: `kb.py export --manifest --config config.toml --out <dir>` writes one bundle per
  managed repo under `<dir>/<repo_key>/`, each assembled from that audience's slice. A dept bundle carries
  the all-staff base plus its own area and never another department's doc; the exclusion is enforced upstream
  in the slice derivation, so a lower-clearance bundle is leak-safe by construction.
- `--format markdown` (default) or `--format html` picks the doc render. Both carry the same `bundle.json`.

## Bundle layout

```
<bundle>/
  bundle.json          # the manifest an adapter reads
  docs/
    <doc-id>.md        # one reader-facing doc per nugget (or .html with --format html)
    ...
```

Each doc leads with its title, a provenance line (`Source: ...` or `Attested by: ...`), a `Verified:` line,
then the nugget body, then any reader caveat (a staleness note when the nugget was last verified over 90 days
ago, and a note when its status is not `published`). The frontmatter metadata is not repeated in the doc; it
rides in `bundle.json` so the prose stays clean and the machine-readable fields stay in one place.

## `bundle.json`

```json
{
  "schema_version": 1,
  "generated_utc": "2026-01-01T00:00:00Z",
  "audience": "Technical",
  "target_neutral": true,
  "format": "markdown",
  "docs": [
    {
      "id": "tech-vpn",
      "title": "VPN profile",
      "domain": "technical",
      "type": "how-to",
      "status": "published",
      "owner_gid": "…",
      "owner_name": "…",
      "source_document": "runbooks/vpn.md",
      "confidence_score": "high",
      "last_verified": "2025-11-01",
      "source_repo": "technical",
      "acl": "Technical",
      "path": "docs/tech-vpn.md"
    }
  ]
}
```

- `audience` / `acl`: the clearance this whole bundle is scoped to. An adapter maps the bundle to the target
  space or permission group for that audience. `acl` repeats the bundle audience on every doc so a per-doc
  importer needs no outside context.
- `source_repo`: which repo holds the body (a base doc in a dept bundle still originates in the base repo).
- `path`: the doc's location within the bundle, relative to the bundle root.
- The field set is additive and carries `schema_version`; an adapter ignores fields it does not use, so new
  fields never break an existing adapter.

## Doc render

- **Markdown** (default): the neutral form. Confluence and Box ingest Markdown directly; it is diffable and
  dependency-free.
- **HTML** (`--format html`): a minimal, self-contained page per doc, for a target that takes HTML. The
  converter is deliberately small (headings, paragraphs, unordered and ordered lists, fenced code blocks,
  backtick code spans) and HTML-escapes all content first, so a body can never inject live markup. Inline
  bold and italic are left as literal characters; a target that needs richer inline formatting converts from
  the Markdown bundle instead.

## Per-target mapping

Each adapter reads `bundle.json` and maps it onto the target. The mapping is the same shape for every target;
only the destination construct differs.

| Bundle element | SharePoint | Confluence | Box | Live push (later) |
|---|---|---|---|---|
| Bundle (`audience` / `acl`) | a document library or site scoped to that audience group | a space (or a labelled parent page) for that audience | a folder scoped to that audience's collaboration group | resolve the destination container and its permission set for the audience |
| Doc (`docs[].path`) | a page or file in the library | a page under the space | a file in the folder | create or update the item, keyed by `id` |
| `id` | the stable item key for idempotent re-import | the page key | the file name key | upsert by `id`; do not duplicate on re-run |
| `title` | page title | page title | file title | set on create and on update |
| `source_document` / provenance | a source property or footer | a page property or footer | a description field | preserve provenance so the exported copy stays attributable |
| `last_verified`, `status` | column metadata | page labels or properties | metadata field | carry through; a target can surface the staleness caveat itself |

## Idempotency and re-import

Re-run `kb.py export` to regenerate a bundle from the current KB; it overwrites the bundle directory. A
target adapter keys on `id` so a re-import updates existing items rather than duplicating them. The KB remains
the single source of truth: an export is a rendered copy, never a second editable home. Edits flow back
through the manager, not through the target.
