# Symbion shared stores: two people's agents on one store

**Date:** 2026-10-26
**Status:** Built: 0057b00 (the setting, sides), 1901811 (commit identity,
`Side:`), ec94ce3 and 7220a89 (merge driver), c4a9e43, d90e31b, 22f36b4 and 70b4c22
(`pull`), a6fc316, 9d5b963, 87444c1 and 977f664 (sync), 3b8227e, f9d05f7 and
f8da0b0 (session start), c137415 and 91f8f5c (labels, `list --side`, GUI),
204fb94, fe69609 and 3a91119 (docs), 90ce841, 81d5222 and d10e588 (the final
review's fixes). Dated notes mark the decisions reshaped in the build.
**Amends:** the skill's `mechanics.md`, "Two copies of one store": in a shared
store, symbion merges the copies itself.

## The need

Two people work on one project. Each has their own agents and their own store, so
neither side reads what the other side's agents learned, decided or left open. One
store with a clone on each side, synced through its git remote, would fix that.
Today that setup works badly:

- symbion has `push` and no `pull`. The session-start hook fetches nothing, and
  `commit` does not push. Each sync is a step someone must remember.
- Each write appends to the end of `notes.jsonl`, so a pull after both sides wrote
  conflicts. git's `merge=union` is no fix: two edits to adjacent lines merged with
  exit 0 and left each edited id on two lines (reproduced 2026-10-26).
- `commit` takes only the caller's own rows. Another session's rows stay pending in
  the working file, and a plain `git merge` refuses over them.
- Every agent writes as `claude` (or `codex`, `hermes`). One side's rows read as
  the other side's own: the last wrap is whoever wrapped last, and no other-side row
  is "someone else's", so none waits on you and none is labeled `from`.
- With distinct authors alone, the reverse fails: every open row whose newest word
  is the other side's waits on you (`summary.waits`), so their tasks fill your
  session start.

## Goals

- Each side's session start reads as it does today: its own side's open rows,
  waits, priorities and last wrap.
- One line in the summary says the other side wrote something, with a count and a
  command. Nothing more about the other side reaches session start.
- Every query shows both sides, and labels the other side's rows: `list`, `show`,
  `context`, the GUI, the gotcha hook.
- Nobody pushes or pulls by hand. Session start pulls, `commit` pulls and pushes,
  and a write checks upstream at most every few minutes.
- A merge never loses a row and never leaves a line the reader cannot parse.

## Non-goals

- Live sync. The other side's rows arrive at a pull, which is soon enough: next
  session, or the next commit.
- Mentions, addressing, notifications: no `@user`, no row sent to a side.
- Folding an existing second store into a shared one. Once shared stores work, that
  path gets its own design.
- Access control beyond the git remote's, and any server.

## Anti-goals

- A store without the setting prints or stores anything it does not today.
  *Amended 2026-10-26: see the note under Constraints.*
- A pull that commits, drops or reorders another session's pending rows.
- A merge that loses a row, or keeps two lines that silently disagree.
- Running a changed `symbion.toml` command without first saying the file changed.

## The design

### 1. The setting

`shared = true` in `symbion.toml` turns everything below on. With it off, or
absent, nothing here runs and no output changes. *(Amended 2026-10-26: see the note
under Constraints.)* The setting is in the store, not
in a flag or an env var: sharing is a property of the store, both clones read the
same file, and no agent has to remember a flag. `shared = true` with
`local_only = true` is refused at config read, naming both keys. A shared store
with no origin says so in one line, as `commit` says "no remote" today.

### 2. Sides

- A side is the git `user.name` that `git -C <store> config user.name` reads in
  that clone: each person's own, the same for all their agents. symbion writes
  no setting for it, and the shared file holds none. In shared mode a write with
  no `user.name` is refused, naming the `git config` command that sets it.
- Each new row version carries `side`: the side that wrote that version. A row
  belongs to the side of its first version, read from its chain as
  `first_written` is. Edits by the other side do not move it.
- A row with no `side` belongs to every side: it reads in each side's summary as it
  does today. Rows written before the switch are such rows, and so is any new row
  that a symbion predating this design writes. They add noise to the other side's
  summary, and are never shown as the wrong side's.
- An older symbion's edit is the one misattribution: `note_to_dict` writes unknown
  fields back, so the edit carries the old version's `side`, not its writer's.
  Both sides upgrade before the switch (the docs say so). An older symbion commits
  as `git_name`, so when a pull brings a store commit with that author in a shared
  store, it prints one line: a clone runs an older symbion.
- Arcs carry `side` too, from the side that created them. Arcs also write unknown
  fields back.
- In shared mode, store commits are authored by the clone's git identity
  (`user.name`, `user.email`) instead of `git_name` and `git_email`, so the store's
  `git log` shows which side made each commit. A wrap commit adds a `Side:` trailer
  beside `Wrap:`.

### 3. Session start is your side's

- The reader is the pair (author, side). `summary.wrote_it` reads a row as someone
  else's when its author or its side differs from the reader's. A row with no side
  matches any side.
- Every summary block (open counts, waits, priorities, due rows) leaves out rows
  whose side is another side's.
- The arcs block lists your side's arcs and arcs with no side. An arc's progress
  counts every row in it, from both sides: an arc is its creator's checklist, and
  the other side may file boxes in it. `N open in arcs` counts your side's rows.
  *Amended 2026-10-26: an arc of the other side is also listed while your side, or
  a row with no side, has an open box in it. The count `N open in arcs` includes
  that box, and no other line would show it.*
- Two other surfaces depend on the reader and follow the same rule:
  - The news a write prints (`cli._say_news`) leaves out other-side rows. Without
    this, the first write after each pull would print the other side's rows as
    news, and they would move its cutoff.
  - The GUI home page is the person's session start: its waiting, due and handoff
    blocks read the person's side only.
- A row your side raised and the other side amended last waits on you. That is
  today's rule for a row someone else amended. It is not addressing: the row was
  already yours.
- The last wrap is the newest wrap commit with your side's `Side:` trailer, or with
  no `Side:` trailer.

### 4. The nudge

In shared mode, one line per other side that has rows or wraps since your side's
last wrap (since the store's start, if your side has none). For example:

```
other side, NAME: 7 rows and 1 wrap since your last wrap: symbion list --side NAME --since 2026-10-24
```

It prints no rows. The hook's text budget (`_TRIM`) holds it like any block.
*Amended 2026-10-26: the command is `list --side NAME --since wrap`, and with no
wrap by your side it has no `--since`. The line counts the entries that command
lists: heads of NAME's side written since the wrap. At most three other sides
print, and `--full` lists the rest.*

### 5. Labels on queries

- `list`, `show`, `context`, the GUI's notes and arc pages, and the gotcha hook
  show both sides. An
  other-side row names its side with its author, for example `claude (NAME)`. Your
  own side's rows print as they do today.
- The agent line (`cli._print_note`) gains `side=NAME` on other-side rows. Its
  format is a contract: nothing else on the line moves.
- `list --side NAME` keeps only that side's rows. `--side` with no shared setting is
  refused.

### 6. Sync

**The merge driver.** A hidden verb, `symbion merge-driver %O %A %B`, merges
`notes.jsonl` and `arcs.jsonl`. symbion registers it for each clone in
`.git/config` (`merge.symbion.driver`) and `.git/info/attributes`, the first time a
shared-mode command runs there, as `gitref.set_upstream` sets the upstream. Nothing
is committed, so a clone without symbion merges as git does. A person running
`git pull` by hand gets the same merge.
*Amended 2026-10-26: `api.pull` registers the driver, and no other command does.
The first `symbion pull` in a clone does, and session start and `commit` both run
one. A `git pull` by hand in a clone that has not yet run one merges as git does.*

- `notes.jsonl` merges as a set of lines against the base. A line both sides keep
  stays. A line either side added stays. A line either side removed or renamed is
  dropped, because an edit renames the replaced version's line. Order: our lines in
  our order, then the lines only their side added, in their order.
  *Amended 2026-10-26: a head that is the base line rewritten in place (same id and
  same `supersedes`, as a `sed` over the file makes it) is no new version. Against a
  new version of that entry, the rewrite is dropped and the new version stands, with
  no split. Two different rewrites of one line keep the upstream's, and the pull says
  so. Under a rebase git hands the driver its sides the other way round, so the
  driver swaps them back, and the already pushed head keeps the id. The driver runs as
  `python -P -m symbion merge-driver`: git runs it from the store's root, and
  `-m` would put that directory first on `sys.path`, where a `.py` file the other
  side committed could shadow a module.*
- When both sides edited one entry, two heads share its id. The driver settles it:
  the upstream head keeps the id, because it was pushed first, and the local head
  gets a new id, so it reads as its own entry. Each keeps its own earlier versions.
  The pull prints both ids. No line is dropped, and no id is left on two heads:
  the clone that merges sees the collision, so no agent fixes it by hand and no
  two agents fix it two ways.
- `arcs.jsonl` is rewritten whole on each change and keeps no history, so it merges
  by arc id, field by field against the base. A field only one side changed takes
  that side's value. Both sides archiving one arc keeps the earlier `archived_at`.
  Any other field both sides changed, or a slug both sides created, takes the
  upstream's value, and the pull prints the value it dropped. Rows name an arc by
  its id, so two arcs created with the same slug become one checklist. An arc never
  stops a pull.
- A whole-file rewrite on one side (a kinds rename by `sed`, as `reference.md`
  describes) merges as the removal of every base line. The other side's new rows
  keep the old kind, and `rename` moves them, as it moves any row of an undeclared
  kind. The builder tests this case.

**`symbion pull`.** Fetch, then under the store lock:

1. Set aside the pending rows: the working `notes.jsonl` and `arcs.jsonl` against
   HEAD. *Amended 2026-10-26: the set-aside files, with the old HEAD's sha, are also
   copied whole into the git dir (`symbion-pull`) until step 3 is done. A pull that
   is killed in between leaves the copy, and the next pull puts the rows back first,
   with one line that says so. A copy that is not whole (its HEAD, its list of files,
   a file it lists, or the commit it names is missing) is never applied: the pull
   stops with one line that names the copy, and nothing is put back from it. A merge
   left in progress also stops the put-back; the rows wait in the copy until the merge
   is done or aborted.*
2. Put those files back to HEAD and run `git merge` of the upstream, using the
   driver.
3. Put the pending rows back on the merged files, with the same set merge, using
   the old HEAD as the base. They stay pending, under the same session, for the
   same `commit` to take. *Amended 2026-10-26: the rows go back as a delta, not as a
   set merge: the lines the session removed come off the merged files, and the lines
   it added go on, each read through step 2's split renames (`merge.reapply`). A set
   merge forked a chain when step 2 had split an entry that the session then
   edited again.*
4. If a file the driver does not merge conflicts (`symbion.toml`, a doc under
   `docs/` or `archive/`), run `git merge --abort`, restore the files exactly as
   they were in step 1, and print the conflicting paths and the git commands that
   finish the merge by hand. A failed pull changes nothing. Until that merge is
   done, `commit` commits locally and says its push waits. This is the one case
   that needs a person or an agent to run git.

A pull ends the undo level, as a commit does: undo already refuses when the files
changed since the write. *Amended 2026-10-26: only a pull that merged something, or
put back a cut-off pull's entries, ends it. A pull that found nothing new leaves
the level.* If a pull changes `symbion.toml`, it prints one line that
names the commit and its author, before anything else runs that file's commands.
It warns and does not refuse.

**When it runs.**

- At session start: `summary --hook` pulls first, in Python, so the hook's
  `.ps1` twin needs no change. A failed fetch (offline, a timeout) prints one line,
  and the summary reads the local store.
- At `commit`: commit, pull, then push. A rejected push prints one line, with the
  command that retries.
- During a session: after a write, if the last fetch (the mtime of `FETCH_HEAD`) is
  older than the interval, fetch with a short timeout, outside the lock. If
  upstream holds commits HEAD lacks, print one stderr line:
  `origin has N new store commits: symbion pull`. It never merges, so a session's
  view of the store changes only when the session asks. *Amended 2026-10-26: only
  `add`, `resolve` and `supersede` check. A fetch that failed also starts the
  interval (a mark in the git dir, `symbion-fetched`, beside `FETCH_HEAD`, which a
  failed fetch may not move), so an origin that is down costs one wait per interval,
  not one per write. A check that fails says nothing.*

## Constraints

- With `shared` off, the whole test suite passes unchanged, and no output changes.
  *Amended 2026-10-26: text output does not change, and no stored byte does. The
  one exception is `summary --json`, which gains three keys in every store: `sync`
  and `others` (empty lists) and `others_elided` (0): one key set in every store
  is what a reader of `--json` can rely on.*
- Before you trust each new test, break the code it guards and watch the test fail
  (CLAUDE.md).
- Session-start text stays under the hook's budget.
- The merge driver and `pull` run on every platform the suite runs on, with the
  oldest git the suite supports. LF line endings, as `_replace_atomically` writes.
- No network call while the store lock is held, except the merge itself, which
  needs no network.

## Left to the builder

- The wording of the nudge, the labels and the pull's lines.
- The fetch interval (around 10 minutes) and the fetch timeout.
- Whether `side` is a row field (the recommendation) or part of the author string.
- Docs: `mechanics.md` ("Two copies of one store"), `docs/reference.md` (the
  setting, `pull`, `--side`), and the lines `SKILL.md` needs.

## Calls made in the design, for the user to check

- The side is the clone's git `user.name`. It needs no setting and is never in the
  shared file.
- A row with no `side` belongs to every side.
- An amendment by the other side to your row waits on you.
- An older symbion on a shared store gets a warning at pull, not a refusal.
- An arc belongs to its creator's side, and its progress counts both sides' rows.
- Write-time news and the GUI home page follow the own-side rule; the GUI's notes
  and arc pages show both sides.
- When both sides edited one entry, the upstream edit keeps the id and the local
  edit becomes its own entry.
- An arc conflict takes the upstream's value and never stops a pull; only a
  conflict in `symbion.toml` or a doc needs git by hand.
- A `symbion.toml` changed by a pull is a warning, not a refusal.
- In shared mode, store commits carry the clone's git identity, not `git_name`.
- `pull` never merges over pending rows in place: it sets them aside and puts them
  back.
