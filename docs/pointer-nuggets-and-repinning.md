# Pointer nuggets: pinning, staleness and re-pinning

A pointer nugget indexes a document whose single source of truth lives somewhere else. The nugget carries a
one-line abstract and a `source:` that names the document. This file is about what `kb.py` does with that
`source:`, how to tell that a pointer has gone stale, how to bring it up to date through the manager, and
what the audit commands can and cannot tell you along the way. It is generic on purpose and carries no
site-specific identifiers; the examples use made-up names (`example-org/example-repo`, `seed-example-readme`).

Checked against `kb.py` at commit `63cb2a6`, by running the commands and functions named below against
scratch fixtures rather than by reading the code alone. Where this file says "today" it means that commit.
A later pass added the passages on old and new pins, the `prescan` tag, a failed or killed publish, `sync`
after a commit that changed no nugget, the two `unfetchable` texts and how a remote is read. `kb.py` is the
same file at `066b643`, and those cases were run the same way: the audit ones through `audit_pin_row` and
`pin-audit` with the fetch replaced by a local read, the publish and `sync` ones on scratch bare remotes.
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

This comparison is the only thing that finds a stale blob pin. `pin-audit` cannot, because its verdict is
the same at the old pin and at the new one (section 4).

**Count the equal-blob case and the differing-blob case apart when you sweep.** A tally of every pointer
whose pin is not the last-touching commit puts the equal-blob pointers in with the genuinely stale ones, so
"N stale" overstates how many readers are being served old text. A tally of blob differences alone hides
how many pins are merely behind. Report both numbers, each with its name, and deal with the blob
differences first. Neither number is the count of nuggets that need a body rewrite, which comes only from
reading the bodies.

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
  assuming a change is missing. The comparison is with the `registry.json` in the tool's own clone of the
  data repository, which equals the remote's file only after a fresh fetch and reset (the next sections
  show the case where it does not).
- Each run fetches every managed repository, unless a fetch TTL says otherwise. With a positive TTL
  (`fetch_ttl_seconds` under `[cache]` in `config.toml`, or `--max-age` on the command) a repository
  fetched within the window is not fetched again, so the aggregate, and any publish built from it, can
  lack a merge made since. The default is 0, which always fetches. `--force` fetches regardless of the
  TTL, and `sync` always fetches.
- The registry's `last_verified` is read from each nugget's `verified` on the default branch at the moment
  of the run. A publish therefore carries whatever `verified` is there, right or wrong, and a merge without
  its publish leaves readers on the previous registry until someone publishes.

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

### A publish that fails on one slice, and one that is killed

`--publish` pushes the slices one at a time, in order of repository key, and each push is final. A slice
whose push fails is reported as `publish <key>: publish failed (push; write access?)` and the run carries
on with the next. The hint is a guess: the same text covers a rejected push, a missing credential and a
timeout.

**The failure may be a lost race, which is not a failed write.** If a peer's merge or publish reaches that
repository between your fetch and your push, the push is rejected as non-fast-forward. This was reproduced
on scratch bare remotes by holding one push while a second control home published: the second run pushed a
slice that already carried the new entry, and the held push was then rejected. A publish rebuilds from
every repository's remote head, so the peer's publish can already hold your entry, and retrying blind is
the wrong first move. Instead:

1. Run the review pass again (`kb.py index --manifest`, no `--publish`) and compare each slice's entries
   with the data repository's `registry.json`, read by git ref (below).
2. Equal entries mean your entry landed. Publishing again reports `unchanged` for that slice and makes no
   commit.
3. Differing entries mean it did not. Publish again with `--force`.

**`--force` matters here.** A failed push leaves its commit in the tool's own clone, and the registry file
that commit wrote stays on disk. A fresh fetch hard-resets that clone to the remote, which discards the
stranded commit, so the next run starts clean. A run that reuses the clone under a positive TTL does not
reset it, compares your entries with the file it wrote itself, and reports `unchanged` while the remote
still lacks them. Checked by making a scratch remote reject pushes, lifting the rejection, and retrying
inside the TTL: `unchanged`, nothing on the remote, the stranded commit still in the clone. The same retry
with `--force` published. So with a TTL configured, `unchanged` after a failed push proves nothing until you
have read the remote.

**A run killed part-way is the same case from the other end.** Slices already pushed stay pushed. The
aggregate file that `sync` uses as its baseline is written only after the last slice, so a killed run
leaves the old baseline behind. Checked by killing a run while its second push was held: the first slice
was on the remote, the second was not, the baseline file was byte-identical to before, and `sync` then
reported `repo changed` for the repository whose slice had landed. Read each remote's `registry.json` and
its head commit before running the publish again. The re-run publishes only the slices still behind and
reports `unchanged` for the rest.

`kb.py` does set time limits: 180 seconds on each git command it runs and 30 seconds on each `pin-audit`
request. A git command that runs out of time is reported with the same text as any other failure (an
unreachable repository for a fetch, a failed publish for a push), and the process it stopped may have
finished its work on the remote first. That is one more reason to read the remote before retrying, and one
more reason not to blame a missing limit for a run that seems to hang.

### Verify by content, not by commit id

A squash merge and a publish both carry **content** into a repository without carrying the **commit**.
The publish writes its own commit in each data repo, so a search for the nugget's own commit in the other
repositories finds nothing and reads as absence. Instead, read each remote `registry.json` and check that
the new pin is present, the old one is absent, and an entry you did not touch is still there as a control.

**Read the remote by git ref.** `git fetch`, then `git show origin/<default-branch>:registry.json`, answers
for a named commit, and `git ls-remote` gives the tip with no clone at all. That is how `kb.py` itself
reads every managed repository, so it is the read that speaks for what the tool published. A raw-content
web URL is a cached front end, not part of `kb.py`: when this was written its host sent a five-minute
cache lifetime in its response headers, so it can show the previous registry straight after a publish. A
clone you have not fetched can do the same. Neither is evidence against a publish that a ref read
confirms.

Then `git fetch` and `git pull --ff-only` every local clone the publish touched, after the publish and not
only after the merge. A clean `git status` says nothing about how far behind the remote a branch is.

**Reading back a branch deletion.** After deleting a branch such as `kb/repin-example-1` on the remote,
confirm it by the exact name. A call to the hosting API that lists "matching refs" for
`heads/kb/repin-example-1` is a **prefix** match, so it still returns the branch's siblings
`kb/repin-example-1-2` and `kb/repin-example-1-3`, and the delete looks as if it failed. Checked against
this repository: the matching-refs call for the prefix `heads/ma` returned `refs/heads/main`, while the
exact-ref call for `heads/ma` answered not found. Ask for the exact ref (not found means absent), and run
the same call on a branch that must exist, such as the default branch, so that a not-found answer from a
broken call cannot pass for a clean delete.

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

### `pin-audit` cannot tell an old pin from a new one

A row's verdict depends on the body and on the text at the pin, never on how far the pin lags the default
branch. A pointer whose file has changed since the pin therefore reads the same at the old pin and at the
new one. Checked with a README edited below its opening paragraph and given a bumped version stamp, so
that the blob at the pin and the blob on the default branch differed: a generated body read `ok` with an
empty detail at both pins, an untagged hand-written body read `ok` (`hand-written body; status-checked
only`) at both, and a tagged body with a backticked term read `enriched` at both.

So a clean row does not say the pin is current. The git comparison in section 2 does, and nothing else
finds a stale blob pin. Use `pin-audit` as the check on the body and the comparison as the check on the
pin, and run both, because each is blind to what the other sees.

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

**Test the candidate body rather than reading the backticks by eye.** Call `audit_pin_row(meta, body,
source_text)` with the tag the nugget will carry and the source text at the pin, and read the verdict: the
row you want is `enriched`. Underneath it is `_body_claims_are_in_source(body, source_text)`, which returns
True or False for the term check alone. It is a private helper whose name may change, so prefer the public
function. A failure here is invisible in the marked-up text and obvious in the result.

**Adding the `prescan` tag to a nugget that has none moves its row from `ok` to `diverged`.** An untagged
body reads `ok` only because every check beyond the status line is skipped for it. Tag it and the
regeneration comparison runs for the first time, and a body that a person wrote or improved never
reproduces from the generator. Checked with one plain-prose body in three states: untagged, `ok`
(`hand-written body; status-checked only`); the same body with the tag added and nothing else,
`diverged`; the body marked up with backticked or bolded terms that all occur in the source, plus the
tag, `enriched`. A body that already carries markup
goes straight to `enriched` when tagged, provided every term is in the source, and stays `diverged` if one
is not. So tagging alone is not progress, and the `ok` it replaces was never a stronger check, because
nothing had compared that body with its source. Tag a nugget only when you mean to bring its body under
the term check, and have the markup ready.

A `diverged` row is a prompt to find the cause, not a category. Two rows with one verdict can need opposite
treatment: one a plain-prose body that wants markup, another a real defect that markup would only launder.

### Call the audit function; do not rebuild it

`audit_pin_row(meta, body, source_text, tree_names=None)` is a pure function with no network and no clock,
so it can be run against source text you read from a local clone, with no token at all. Use it, or the
command, rather than reimplementing the regeneration comparison. A reconstruction that feeds the generator
the wrong input reports "body rewrite owed" on a body that is the generator's own output, and acting on that
would replace a strong check with a weaker one for good.

Reading the body against the source yourself is a second instrument, not a worse copy of the first. When
the two disagree, that is information: resolve it by reading the source of the authoritative one (the
function, here), and do not settle it by picking the answer you expected.

### A truncation check is not a falsehood check

`kb.py` guards against a generated abstract that was cut off mid-sentence with `_looks_truncated`, and it
is easy to take for more than it is. It looks at the end of one string: a missing closing full stop,
exclamation mark or question mark, or a last word that is a dangling function word such as "the" or
"and". Its own docstring calls it partial and a backstop. Checked on scratch strings: a complete sentence
that states something false returned False, and so did one that ends on a content word, while a fragment
with no terminator and a fragment with a full stop appended after "the" returned True.

It has one call site, inside the generator's abstract builder, so it guards what the generator would
write and is never applied to a stored body. A stored body cut off mid-sentence, tagged `prescan` and
carrying backticked terms that are in the source, read `enriched`. So a false body passes the guard
because it is complete, and a cut-off stored body is not something `pin-audit` looks for. Only a read of
the body against the source answers either question.

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

The text of the verdict varies, which is easy to mistake for several failures. Each row carries a detail:
`pinned blob could not be read` for a file pin, and `directory listing could not be read at the pin or on
the default branch` for a directory pin. The plain-text report groups the rows under one heading,
`pinned blob unreadable; nothing claimed`, which is the same for both kinds of pin and is not a summary of
the corpus: the closing line counts these rows together with the external pointers as "could not be
checked here". `--json` carries the row details and the counts and has no heading. All of these mean
`unfetchable`, and none says why.

To tell a dead pin from a failed read, ask git whether the pin is a real object. In a full clone of the
source repository, `git cat-file -t <sha>` prints `commit` for a real commit and fails for one that does
not exist, and `git show <sha>:<path>` reads the file at the pin. `git merge-base --is-ancestor <sha>
origin/<default-branch>` then says whether the pin is on the default branch's history: a real commit that
is not (one from a side branch, say, or one dropped by a history rewrite) passes the first two and fails
the third. A shallow clone can fail `cat-file` for a real pin, so fetch the full history first. (Checked on
scratch with a real pin, a pin that does not exist, a side-branch commit and a depth-1 clone.) If the
object exists and `show` reads the file, the pin is sound and the row was a failed read, so re-run with a
confirmed token.

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

### `sync` after a commit that changed no nugget

`sync` compares each managed repository's current head commit with the head recorded in the aggregate
file, as well as comparing nugget bodies. Any commit to a managed repository moves the head, a
documentation-only one included, so `sync` then reports `repo changed: recorded <sha> -> live <sha>` and
nothing else: no `added`, `removed` or `changed` line for any nugget. Checked after a README-only commit to
a scratch data repository. That line is the head moving, not a problem with a nugget, and it is not a
reason to publish.

`kb.py index --manifest` without `--publish` records the new head, after which `sync` reads clean. In the
same check each slice it wrote held the same entries as that repository's `registry.json` on the remote,
so there was nothing to publish. A publish records the head of its own commit in the baseline for the same
reason, which is why `sync` reads clean straight after one. The same line appears when a peer's publish, or
a killed run of your own, moved a head after the baseline was written.

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
  current condition ("the migration is still in progress") goes false the moment the source retires it,
  and a body that quotes a version, a date or a count goes stale on every source change. A thematic abstract
  that gives the arc and defers to the source needs no re-earning. One that enumerates does.
- **"Behind" is not "wrong".** A generated body is the source's opening paragraph, so a change far below it
  cannot make the body false, and re-pinning it is tidying a lagging pointer. A body that under-describes its
  file is a different matter from one that contradicts it. The second misleads a reader, which is the
  more serious fault. The first is not a falsehood, and whether it is worth a rewrite is a judgement about
  what a reader needs from the pointer, not a defect to clear.
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
