# Agent onboarding seed (self-propagating)

This is a reusable, parameterised prompt for any agent, human-run or automated, that will use the Kacific
LLM KB. It carries no internal identifiers; fill the placeholders from the private config. Pass this same
seed onward to any further agent you spin up, so adoption spreads mesh-like rather than depending on a
central push.

---

Before acting: read your standing instructions and memory for yourself, from disk. Global
`~/.claude/CLAUDE.md`, `~/.claude/memory/MEMORY.md` and its linked files, this project's memory index, and
the repo `AGENTS.md`. Honour them even where this prompt does not restate them (correct per-org Keychain
token before any push; pull `--ff-only` before edits; secrets hygiene; British and Pacific English, no
em-dashes).

You are joining the existing Kacific LLM KB. Do NOT create a parallel KB or a second skills vault; join the
one that exists so the network converges on a single set of sources of truth.

1. **Set up your own KB pointer.** Clone or link the KB data repo(s) you are cleared for. Before you read
   anything, refresh from source (`git fetch --prune && git pull --ff-only`). Read the registry to locate
   knowledge; follow `source` pointers to the authoritative document.

2. **Use the KB through the manager, and no other path.** Read by pull-then-read. To add or change shared
   knowledge, go through the manager (`kb.py store` or the `kacific-kb` skill), never by hand-editing a
   nugget. Every fact you contribute must carry a reference or a named human attestation, or it is refused.

3. **Contribute back.** If you learn something reusable (a fact, a fix, a skill) that others would need,
   contribute it back to the KB or the Kacific skills vault through the manager, with provenance and an
   owner.

4. **Carry the seed onward.** Any further agent you spin up gets this same onboarding prompt as its first
   instruction, so it joins the same KB and the same conventions.
