# Symbion rewritten stamps: a check follows its commit through a history rewrite

**Date:** 2026-10-26
**Status:** Built: ec46a82 (stamps), 2e4db15 (lookup), 5952a84 (list), d320f0a (GUI), 9103c22 (git calls), 73abee3 (docs), 3e6a37e (review fixes).
**Amends:** `2026-09-04-symbion-design.md`, the four states and the rule that only a
re-stamp survives a rewrite.

## The need

A verdict row is stamped with HEAD's sha. A history rewrite (an amend, a rebase,
`git filter-repo`) gives each commit a new sha. The stamp then names a commit no
branch holds, and the row reads `dangling` for good, or `unverifiable` once git
prunes the old objects. The run is still on record, and often still true: a
re-author or a reword leaves the code alone.

In the field, agents that rewrote history kept the link by hand. They typed a map
of old to new shas into a check's body, or wrote a doc in the store and asked for a
verb. Git's own metadata can do that work.

## What carries the link

These rewrites keep each commit's author date and subject: amend, rebase (with or
without edits), cherry-pick, and `filter-repo` (a re-author, a text replacement).
In the field data, a live commit with the dangling stamp's author date and subject
was one successor for each clean dangling stamp, and it agreed with each map
written by hand.

- **Not the tree.** A rewrite that also edits a file (a scrubbed line, a changed
  fixture) changes the tree of every commit after it. A tree match found almost
  none of the successors.
- **Not the author date alone.** It repeats in real repos.
- **Not a map, for now.** A verb could read `filter-repo`'s `commit-map` or the
  input git gives a `post-rewrite` hook. That is exact, but an agent must run it,
  and a rewrite by someone else, or a forgotten one, stays lost. It is the upgrade
  for the misses below.

## The design

1. **The lookup.** It runs where `gitref.check_state` returns `dangling` today, and
   where the stamp's object is missing but the stamp holds an author date and
   subject (2). It looks for commits on `refs/heads`, `refs/remotes` and
   `refs/tags` (the refs the dangling test reads) whose author date (git's raw
   form: seconds and offset) and subject equal the stamp commit's. Exactly one
   match is the successor. None, or more than one, leaves the row as it reads
   today. A second rewrite needs no chain: the live commit still carries the
   first commit's date and subject.
2. **Stamp the key and the tree at write.** `provenance_stamp` adds HEAD's author
   date, subject and tree id to each new stamp (`author_date`, `subject`, `tree`),
   from one `git log -1`, so the lookup and the tree test outlive gc. An existing
   stamp works while its old object exists. A `[provenance] command` stamp is the
   project's own, and symbion adds nothing to it.
3. **The state is `rewritten`.** Never `current`, `behind` or another existing
   word, so a reader that matches the old words cannot take it for a live stamp.
   With it go the successor's sha, the successor's own relation to HEAD
   (`current`, `behind N`, `ahead N`, `diverged`) and the tree test: same tree,
   tree differs, or unknown (an old stamp with no `tree`, its object pruned).
   Unknown must not read as either of the other two.
   - Terminal (`term.py`): `rewritten as 4e1a9c2, tree differs, behind 3`,
     `rewritten as 4e1a9c2, same tree, current`, or with unknown, no tree word.
   - The agent line (`cli._print_note`): `state=rewritten`, and fields for the
     successor, its relation and same tree.
   - `--json`: `state: "rewritten"`; `distance` is the successor's; keys for the
     successor's sha, its relation and same tree. `list -h` names them.
   - The GUI badge: the notable colour, not the bad one. Its tip: ran at OLD;
     rewritten as NEW, matched by author date and subject; same tree, or the
     tree differs; then HEAD's commits since NEW, as `behind` lists them. While
     OLD exists, `git diff OLD NEW` shows what the rewrite changed.
4. **Cost.** A row that is not dangling makes no git call it does not make today.
   The index of live commits (one `git log` over the refs above) is built at most
   once per repo per command or GUI page (the `gitref.heads_once` scope), only
   when a dangling stamp is met. It is not kept across pages, as
   `gitref._RELATIONS` is: a serve's HEAD stays put while an agent rewrites a
   branch in another worktree, and the dangling test is asked on every read for
   the same reason. The builder bounds the `git log` (by the oldest dangling
   stamp's date, say) and times a page on a large repo before and after.
   (Built 2026-10-26: at most twice a scope. The first build is bounded by the
   first stamp's date; a stamp older than that bound builds it once more over
   the whole history. `list` reads newest first, and a bound lowered for each
   older stamp rebuilt the index for each one. A dangling stamp never clears, so
   two of different ages make each page that shows them walk the whole history:
   about 20 ms at 900 commits, 0.8 s at 63,000.)
5. **Stores over several repos.** The lookup runs in the stamp's repo
   (`gitref.stamp_cfg`).

## Why `rewritten` is not `current`

A successor says "this commit became that one". It does not say "the result still
holds". A re-author keeps the code. A rebase onto a new base puts the change on
other code. So the state keeps its own word, and same tree is the one case where
the run's input is known to be identical. The 2026-09-04 rule still holds: nothing
reads as verified that nobody can re-check.

## Misses

These stay `dangling`, or `unverifiable` once the object is gone:

- A reworded commit: its subject changed.
- A commit squashed or fixed up into another. The commit it folds into keeps its
  own date and subject, so a stamp on that one matches.
- A commit the rewrite dropped.
- A stamp written before this design whose object git has pruned.

A stamp whose old line still sits on a branch (a backup) reads `diverged`, as
today: the lookup runs only for `dangling`. A dirty stamp still reads
`unverifiable`: dirty outranks every relation.

## Not in this design

- No row is rewritten or re-stamped. The lookup runs at each read.
- Rows filed on a rewritten commit, as a `commit:` target or ref. The same key could
  lead `context --target commit:NEW` to them, but that is its own design: it
  changes what `list --target` returns, it touches `context --commit` and
  `--branch`, and a commit target would have to store its own key to outlive gc
  (the user's call, 2026-10-26).
- A summary line for dangling checks.
- A verb that reads a commit map. Build it when a miss shows up in the field.
- A working-tree hash for dirty stamps. That is a separate problem.

## Tests to aim

Each one fails with the lookup turned off.

- A check, then `commit --amend` with no change: `rewritten`, same tree, `current`.
- An amend with a file change: `rewritten`, the tree differs.
- A reword: `dangling`.
- Two live commits match (a cherry-pick to two branches): `dangling`.
- A re-author (author changed, date and subject kept, as `filter-repo --mailmap`
  writes): `rewritten`.
- The old object pruned: `rewritten` with the stored date, subject and tree, the
  tree test answered; `unverifiable` without them.
- In one process, a rewrite after the first read: the next page or command finds
  the successor while HEAD stays put.
- A test that rewrites history clears `gitref._RELATIONS` and the new index cache.

## Docs to update when built

- SKILL.md's state list. It stays under its 16 KB cap: a line goes in only when
  another comes out.
- `docs/reference.md`'s states.
- A dated note at the four-state rule in `2026-09-04-symbion-design.md`.
- The GUI's `dangling` tip keeps "re-run the check" for a stamp with no successor.
