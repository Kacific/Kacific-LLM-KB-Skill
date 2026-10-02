# Pointer nuggets: pinning, staleness and re-pinning

A pointer nugget indexes a document whose single source of truth lives somewhere else. The nugget carries a
one-line abstract and a `source:` that names the document. This file is about what `kb.py` does with that
`source:`, how to tell that a pointer has gone stale, how to bring it up to date through the manager, and
what the audit commands can and cannot tell you along the way. It is generic on purpose and carries no
site-specific identifiers; the examples use made-up names (`example-org/example-repo`, `seed-example-readme`).

Checked against `kb.py` at commit `63cb2a6`, by running the commands and functions named below against
scratch fixtures rather than by reading the code alone. Where this file says "today" it means that commit.
The format of a nugget is in [`schema/kb-entry.md`](../schema/kb-entry.md), and the first-run setup, the
Python version split and the write-path guard are in the repository's [`CLAUDE.md`](../CLAUDE.md); neither
is repeated here.

## 1. What a pointer pins

`kb.py prescan` writes a pointer's `source:` as a GitHub **blob URL pinned to a commit**:

```
https://github.com/example-org/example-repo/blob/<commit-sha>/README.md
```

The commit is the source repository's HEAD at scan time. The pin is a fixed point, so it does not follow the
source forward: when the targeted file changes on the default branch, readers of the KB keep being served
the old blob until someone moves the pin. Nothing in `kb.py` does that automatically.

A `source:` can take other shapes, and `pin-audit` (section 4) tells them apart:

| Shape | What it is | What `pin-audit` says |
|---|---|---|
| `.../blob/<sha>/<path>` | one file at a fixed commit | audited against the file at the pin |
| `.../tree/<sha>/<dir>` | a **directory** at a fixed commit | compared with the directory's file names today |
| `.../blob/<branch-or-tag>/<path>` | a moving ref, not a pin | `unpinned-ref`: it follows the file forward, so it can never be audited for drift |
| any non-GitHub URL | a page elsewhere | `external-pointer`: not auditable here, not a defect |

A `#fragment` after the path is stripped before the file is requested, so a pointer may legitimately aim at
one section of a file. A pin is recognised only as 7 to 40 lowercase hexadecimal characters. A six-character
abbreviation or an uppercase hash is read as a ref name and reported as `unpinned-ref`.

Because a pointer can pin a directory as well as a file, any sweep that looks for pointers by matching
`/blob/` alone will miss the directory pins entirely. Look for what a `source:` line can be, not for the one
shape you have already seen.

## 2. Deciding that a pointer is stale

**Staleness is per file, not per repository.** A repository changing does not make its pointer nuggets
stale; only a change to the exact file (or directory) a pointer targets does. Compare the targeted path at
the pin with the same path on the default branch:

```
git -C <source-repo> fetch --prune origin
git -C <source-repo> log -1 --format=%H origin/<default-branch> -- <path>
git -C <source-repo> rev-parse <pin>:<path> origin/<default-branch>:<path>
```

Read the three results together:

- The last commit that touched the path **equals the pin**: the pointer is current.
- It differs, and the two blob ids from `rev-parse` are **equal**: the content is unchanged and readers are
  being served the right text. The pin is only behind by convention (for example the file was edited and
  then reverted). Re-pinning is tidy but not urgent.
- The two blob ids **differ**: the pointer is genuinely stale and readers are being served old text.

For a directory pin, use `git log -1 --format=%H origin/<default-branch> -- <dir>` and compare
`git ls-tree --name-only <pin> <dir>/` with the same listing at the branch tip.

**Derive the new pin from that last-touching commit of the exact path, never from somewhere near it.**
Use the full hash from `git log -1 --format=%H -- <path>`. Do not use the tip of the default branch, and do
not use "the commit that landed around the same time": a near-HEAD commit that never touched the file
writes a pin that looks right and is not the last-touching commit. Two pointers in one repository can need
two different pins, and two nuggets can legitimately share one pin when one commit last touched both files,
so when an old pin still appears in a registry, attribute it to nugget ids before calling it residue.

**Enumerate before you decide.** A report that names one stale nugget is not the set of nuggets that point
at that repository.

1. Search the `source:` lines of every data repo for the source repository's name, including any former
   name. A nugget keeps the URL it was pinned with, so a renamed repository hides its pointers under the old
   name.
2. Use each nugget's `related:` list as a second surface when several pointers share one source repository
   but target different files.
3. Test each hit against its own path, as above.

Do the search with `git grep` against the freshly fetched default branch, not with `find` or a recursive
`grep`. The nugget loader inside `kb.py` skips any path with a `.claude` directory below the repository
root, so its own counts are not doubled by a linked worktree, but `find` and `grep -r` walk straight into one
and count every nugget twice.

## 3. Re-pinning through the manager

A re-pin is a normal nugget change, so it goes through the same gate as any other. Never edit a nugget,
`registry.json` or `registry.md` by hand.

1. **Work in a linked worktree of the data repo**, as the write-path guard requires.
2. **Change `source:` in a copy of the nugget**, validate it with `kb.py store <file>`, then write it with
   `kb.py store <file> --into <worktree>`. `store` checks the schema, the provenance rule and the
   no-em-dash voice rule, and then writes the file **exactly as you gave it**. It does not alter the body,
   the tags or `verified`, which is why a pure re-pin leaves them as they were (section 5).
3. **Commit and land the change** by the data repo's normal route.
4. **Publish from the control home**, in two steps:
   - `kb.py index --manifest --config config.toml` with no `--publish` rebuilds the aggregate and writes
     each audience slice into the control home for review. Compare each slice's entries with the data repo's
     current `registry.json` to see what a publish would change.
   - Then add `--publish` to commit the changed slices into their data repos. This is a write step that
     needs write access, so a read-only scheduled run never passes it. The manifest commands read
     `config.toml`, so they need Python 3.11 or later (or the `tomli` backport).
5. **Verify the result by content** and bring your local clones level (below).
6. **Run `kb.py pin-audit`** on each affected repo and read the rows for the nuggets you touched.

Do not use `kb.py index <repo>` for this. It reads only that repo's own nuggets, so its output drops the
shared base entries every published slice carries.

### What a publish does

- It derives each repository's slice from the aggregate by the manifest's audience map. An entry from a
  repository whose audience is visible to other repositories appears in **every** slice that includes that
  audience, so a change in a base repository republishes several slices, and every one of those clones goes
  stale locally.
- A slice is committed only when its **entries** or its audience changed. The `generated_utc` stamp is
  ignored for that test, so an unchanged slice produces no commit. The review files in the control home do
  carry a new stamp every run, so a plain `diff` of them against the data repos reports every slice as
  changed. Compare entries, not files.
- A changed slice is committed as its own commit (`kb: publish registry slice for <audience>`) on the data
  repo's branch and pushed. So one landing is **two** commits on that repo's default branch: your merge and
  the publish.
- `unchanged` after your merge means the published slice already equals what the merged data produces.
  Either a peer published first, or your edit changed no field the registry carries. Check which before
  assuming a change is missing.

### Do not publish from a partial aggregate

If a managed repository cannot be fetched, the aggregate is built without its entries, and the run goes on.
The publish step skips only **that repository's own slice**. Every other slice is still derived from the
partial aggregate and is committed if its entries differ, and they will differ for any slice that includes
the unreachable repository's audience: the published slice loses those entries.

This was checked on a scratch bare remote. A reachable repository's slice, derived with its base repository
unreachable, dropped the base entry and was reported `published`. So: read the review output first, and
publish only when every managed repository reads `ok`. A transient fetch failure is usually the cause, and a
re-run is the remedy.

The partial aggregate is also written to the recorded aggregate file, which is the baseline `sync` compares
against. Until a complete run replaces it, `sync` reports the missing repository as `recorded none -> live
<sha>` with its nuggets as `added`. That is a stale baseline, not drift in the repository.

### Verify by content, not by commit id

A squash merge and a publish both carry **content** into a repository without carrying the **commit**.
The publish writes its own commit in each data repo, so a search for the nugget's own commit in the other
repositories finds nothing and reads as absence. Instead, read each remote `registry.json` and check that
the new pin is present, the old one is absent, and an entry you did not touch is still there as a control.

Then `git fetch` and `git pull --ff-only` every local clone the publish touched, after the publish and not
only after the merge. A clean `git status` says nothing about how far behind the remote a branch is.

### What a review pass should show

The registry carries `source_document`, `content_hash` (over the body only) and `last_verified`. Each kind
of change moves a different set, which is a quick way to confirm a change is the one you meant:

| Change | Registry fields that move |
|---|---|
| Re-pin (pure `source:` bump) | `source_document` only |
| Body edit | `content_hash`, and `last_verified` if it was cleared |
| Verification (a person read the body) | `last_verified` only; `content_hash` is unchanged |

## 4. What `pin-audit` tells you, and what it cannot

`kb.py pin-audit --repo <data-repo>` reads each pointer nugget in one repository (a `reference` nugget whose
`source:` is a URL), fetches the file it pins from the GitHub API, and reports a verdict per row. It is report-only. `--json` writes the full report to
standard output.

| Verdict | Meaning |
|---|---|
| `stale-status` | the body calls the work open while the source calls it closed; checked for every pointer whose pinned file could be read, hand-written or generated |
| `ok`, empty detail | a generated body that still reproduces exactly from the source |
| `ok`, detail `hand-written body; status-checked only` | **nothing was checked beyond the status line** |
| `enriched` | the body says more than the generator reads, and every backticked or bolded term it uses is in the source |
| `diverged` | a generated body that no longer follows from the source; needs a human read |
| `stale-tree` | a pinned directory has gained or lost files since the pin |
| `unpinned-ref` | the source is a moving ref, not a pin |
| `unfetchable` | the pinned file could not be read, so nothing is claimed |
| `external-pointer` | outside GitHub, not auditable here |

### Three checks, and what they leave out

For each pinned file it can read, `pin-audit` runs three checks: a status contradiction between body and source; for a nugget tagged
`prescan`, whether the body is what the generator would write from the source today; and, if it is not,
whether every backticked identifier and bolded phrase in the body appears in the source. A clean verdict
therefore means only that none of those found a problem. It does not mean the body is true.

It cannot see:

- **Anything the body leaves out.** A body that describes eight of a file's ten sections reads `enriched`
  before and after, because the check asks whether the body's claims are in the source and never what the
  body omits. Comparing the source's `##` headings with the body's clauses is one command and a read.
- **A version or date stamp**, or a count, quoted in plain prose. A body that still says "v1.8" against a
  source now at "v1.9" passes, because a stamp is not a term the check extracts.
- **A claim the source never makes**, if it is written in plain prose rather than as a backticked term.
- **A wrong path or name in plain prose**, for an untagged nugget, where the status line is all that is read.
- **A content edit inside a pinned directory** (below).

So the first step of a re-pin is still to read the body against the file at the **new** pin. A brief or a
commit message that calls a change "a pure SHA bump" is a claim about the body, and it is tested by that
read.

### `ok` has two meanings

`ok` has two code paths, told apart by the `detail` field and not by the verdict. An **empty** detail is the
regeneration match: the whole abstract reproduces from the source, which is the strongest check there is, and
the right action is none. The detail `hand-written body; status-checked only` is what a nugget with **no
`prescan` tag** gets, and it short-circuits before any comparison is attempted, so nothing has checked that
body beyond its status line. Reading the second as the first reads "fully verified" off a nugget nobody
has checked. When you cite this behaviour, match on the detail text, not on a line number in `kb.py`.

### `diverged` on a plain-prose body

Only a nugget tagged `prescan` reaches the body checks at all. For such a nugget whose body a person has
since rewritten as plain prose, `diverged` ("needs a human read, not a regeneration") can mean "there was
nothing to check" rather than "the body is wrong". The term check extracts only backticked spans of 4 to 40
characters and bolded phrases of 6 to 40 characters, and a body with no such terms is left flagged on
purpose: there is nothing to check, which is not the same as having checked.

The fix is markup, not a rewrite. Backtick the identifiers the prose already names, change no claim, and
the row moves to `enriched`. Three cautions:

- **Confirm each term appears literally in the source before storing.** One absent term keeps the row
  flagged.
- **Do not backtick anything shorter than four characters.** A three-character span does not match as a
  term, and the extractor then pairs its closing backtick with the next opening one and extracts the prose
  between them. Checked directly: ``one `/16` per site, a `/25` per rack`` yields the single term
  `' per site, a '`, which is not in the source, so the row stays `diverged` for a reason invisible in the
  marked-up text.
- **Do not mark up a row that already reads `ok` with an empty detail.** Its body is the generator's own
  output, so regeneration equality already checks every word. Hand markup breaks that equality for good and
  leaves only the weaker term check, which covers just the marked terms.

A `diverged` row is a prompt to find the cause, not a category. Two rows with one verdict can need opposite
treatment: one a plain-prose body that wants markup, another a real defect that markup would only launder.

### Call the audit function; do not rebuild it

`audit_pin_row(meta, body, source_text, tree_names=None)` is a pure function with no network and no clock,
so it can be run against source text you read from a local clone, with no token at all. Use it, or the
command, rather than reimplementing the regeneration comparison. A reconstruction that feeds the generator
the wrong input reports "body rewrite owed" on a body that is the generator's own output, and acting on that
would replace a strong check with a weaker one for good.

### `unfetchable` is one verdict for many failures

`pin-audit` requests each pinned file once, with a 30 second timeout and no retry, and every failure
(unauthorised, forbidden, not found, timeout, no DNS) becomes the same `unfetchable` row. The token is read
from the `[github]` section of `config.toml` (see `config.example.toml`) and, failing that, from the
`GH_TOKEN` or `GITHUB_TOKEN` environment variable. Without one, a private repository answers not-found and
a public one is held to the much lower unauthenticated rate limit.

Read the **size** of the result before the content:

- Nearly every row `unfetchable` is a fact about the credential, not about the corpus.
- A handful `unfetchable` among many `ok` rows means a token was present and working. Re-run with a
  confirmed token before concluding anything, since a transient failure looks identical to a dead pin.

The list of rows that could not be read and the list of rows that are stale are different questions, and
neither stands in for the other. Both directions have been seen: a read failure flagging a pointer that was
current, and a clean read hiding one that was stale.

This is a known weakness of the command, tracked as an open issue on this repository, and is described here as
it stands at the commit checked.

### Directory pins

For a `/tree/` pin the audit compares **file names** at the pin with file names on the default branch.
Subdirectories are not counted, and a change to the content of a file that is in both listings is invisible,
so `ok` reads "directory membership unchanged, N entries" and says nothing about content. The count is a
count of files, and the API's own count of entries including subdirectories will be larger, which makes the
audit look wrong when it is not. For a directory pin the last-touching commit
(`git log -1 -- <dir>`) is what decides whether to re-pin, and a read of the changed files is what decides
whether the abstract still holds.

### Scope

One run covers one clone. The report says so in its opening and closing lines, and a clean run in one
audience repository says nothing about the others. The corpus-wide count also barely moves when one nugget
is fixed, so a stable count is no evidence that a particular nugget is clean; check the nugget.

### `sync` and a repository it cannot reach

When `kb.py sync` cannot fetch a managed repository it lists `UNREACHABLE: <reason>` under that repository,
and the run still opens with `sync: drift detected across N managed repos.` So a run in which every repository
is unreachable (for example because no credential was exported) reads as drift everywhere. Read the per-repository
lines and not the headline. That is how it behaves at the commit checked, it is tracked as an open issue on
this repository, and this file makes no claim about what `sync` returns to the shell.

## 5. `verified` through a re-pin

`kb.py` never clears or bumps `verified`, `verified_by` or `verified_by_name` on a nugget that exists. `store`
writes the file verbatim. Those fields are read by validation, the age test in `rot`, `verify-audit`, the reader
footnote that `answer` and `export` add, and the registry's `last_verified`; none of them writes. The one place it writes `verified` is `prescan --commit`, which stages brand-new candidates as
`unverified`. The field is defined in the schema as
the last **human** verification, so every rule below falls on whoever edits the nugget; no tool will catch a
mistake here.

- **A pure re-pin leaves `verified` alone.** Moving a pin verifies the pointer. Nobody read the prose, so
  bumping the date asserts a check that did not happen.
- **A body edit must re-earn the date or clear it. It must not carry the old date across.** The date records
  a human reading a specific body, and after a rewrite that body no longer exists. A same-day rewrite is the
  worst case, because a recent date beside fresh prose looks like corroboration.
- **Clearing means writing `verified: unverified` and also removing `verified_by` and `verified_by_name`.**
  `store` refuses a `verified_by` with no date to go with it (checked: `verified_by is set but verified is not
  a date`). Once cleared, `rot` flags the nugget as Outdated (never verified) and `verify-audit` reports it
  as unverified, which is the honest state and the self-correcting direction.
- **Never let a re-pin and a body edit share one commit without deciding which rule applies**, because the
  procedure for each is the negation of the other.

An over-claimed date is the expensive error and an under-claimed one is the cheap one: a date that is too
old gets the nugget flagged and re-read, while a date that is too new suppresses the flag for
`ROT_OUTDATED_DAYS` (90 days today) and is simply believed. Stamping many nuggets with one date at once therefore switches
the hygiene sweep off for that window, whatever the bodies say. A re-pin does not clear a `rot` flag either,
since the Outdated rule reads `verified` and not the pin. The optional softeners described in the schema can
hold the flag back for a nugget in real use or whose pin-audit verdict is `ok` or `enriched`, and they never
change `verified`.

**Auditing a past commit.** Judge a `verified` change on three diffs and never on one: the frontmatter, the
body and the commit message. A trace of frontmatter alone cannot see that the same commit also rewrote the
body, which is exactly the case where moving the date is required rather than forbidden, so it can report a
correct commit as a violation and propose to restore an older date. `pin-audit` is deliberately not
date-aware and cannot referee this. Before re-pinning, `git log -p -1` the nugget itself, since a date
stranded by the previous landing is only visible there. And when a body edit and an unchanged date travel
together legitimately, say in the message when the read happened relative to the write, or the commit is
indistinguishable from the defect.

## 6. Writing a body that survives re-pins

- **Describe what the file contains or points at, never what is still open in it.** A clause that states a
  current condition ("the remaining jobs are still unwrapped") goes false the moment the source retires it,
  and a body that quotes a version, a date or a count goes stale on every source change. A thematic abstract
  that gives the arc and defers to the source needs no re-earning. One that enumerates does.
- **"Behind" is not "wrong".** A generated body is the source's opening paragraph, so a change far below it
  cannot make the body false, and re-pinning it is tidying a lagging pointer. A body that under-describes its
  file is a different matter from one that contradicts it; only the second is a defect.
- **A nearby change is not evidence that a particular claim changed.** Read the sentence a nugget actually
  maps to at its current wording, not just the hunk that changed closest to it.
- **A rewrite can invent.** A body edited "to catch up with recent events" can state a completion that never
  happened, in the confident, specific style of something real, and it will pass every mechanical check
  because it is well formed. For every claim that something has finished, passed or been resolved, find the
  sentence in the current source that says so and read it. The more a claim reads like the natural next
  chapter of an ongoing story, the harder to check it.
- **Read the generated diff before committing.** `store` checks the schema, the provenance rule and the
  em-dash rule. It does not check grammar or truth, and a scripted replacement that proves each substitution
  hit exactly once still says nothing about how the new sentence joins its neighbours.
- **A statement about another repository's state has a short half-life.** Date it ("as read on <date>") and
  say what the document does not know.
