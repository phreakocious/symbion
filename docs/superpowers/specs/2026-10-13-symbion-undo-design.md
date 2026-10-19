# Symbion undo: one level, the caller's last write

**Date:** 2026-10-13
**Status:** Built.
**Relates to:** the user's request for an undo (2026-10-01), and the in-place edit
(`store.rewritable`), whose earlier text no disk holds.

## The need

The user asked for an undo, in the CLI and the GUI. Two cases carry it:

- **A misclick.** In the GUI a resolve or a star is one click, and nothing
  takes it back except a second write, which leaves a trace of both.
- **An edit in place.** Before `symbion commit`, an edit to your own row rewrites
  it. The text it replaced is on no disk, so a wrong edit cannot be recovered.

A general undo fights the append-only store. Taking back committed work is a
new row that reverts it, and needs no new verb.

## The design

- **One level.** Under the store lock, each write first copies `notes.jsonl`,
  `arcs.jsonl` and the sessions file into the store's git dir
  (`symbion-undo/`). After the write, the copy becomes the undo level if a file
  changed. A write that changed nothing, such as one refused under the lock,
  leaves the previous level in place.
- **The whole write.** A batch (`add --from-json`, `arc seed`, `rename`) is one
  write under one lock, so one undo takes back all of it.
- **`undo ID`, guarded.** It refuses, naming the last write, unless:
  - ID is a row or arc that write touched,
  - the write was the caller's, by the rule `commit` takes rows by (this agent
    session's write, or with no session, a write no session made, of rows the
    caller's author wrote),
  - no commit has run since (a commit ends the level),
  - and the files are as that write left them (no git command or hand edit
    since).
- **The GUI.** The notice after a resolve, a star, an edit or an arc tick has an
  undo action. A new notice dismisses the older ones: an older notice's undo
  would take back a newer write that it does not name.

  *2026-10-13, later the same day: replaced.* An undo icon in the top bar shows
  while the store's last write is the person's and uncommitted (`store.undoable`,
  read with the commit and push buttons every 5 s). A click opens a dialog that
  lists what the undo changes (each row it removes, and each row it puts back
  with the fields that change), and undoes only on confirm. Why: the notice's
  8 s set a deadline the store does not have, since the level lasts until the
  next write, commit or undo. A click as the notice faded did nothing (NiceGUI
  deletes the element when Quasar dismisses it, a second before the fade ends),
  and the add form offered no notice at all, so a note added to the
  wrong store took a `git restore`. The dialog names the write, so the icon
  needs no label, and the page reads no rows to draw it.

## Why a copy and not the git index

The first design staged the files before each write, so the index held the
state before the last write. Measured 2026-10-13 on a store's `notes.jsonl`:
each `git add` writes a blob over a quarter of its size that nothing
references. Git prunes such blobs only after two weeks, and auto-gc counts
loose objects, not bytes, so a busy store grows by tens of MB a day. A plain copy took 5 to 8 ms and leaves
git untouched. The index is also shared with anyone who runs `git add` in the
store, and `commit` builds its own index.

## Limits

- One level, shared: any write by anyone replaces the level. A refusal names
  the write that holds it.
- An arc-only write records no writer (an archive keeps the arc's creator), so
  with no session any caller may undo one.
