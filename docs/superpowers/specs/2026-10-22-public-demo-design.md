# Symbion public demo: a read-only serve with a stopped clock

**Date:** 2026-10-22
**Status:** Designed with the user on 2026-10-22, in conversation. A reviewer's read
the same day settled five points in the text and raised three the user answered:
marked "the user, 2026-10-22" below. Not built.
**Relates to:** the GUI design (2026-09-08), whose pages and controls this design
leaves as they are; the question-options design (2026-10-16), for the option buttons
a read-only page still draws.

## The need

The README, on GitHub and on PyPI, links to a live demo: the real GUI over a real
store, so a reader "can see what it looks like with real data in it" (the user,
2026-10-22). The store is a snapshot of a demo project an agent worked over several
sessions: a part-done arc, open questions with options, pending predictions, a
parked idea, a star, and checks in several states.

A reader clicks around as at their own serve: opens entries, follows a cited id,
filters by kind, reads a doc, tries the buttons. Today `symbion serve` cannot face
the public. Its GUI writes with no login, and two of its controls act on the machine
that serves the page. A snapshot also ages: within weeks every age reads `5w ago`
and every due task reads overdue.

## Goals, non-goals, anti-goals

Goals:

- Every page and control of the GUI works for a visitor, and nothing a visitor does
  changes the store, the project checkout or the host.
- Ages, due phrases and the last wrap read as they did on the day of the snapshot,
  however long it is served.
- The demo runs symbion as built from main, with no fork and no demo-only patch. It
  is upgraded as main changes: "we'll keep it updated as we make changes" (the
  user, 2026-10-22).

Non-goals:

- Current calendar dates. A visitor in February sees `written 2026-10-15 · 6d ago`,
  and the user is fine with that (2026-10-22). Rewriting the dates in a copy of the
  store was weighed and dropped: the dates inside entry ids and body text would stay.
- Logins, accounts, or any write path for visitors.
- A static export. NiceGUI builds each page from the server over a live
  connection, so a saved copy loses every click.

Anti-goals:

- A visitor writes to the store or starts a process on the host, by any control,
  key or crafted request.
- A sparse page. Hiding the write controls was proposed and rejected: "without
  them, it will look a bit sparse" (the user, 2026-10-22). The controls stay, and a
  click teaches instead.
- A demo that reads abandoned: `overdue 40d`, `last wrap 3mo ago`.

## The design

### `symbion serve --read-only`

A new `serve` flag, for the public demo: "it's really just for the demo but only
because I can't imagine anyone using it" (the user, 2026-10-22). So its banner and
notices say demo. Every page draws as it does without it, with every control.

- **A click gives a notice, not an action.** Each control that writes answers with a
  notice that names what it does in a real store, for example: "Read-only demo: in
  your own store, this stars the entry." Dialogs still open and take typing (the edit
  dialog, a new entry, a new arc, the answer field), and their save gives the notice.
  The keys (`x` resolves, `s` stars, `e` edits, `a` answers) act as their buttons do.
- **One refusal below the GUI.** Under the flag, every write refuses where it lands:
  `store._lock`, which every row write, `commit` and `undo` take (the undo preview,
  `apply=False`, included), and the two `api` writes that do not take it, `save_doc`
  and `push`. The builder confirms that no read the serve makes takes the lock. The
  refusal is one exception
  type. The GUI turns it into the notice wherever a handler meets it, so a control
  this design missed still writes nothing; its notice is the generic one.
- **The GUI's writes today**, the list a test holds the flag to: resolve, star and
  answer (`gui/notes.py`), edit (the edit dialog), new entry, undo, arc follow-up and
  new arc (`gui/arcs.py`), seed, arc rename and archive (`gui/pages.py`), doc save
  and the file drop, commit
  and push (`gui/chrome.py`). `tests/test_gui_seam.py` holds the GUI to writing only
  through `api`, so the guard there covers every GUI write.
- **Controls that act on the host or the visitor's machine.** The file viewer's
  reveal button runs a process on the serving machine (`_reveal`), and its "open in
  VS Code" button links a `vscode://` URL to a path on that machine. Under the flag
  both draw and give the notice. `_reveal` never starts a process, and the editor
  button carries no link. On a host with no desktop (no `DISPLAY`, as on a server),
  `file_buttons` draws neither today; the guard is for a read-only serve on a desktop.
- **The flag reaches every process of the serve**, the `--reload` worker included.
- **A banner.** One line in the top bar says the page is a read-only demo of symbion,
  names the symbion version it runs, and links to the README on GitHub. A visitor who lands from a link learns what
  they see and how to get it.
- **Controls that draw only with something to act on stay as they are.** Commit,
  push and undo draw only with pending rows, unpushed commits or an undo level; seed
  only for an arc over a cataloged type. A clean snapshot has none, and the page
  shows them as a real clean store would: "fine without the controls" (the user,
  2026-10-22).
- **Whose view.** `--author` already sets the reader. The demo runs as the store's
  owner, so a visitor sees what waits on the owner, as the owner does.

### `SYMBION_NOW`: a stopped clock

`SYMBION_NOW=<ISO 8601 time with an offset>` makes symbion read that time as now. The
demo sets it to the snapshot's last store commit. Every age is computed on the
server; the browser does no date math (`gui/` holds no `Date` arithmetic, 2026-10-22).

- **One function.** `store._utcnow` becomes the one clock, and it reads the variable.
  The reads that bypass it today go through it: `summary.age_days` and
  `summary.age` (`datetime.now().astimezone()`), `gui/notes.ago`, the recent boards'
  cutoff in `gui/pages.py`. Two clock reads stay real, because they are not readings
  of the store: the serve's start time in `gui/servers.py`, a process record, and
  the week in `cli._list_gotchas`, which prunes cache files by their real
  modification time (corrected while planning, 2026-10-22).
- **A bad value refuses at startup**, naming the variable and the form it takes. It
  never falls back to the real clock in silence: a demo whose clock slipped would
  read `5w ago` with no error anywhere.
- **Writes under it** stamp the stopped time, since they read the same clock. The
  demo writes nothing; a test may use this.
- `docs/reference.md` documents it beside the other settings: for a demo or a test.

## Tests

- Under `--read-only`, each control in the list above leaves the store's files, its
  git HEAD and the project checkout unchanged, and shows its notice.
- Each `api` write function refuses under the flag. The test enumerates them, so a
  new write function without the guard fails it.
- Under the flag, the reveal button starts no process (patch `subprocess.run` to
  fail the test) and the editor button has no `href`.
- `SYMBION_NOW` set: the summary and an entry page of a fixed store read the same
  ages on two runs a minute apart, and they match a run with the real clock on the
  snapshot's day. A malformed value exits non-zero at startup with the message.
- Before trusting each new test, break the code it guards and watch it fail
  (CLAUDE.md).

## The builder's calls

- Each control's notice wording, and the generic one.
- How the flag reaches the write layer: an environment variable the serve sets for
  itself, or a field on the context. Either must reach the `--reload` worker.
- Where the GUI catches the refusal: in each handler, or once for the app.
- The banner's look, inside the existing top bar and palette.

## Not in this design

The deployment: the host, its web server, the snapshot copies and the release that
carries the README link. They live in the maintainer's notes, since they name one
machine.
