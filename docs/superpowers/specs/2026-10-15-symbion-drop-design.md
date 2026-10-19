# Symbion drop: a file dropped on a GUI page

**Date:** 2026-10-15
**Status:** Approved in conversation 2026-10-15, in two sections, and as written the
same day. Built 2026-10-15. The page event is `sbDrop`: NiceGUI names a `ui.on`
type in camelCase.
**Relates to:** the GUI design (2026-09-08) and its composer; a doc's name is its
path within the store's `docs/` (2026-10-15).

## The need

The user's idea, 2026-10-15: drag a file onto the GUI. A huge markdown file is
offered to the store's `docs/`. Anything else becomes the body of a new entry. A
file the serve knows becomes the new entry's target. With the composer open, the
text goes into it. Images may come later.

Today a file dropped on a page makes the browser open the file in place of the GUI.

## What a drop does

| Composer | Dropped file | Result |
|---|---|---|
| closed | known | the composer opens with that file as its target |
| closed | other text, 2,000 characters or fewer | the composer opens with the text as its body |
| closed | other text, longer | the offer: Save to `docs/` (then the composer, with `doc:NAME` in refs), Use as body, or Cancel |
| open | known | the file joins refs |
| open | other text, 2,000 characters or fewer | the text goes in at the cursor |
| open | other text, longer | the offer; a saved doc joins refs |

- **Known** means: the same base name and the same text as exactly one file that a
  catalog lists, the repo's files or the store's `docs/`. A browser gives a dropped
  file's name, never its path, so the content decides. A file with the same name
  and other content is a stranger. When several files match (two empty
  `__init__.py`), a notice names them and the drop counts as other text.
- **Longer** means over 2,000 characters (`DROP_DOC_CHARS`). SKILL.md puts an
  entry at about 500 characters and its detail in `docs/`. This holds for any
  text, not only markdown: a long log is detail too (the user, 2026-10-15).
- **The offer** names the doc in a field, the file's name to start with. Save
  writes the doc and refs it as `doc:NAME`. Use as body goes on as short text does.
- **A draft counts as open.** A composer closed with Esc keeps its draft (`_once`
  in `gui/chrome.py`). A drop opens it and takes the "open" rows of the table, so
  no drop overwrites a draft.
- Except for a known file, which is its own target, the composer opens on the
  page's object, as the New entry button's does.
- *2026-10-15, after review:* text that is not markdown (`.md`, `.markdown`) goes
  in as one fenced code block, in the language its name implies, on lines of its
  own. The composer reads `#word` as a tag and `!kind` as the kind, and takes them
  out of the body: a dropped CSS file lost `#main` and `#fff`, and a C file its
  `#include` lines. A fenced block is left alone.

## Limits on a drop

- One file. A drop of several gets a notice.
- Text only. A file that is not valid UTF-8, or holds a NUL, gets the notice
  "text files only".
- At most 500,000 bytes (`DROP_BYTES`). NiceGUI's socket server keeps
  python-socketio's 1 MB limit on a message, and the file viewer refuses a file
  over 500,000 characters.
- Every page takes a drop. While a file is dragged over the page, an overlay says
  what a drop does. With another dialog open (an edit, the viewer), a drop gets a
  notice and changes nothing.

## Saving a doc

`api.save_doc(ctx, name, text)` writes `docs/NAME` in the store and returns the
doc's name.

- The name stays inside `docs/`: relative, no `..`, and no part that starts with a
  dot. A dropped `.gitignore` would change what git sees in `docs/`.
- It never overwrites. A name in use with the same text returns that name and
  writes nothing. With other text it refuses, and the offer proposes the first
  free `NAME-2.md`, `NAME-3.md`.
- It creates the file exclusively (`open(path, "x")`), so two writers cannot both
  create one name. It makes the parent directories.

The `doc` catalog lists untracked files, so `doc:NAME` resolves as soon as the
file is written.

## Committing a doc

The GUI's commit takes only its author's rows (`api.commit` passes
`files=False`), never files: an agent's half-written file is not the person's to
commit. A doc saved from the GUI would then wait for an agent's `symbion commit`,
and a pushed entry would name a doc that the remote lacks.

So `api.commit(author=…)` also stages each untracked `docs/` file named, as
target or ref, by a row it commits. A doc that no entry names stays uncommitted,
as an agent's file does.

*2026-10-15, after review: untracked only, and read literally.* "Each file named"
took another writer's uncommitted edit to a tracked doc, since a resolve copies its
base's refs; read a name like `[ab].md` as a glob; and refused the whole commit
over a doc the store's `.gitignore` ignores.

## Transport

The browser reads the file and sends its text with `emitEvent('sb-drop', …)`. The
page's `ui.on('sb-drop', …)` takes it. Every GUI write already goes over this
per-page socket, so there is no new route.

Rejected:

- **A new POST route.** It is a write that any website could post to on
  localhost, unless it gets its own guard against cross-site requests.
- **A hidden `ui.upload` fed by the drop** (NiceGUI's per-client HTTP route). It
  takes binary files of any size, but it has more parts. It is the path to
  images, and the code marks it with a `ponytail:` comment.

## Where it lives

- `gui/chrome.py`: `_DROP_JS`, beside `_KEYS_JS`. It stops the browser's own
  handling of a dropped file, shows the overlay, applies the limits, reads the
  composer's cursor when the composer is open, and sends one event.
  `_new_note_button` owns the composer, so it handles the event.
- `gui/notes.py`: `add_form` returns a handle. The handle sets the target, sets
  the body or inserts at a cursor, adds a ref, and says whether a draft exists.
- `api.py`: `known_files(ctx, name, text)`, `save_doc`, and the doc staging in
  `commit`. `known_files` tries every catalog type. It decodes each candidate as
  the browser decodes the drop: UTF-8, a leading BOM dropped, CRLF kept. The path
  of a target's file, which the viewer's `view_path` computes today, moves to
  `api.object_path`, and both use it.
- `data/help.md`: a line on dropping a file.

## Tests

- `known_files`: one match, two identical copies, the same name with other
  content, a `docs/` file, and CRLF and BOM files.
- `save_doc`: a write, an identical name reused, other text refused, and `..`, an
  absolute name and a dot part refused.
- Commit: the GUI's commit takes a doc its entry names, and leaves a doc that no
  entry names.
- Each row of the table and each limit, through NiceGUI's `user` fixture, by
  firing the event.
- In Chrome: a synthetic drop of a `File` leaves the page in place, and the event
  reaches the server. A drag from the OS cannot be scripted; the user tries it.
- Each guard is broken once, to see its test fail.

## Limits

- Text only, 500,000 bytes, one file at a time.
- The thresholds are guesses until the user tries the feature ("I'll have to
  play with it to see how it feels"). Each is one constant.
