# Symbion diff viewer: a commit, and a file since an entry

**Date:** 2026-10-30
**Status:** Drawn as five frames on a design canvas on 2026-10-30. The user's answer
the same day settled which of them get built and that a split view belongs in the GUI.
This document covers four of the frames. A fresh reader then reviewed it against the
code. It waits on the user's read.
**Relates to:** the GUI design (2026-09-08); the file viewer (`viewer`, `viewable` in
`gui/notes.py`); a commit's message and diff in that viewer (commit `bdde92e`).

## The need

Commit `bdde92e` put a commit's message and diff in the file viewer, with links to
the commit on its forge. The user asked for that as "the diff, then jump to it, or its
MR/PR", and then for side-by-side diff options and for the commit messages to show.
Of the frames drawn for it: "it's all better than where we're at.. :) side-by-side
diff is far easier to reason about for me, but I realize this is divisive".

Today:

- The viewer shows a commit's diff as one pygments `diff` code block
  (`fenced(diff, "commit.diff")`): unified, with no line numbers.
- Its dialog is the file viewer's `.sb-md-card`, `min(860px, 94vw)` wide.
- A check's `behind N` chip and a file entry's `N commits since` chip list up to 8
  commits in a hover tip (`check_tip`, `since_tip`). Nothing in the tip is clickable.
- A commit's object page shows a count line, the diff button and the forge links,
  then the entries.

## Goals

1. A diff reads unified or side by side, at the reader's choice, kept per browser.
2. The commits behind a `behind N` or `N commits since` chip are a list the reader
   can click, not a tip.
3. A file entry can show what changed in its file since the entry was written, and
   the file as the entry saw it.
4. A commit's page leads with the commit: its message, its files, and the entries on
   each file.

## Non-goals

- `[links]`, the URL templates per custom catalog type, drawn as the fifth frame. It
  adds a config convention, so it gets its own design.
- Syntax colour inside a diff. Today's fence colours only the `+` and `-` lines, so
  nothing is lost.
- A diff between two arbitrary commits, a file's full history, comments on lines.
- A third "files" mode, drawn in the frame beside unified and side by side. A file
  list at the head of every diff, and the commit page's file list, cover it
  (claude's call).
- A diff for an entry whose file changed only by uncommitted edits. `changed_since`
  lists commits, so such an entry has no since chip today, and this design adds none.
- A list that opens from the chip and stays on the page, drawn in the third frame.
  The chip opens the viewer instead (part 2, claude's call).

## 1. The diff view

One renderer serves every diff in the GUI: a commit's, and a file's since an entry
(part 2). Its input is unified diff text, from git or from `difflib`. It parses the
text into files, hunks and lines. Each line has its old and new line number.

**Merges.** `git show` gives a merge's stat against its first parent, but its patch
as a combined diff: empty for a clean merge, and `@@@` hunks the parser cannot read
for one with conflicts resolved. A merge reads against its first parent
(`--diff-merges=first-parent`): what the merge brought onto its branch. The head
says so: "a merge: the changes it brought onto <first parent's short sha>". Today's
viewer shows a merge's stat with no patch at all.

**Head.** The dialog's head line names what the diff is of. Under it, a list of the
files, each with its `+a −b`. A click on a name scrolls to that file's section. A
one-file diff shows no list.

**Unified.** Two number columns (old, new), then the line. A deleted line has a `−`
mark and the red wash; an inserted line has `+` and the green wash. The washes are
the theme's (`.gd`, `.gi` in `gui/theme.py`), so a diff looks as it does today.

**Side by side.** Old on the left, new on the right, each with its numbers. A context
line shows on both sides. A run of deleted lines is paired, line by line, with the
run of inserted lines that follows it in the same hunk. Where one run is longer, the
other side has blank rows, in a darker fill than either wash.

**Changed words.** In a paired deleted and inserted line, the words that differ get
a stronger wash. This holds in both modes. `word_diff` in `gui/notes.py` marks the
changed words of an edited `checked` or `result`, with `SequenceMatcher` over words.
Reuse the method, not the function: it returns one string with both sides mixed,
and side by side needs each side alone.

**Other records.** A binary file, a rename, a mode change and git's
`\ No newline at end of file` show as one muted line, in git's words.

**The toggle.** A two-button control in the dialog's head: `unified`,
`side by side`. The choice is kept per browser, across tabs and restarts. A first
visit reads unified, as git's own output does (claude's call).

**Width.** A diff dialog is wider than the file viewer's 860px: wide enough for two
80-column sides at the viewer's mono size, else the width of the window. Where the
dialog cannot hold side by side, it reads unified, and the toggle is hidden: a
side-by-side pick renders the unified drawing too, and a CSS width rule shows the
one that fits. (Built 2026-10-30: hidden, not disabled with a tip. The server
cannot know the window's width, and a disabled control there could not be used
anyway.)

**Size.** Today's cap holds: a diff past `VIEW_CHARS` is cut, and the dialog says
so. The diff renders as one HTML element built on the server, all text escaped, not
as one NiceGUI element per line. A 500,000-character diff sent as an element per
line would choke the page's websocket.

Under `serve --read-only`, `gitref._git` keeps every answer for the life of the
process. A whole-tree diff (part 2) kept there would hold the demo's memory for
good. A diff runs outside that cache, or is kept only under the size cap.

**Where the choice lives.** The serve sets no `storage_secret`, so NiceGUI's
`app.storage.browser` and `app.storage.user` are not available. `app.storage.tab`
does not outlive the tab. The toggle sets a cookie, `sb-diff`, which the next
page's request carries to the server, and the page keeps the pick in its client
storage until then. (Built 2026-10-30: a cookie, not `localStorage`, which the
server could read only with a round trip from the browser.)

## 2. Commits since an entry

**The chips.** A click on one of these chips opens the viewer:

- A file entry's `N commits since`.
- A check's `behind N` and `ahead N`.

The chips are drawn in the browser by `gui/card.js`, and a click reaches the server
as one `act` event, as the view button's does. The viewer opens as a dialog from the
server, like a commit's. The frames drew a list that drops from the chip and offers
the diff as a second click. One click to the diff, with the list at its head, needs
no new state in `card.js` (claude's call). The hover tip stays and says what a click
opens.

**The list.** At the dialog's head, the commits, newest first: from `changed_since`
for a file entry, from the stamp's range (`check_tip`'s) for a check. Each row has:

- the short sha, a link to the commit's page
- the commit's age
- its subject, cut to one line
- a button that opens the commit in the viewer

Past a cap, a last line says how many more.

**A file entry: what changed since it.** Under the list, the diff view (part 1) on
one file. On the left is the content the entry saw. On the right is the file now,
uncommitted edits included, which is what `changed_since` compares against.

The content the entry saw comes from the entry's `target_blob`. A catch: `target_blob`
is `git hash-object` without `-w`. The blob is in the object store only once that
content was committed or staged.

The left side is the file at the parent of the oldest listed commit
(`gitref.since_base`), and the diff is `git diff <that commit> -- <path>`, which
compares it with the work tree. When the file there is the entry's `target_blob`,
the head says "as this entry saw it". Otherwise, the entry saw edits that no commit
holds, or has no `target_blob` (written before symbion stamped one). Then the head
says "as of <sha>, the nearest commit: the entry saw edits no commit holds".

git reads both sides through the file's filters, so under `core.autocrlf` or LFS
only the lines that changed read as changed.

(Built 2026-10-30: one source, a commit, in place of the blob first and the
commit as a fallback. In a history without merges, the parent of the oldest
listed commit holds the entry's blob whenever any commit holds it, so the blob
source added nothing. A `git diff` between a commit and the work tree applies the
filters to both sides, where a blob read with `cat-file` and a file read from disk
would need them applied by hand.)

**Read it as it was.** A button in the dialog's head. It opens the file viewer on the
left side's content, titled with the path and "as this entry saw it", or "as of
<sha>" in the fallback.

**A check behind HEAD.** Under the list, `git diff <stamp> HEAD`, the whole tree, in
the diff view, under the size cap. An `ahead N` dialog shows the list alone
(claude's call).

**Repos.** In a store with `[repos]`, the list and the diff ask the repo the entry's
file is in (`cfg.for_type`), or the stamp's repo (`stamp_cfg`), as the tips do today.

**Read-only.** The demo shows what the viewer shows today. A file the viewer would
refuse there gets neither footer button.

## 3. A commit's page

The object page of a commit leads with one card, as drawn in the first frame:

- The short sha, the author's name (no email), and the date in the GUI's zone.
- The message, subject and body, wrapped.
- The diff button, the forge links and a button that copies the full sha.
- A label with the counts: `7 files · +239 −5 · the entries on each`.
- One row per changed file:
  - its target chip, a link to the file's page, with the file's view button
  - its `+a −b`
  - its count of entries and of open ones (`4 entries · 1 open`), as a link to the
    file's page. A file with no entries leaves that blank.

The entries on or referencing the commit follow, as today. The first frame also shows
the branches that hold the commit (`on main, origin/main`). It is not in this design
(claude's call: it costs a `git for-each-ref --contains` per page view, and nobody
asked for it).

A name that is not a commit in this checkout gets no card. In its place, one line
says "not a commit in this checkout", and the page has no diff button. Today the
page shows the button for any name, and only a click on it says so.

## Tests

- **The parser**, in isolation. Cover numbering across hunks, the pairing of runs
  (equal, longer deleted, longer inserted, a deletion with no insertion), the
  no-newline marker, binary files, renames, a mode change and an empty diff.
- **A merge**, from a scratch repo: its diff is the first parent's, not empty.
- **Line endings**: a file under `core.autocrlf=true`, unchanged since its entry,
  shows no changed lines.
- **The GUI**, in `tests/test_gui_pages.py`. Cover:
  - the toggle in the dialog
  - each mode's rows for a small commit
  - a since chip's click opening the dialog, and the list's rows and cap
  - both sources of the since diff, including the fallback with its head line
  - the commit page's file rows and entry counts, and its line for a name that is
    not a commit
- The `user` fixture runs no client JavaScript, so it cannot test the kept choice or
  the narrow fallback. Check those in a browser.
- Each new test fails against a copy with its code broken (CLAUDE.md).

## Docs

- `src/symbion/data/help.md`: the commit lines, and "Hover a state chip to see the
  commit, and for `behind`, the commits since", which becomes a click.
- `docs/guide.md`: the GUI list's commit line.

## Left to the builder

- The exact width at which side by side stops fitting, and the cap on the since list.
- `difflib` or `git diff --no-index` for the since diff. The parser takes either.
- How the client's stored choice reaches the render (part 1, "Where the choice
  lives").
- The CSS, after the theme's tokens and washes and the existing cards.
- Whether the commit viewer keeps `git show --stat`. The parsed file list replaces
  what the stat shows.
