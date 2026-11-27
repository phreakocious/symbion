# Symbion dirty stamps: a check run on uncommitted changes follows the commit that holds them

**Date:** 2026-11-26
**Status:** Built 2026-11-26.
**Amends:** `2026-09-04-symbion-design.md`, the rule that a dirty stamp reads
`unverifiable (dirty tree)` for good. Builds what
`2026-10-26-symbion-rewritten-stamps-design.md` left out: "A working-tree hash for
dirty stamps."

## The need

A verdict row is stamped with HEAD and a dirty flag. A dirty stamp reads
`unverifiable (dirty tree)` for good: the 2026-09-04 design held that its tree "was
never committed and no longer exists". The write's note says so, and sends the
agent to undo, commit, re-run and write again.

That rule teaches an agent to commit before it writes the check. A field report of
2026-11-26: an agent ran a probe on its working tree, committed the fix in two
commits, then wrote the check. The check took the second commit's stamp and read
`current`, though no run had seen that commit. A reviewer caught it, and the run was
redone at HEAD.

A dirty tree is usually committed soon after the run. In the field data, most dirty
stamps on HEAD's line had every dirty path committed later, files new at the stamp
among them. Once a commit holds the exact tree of the write, the 2026-09-04 reason is
gone: that tree was committed, and it exists.

## The design

1. **Stamp the working tree at a dirty write.** When `_dirty_paths` finds a path,
   `provenance_stamp` adds `dirty_tree`: the tree id the working tree would commit
   as after `git add -A`. Untracked files count and ignored files do not, as in
   `git status`. It is computed in a copy of the index with a throwaway object
   directory, which reads the repo's own objects as an alternate
   (`GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`),
   so nothing under the repo's `.git` changes. The index and the objects are
   `git rev-parse --git-path index` and `--git-path objects`, so a linked worktree,
   whose `.git` is a file, hashes too. Three details, from review on 2026-11-26:
   - The copy keeps the index's mtime (`shutil.copy2`). Git re-reads an entry
     whose mtime is not older than the index's. A copy with a new mtime trusted
     the stat of a same-size edit made in the second of the index's last write,
     and the hash dropped the edit that `git status` showed (measured 2026-11-26,
     git 2.55 on macOS and 2.43 on Linux: 10 of 10 runs each; 0 with the mtime
     kept).
   - Each git call runs with `-c core.splitIndex=false`. Otherwise, with a split
     index, the hash left a new `sharedindex.*` file in the repo's `.git`.
   - An index with an assume-unchanged or skip-worktree entry (a lowercase or `S`
     tag in `git ls-files -v`), or a sparse checkout (`core.sparseCheckout`),
     stamps no `dirty_tree`. `git add -A` skips a flagged path's edit, and an
     out-of-cone file's, so the tree matched a commit that lacks an edit the run
     saw.
   - Over 20 MB of changed and new files (summed from `lstat`, before any is read)
     stamps no `dirty_tree`, and the hash runs with `-c core.compression=0`:
     `git add -A` reads each file in full. Added at build, after a side agent's
     report: on the first build, 150 MB of new files took a write 4.2 s, and a
     1 GB one 25.5 s (6.1 s uncompressed).

   A failure stamps without it, as a failed `tree` does. A clean write gets no key
   and no new git call.
2. **The lookup.** In `gitref.check_state`, a dirty stamp with `dirty_tree` looks
   in `STAMP..HEAD` (one `git log --format='%H %T'`) for a commit with that tree.
   The oldest match takes the stamp. First, one `git log --format=%T` per HEAD
   in a `heads_once` scope, from a day before the stamp's author date, lists
   HEAD's trees: a stamp whose tree is in none asks git nothing more. At build,
   200 unmatched rows took `list` from 0.16 s to 2.5 s with a walk each, and to
   0.18 s with the list. No match, or a stamp object git has pruned,
   reads `unverifiable (dirty tree)` as today. A stamp without `dirty_tree` makes no
   new call. The answer is cached per (repo, stamp, dirty tree, HEAD), as
   `gitref._RELATIONS` caches a relation.
3. **The state is `committed`.** Never `current` or `behind`, so a reader that
   matches the old words cannot take it for a clean stamp. With it go the matched
   commit's sha and that commit's own relation to HEAD: `current` or `behind N`, as
   it is in `STAMP..HEAD`. The tree is the same by construction, so no tree word.
   - Terminal (`term.py`): `committed as 9f2c1ab, current`,
     `committed as 9f2c1ab, behind 3`.
   - The agent line (`cli._print_note`): `state=committed successor=9f2c1ab
     successor_reads=current`, the fields `rewritten` uses. A grep for
     `state=current` does not match it.
   - `--json`: `state: "committed"`; `distance` is the matched commit's; the
     successor keys `rewritten` uses. `list -h` names them.
   - The GUI chip takes the colour of the matched commit's state, good for
     `current`: that commit's tree is the tree at the write, as a clean stamp's
     commit is. Its tip: ran on uncommitted
     changes at OLD; committed as NEW, the same tree; then HEAD's commits since NEW,
     as `behind` lists them. A `behind` click reads from NEW.
   - The recent board (`pages._recent`) counts `committed as X, current` as current.
4. **The write's note** (`cli._name_dirty`). With `dirty_tree` stamped, it says the
   entry reads `unverifiable (dirty tree)` until a commit holds this exact tree,
   then `committed as SHA`, and that another edit before that commit keeps it
   unverifiable. It names the paths as today, so an untracked scratch file shows.
   Without `dirty_tree` (a failure, an index flag, a sparse checkout, a
   `[provenance] command`), the note is today's.
5. **Stores over several repos.** The hash and the lookup run in the stamp's repo
   (`gitref.stamp_cfg`).
6. **`[provenance] command`.** symbion adds nothing to its stamp. A command that
   prints `dirty_tree` gets the lookup, as one that prints `tree` gets the rewrite's
   tree test.

## Why `committed` is not `current`

The run was on changes no commit held when it was written, and the reader should
see that. `committed as` names the commit that holds them, beside the stamp's own
sha. The 2026-09-04 rule still holds: nothing reads as verified that nobody can
re-check. A checkout of the matched commit is the tree at the write.

## What a stamp holds

A stamp is taken at the write, not at the run: symbion sees no run. An edit between
the run and the write is in `dirty_tree`, as a commit between them is in a clean
stamp (the field case above). A report the check wrote into the repo before the
write is in the tree, and a commit that holds it matches. So the stamp is the run's
tree only when the write follows the run. SKILL.md is to ask for that (Docs to
update when built).

## Approaches

- **The hash.** (A) A copy of the index and a throwaway object directory: picked.
  (B) The repo's own object store, as `git stash create` does: fewer steps, but it
  leaves loose objects in the user's `.git` and copies a big untracked file into
  it. (C) Tree hashes in Python: writes nothing, but re-implements git's tree format
  (sort order, modes, SHA-256 repos).
- **The match.** The whole tree, the user's pick. The dirty files alone would follow
  more often, but vouch for a tree the run never saw, and need a hash for each path
  with a cap.
- **Docs only.** The skill tells the agent to write the check, commit, and run
  again. It keeps the double run and the rule that taught agents to commit first.
- **The label.** `committed as SHA` (proposed). Plain `current` or `behind N` would
  name a commit other than the stamp's.
- **The capture point.** At the write, as a clean stamp is (picked). At the run: a
  symbion call before each run, which an agent can skip as it skips writing right
  after the run.
- **Index flags.** Refuse the hash (picked). Clearing the flags in the copy works,
  one `update-index` call per flag: one call given `--no-assume-unchanged
  --no-skip-worktree` left skip-worktree set. But a standing flagged edit
  then never matches, and the note would have to name a path `git status` does not
  show.

## Misses

These stay `unverifiable (dirty tree)`:

- Any edit between the write and the commit.
- An untracked file the commit leaves out.
- An index with an assume-unchanged or skip-worktree entry, or a sparse checkout:
  no `dirty_tree`.
- Over 20 MB of changed and new files: no `dirty_tree`.
- A stamp whose commit git has pruned: `STAMP..HEAD` cannot be read.
- A rebase that changes the matched commit's tree before it is read.
- A dirty submodule: the tree holds the submodule's commit, not its changes, so a
  write with a dirty gitlink path stamps no `dirty_tree`.
- Every dirty stamp written before this design.

As with a clean stamp, ignored files are outside the tree: a run that read one is
not pinned by it. A clean stamp has the flagged-path gap as well: an
assume-unchanged edit with no other path dirty makes a clean write, and the check
reads `current` (measured 2026-11-26). That is outside this design.

## Cost

Only a dirty write pays: a copy of the index, `git ls-files -v -s`, `diff-files`,
`ls-files --others`, `check-attr`, `git add -A` and `git write-tree`. Each dirty
file is hashed and compressed into the throwaway directory once more. A read of a
stamp with `dirty_tree` makes one `git log` per (stamp, HEAD).

Measured at build (2026-11-26, macOS, synthetic trees of 1-line files, median of 5
runs): a dirty stamp took 107 ms against a clean one's 27 ms at 1,000 files, 174
against 48 at 10,000, and 906 against 275 at 100,000. At 100,000, `git add -A`
took 188 ms and `ls-files --others` 126.

A clean filter (git-lfs) runs in `git add`, and the hash matched the commit, but
git-lfs wrote its copy of the file under `.git/lfs/objects` (measured 2026-11-26).
So a dirty file with a `filter` attribute stamps no `dirty_tree`. The test is per
file, not per repo: a global `filter.lfs` config, which a git-lfs install writes,
would otherwise turn the follow off in every repo.

## Tests to aim

Each one fails with the lookup or the hash turned off, or, where it names one, with
that detail removed.

- A check on a dirty tree, then a commit of that tree: `committed as C, current`.
  One more commit: `committed as C, behind 1`.
- An edit after the check, then the commit: `unverifiable (dirty tree)`.
- A file new at the check, committed with it: `committed`. Left out of the commit:
  `unverifiable (dirty tree)`.
- The field case: two commits, the second holding the tree: matches the second.
- `commit --amend` folds the change into the stamp's own commit: `committed as`
  the amended commit, while the old object exists.
- The write leaves every file under the repo's `.git` as it was, a split index
  included: fails without `-c core.splitIndex=false`.
- A same-size edit in the second of the index's last write, hashed a second or
  more later: the edit is in `dirty_tree`. Fails with a plain copy of the index.
- An assume-unchanged entry, a skip-worktree entry, a sparse checkout: no
  `dirty_tree`. Fails with the refusal removed.
- A dirty write in a linked worktree: `dirty_tree`, and the commit matches.
- A dirty stamp with no `dirty_tree`: today's state, and no `git log` call.
- A dirty gitlink path: no `dirty_tree`.
- In one process, HEAD moves after a read: the next read finds the new commit.

## Docs to update when built

- SKILL.md's `unverifiable (dirty tree)` bullet: the follow, in place of "This
  never clears", and a check written right after its run. It stays under its 16 KB
  cap: a line goes in only when another comes out.
- `help.md`'s states: a `committed as` bullet, and the `unverifiable` bullet's
  "This never clears."
- `mechanics.md`'s note on a dirty write; `docs/reference.md`'s `[provenance]`
  keys gain `dirty_tree`.
- A dated note at the dirty rule in `2026-09-04-symbion-design.md`, and at "Not in
  this design" in `2026-10-26-symbion-rewritten-stamps-design.md`.
