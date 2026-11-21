# Each version names who wrote it

**Date:** 2026-11-20
**Status:** Designed with the user on 2026-11-20, in conversation. A reviewer's read
the same day raised three points, Codex's review two, Gemini's (through agy)
five, and a last read against the code eleven; each is settled in the text. The
calls made without the user are marked "my call". Built the same day, after the
user kept the GUI badge author-only.
**Relates to:** shared stores' `side` field, which stamps the writing clone on each
version; the unknown-key passthrough of 2026-10-19 (2f6e14c).

## The need

An entry's author is a name any caller sets with `--author`, and SKILL.md tells an
agent to write the user's words under the user's name. Nothing in the row says who
ran the write.

- On 2026-11-20 an agent recorded an answer the user gave by acting, not in words,
  and wrote it with `--author` set to the user's name. The version read as the
  user's own words, and the summary's `last by` line read as the user's activity.
  Nothing could mark it afterwards: the `-- X added below --` line comes from the
  row's author, and `supersede --replace` reaches only the body.
- A script can act on an entry only when its head's author is a person's name,
  as an approval gate. An agent that writes with `--author` set to that name
  passes the same check (2026-11-17).

The user expects to be the main reader of the stamp, and a script that gates on
the author to be the second (2026-11-20). Asked whether agents should still write under the user's name, the
user chose to keep relaying, with a stamp (2026-11-20).

## Goals, non-goals, anti-goals

**Goals**

- Every version this release writes records its writer: an agent's name, `gui`, or
  `cli`.
- A person reading `show`, the summary's labels or the GUI sees `phreakocious via
  claude` where an agent wrote under another name.
- A program reads the writer from `--json` and can tell a known agent's write, and
  a GUI write, from the rest.
- A stamp that an older install copied forward reads as unknown, never as the
  earlier version's writer.

**Non-goals**

- Proof. The writer comes from environment markers. An agent that strips them, or a
  person who sets them, is not caught. An agent harness symbion has no marker for
  reads `cli`, as a person does. Any local process can reach serve, so `gui` is no
  proof either. The stamp catches mistakes, not intent. It reads the markers in
  `shell_author`'s order, so a Pi started from a Codex or Hermes shell reads
  `codex` or `hermes`, as its author does today.
- Model names (the user's idea, 2026-11-02). The stamp could carry one later where
  a harness names its model; Claude Code names none to its shell.
- Which name an agent gives the user's words: the CLI takes the project's git
  `user.name`, the GUI the store's. That decision is the user's, and separate.
- Which rows `commit` takes. It still follows the session record. A script's
  `commit --author NAME` is its own fix.
- Back-filling rows written before this release.
- The store commit's `Rows:` and `Wrap:` trailers. They keep naming authors only
  (my call: the rows hold the stamp, and a trailer is a count).

**Anti-goals**

- A warning or a refusal on `--author`. It fires on the use SKILL.md asks for.
- Noise. A version whose writer and author agree, and a person's write at the GUI
  or a terminal, print nothing new.
- A knob. No flag and no documented variable sets the writer.

## Approaches

1. **Chosen: a stamp tied to its version.** The stamp carries a copy of its
   version's `created_at`, and counts only where the two match.
2. **A plain `writer` field.** A few lines less. Since 2026-10-19 an install keeps
   a key it does not know and writes it back on an edit, so an older install's
   edit copies the previous version's writer onto its own. The copy can open the
   gate: an agent's edit of a version the user wrote at the GUI reads `gui`.
   Shared stores accept this for `side`, and `pull` warns. A publish gate should
   not.
3. **Derive the writer from git or the session record.** One store commit takes
   rows from many writers, and the session record lives on one machine and is
   dropped a week after commit.

## Design

### The field

Each version this release writes carries:

```json
"writer": {"name": "claude", "at": "2026-11-20T18:01:03+00:00"}
```

`at` is a copy of the version's own `created_at`. `name` is one of:

- An agent's name, from the markers `api.shell_author` reads, in its order:
  `codex`, `hermes`, `antigravity`, `pi`, `claude`. `$SYMBION_AUTHOR` does not
  count: it names the author.
- `gui`: written by `symbion serve`, whatever the shell that started it holds.
- `cli`: no agent marker symbion knows. A person at a terminal, a script, or an
  agent harness symbion has no marker for.

Considered and dropped (my call, 2026-11-20): splitting `cli` by whether the
process has a controlling terminal. Every agent symbion supports sets a marker,
so the split would only tell people from scripts, and a script writes under its own
author. untested: whether an unmarked agent started from a terminal gives its
shell one. A Claude Code shell has none (measured 2026-11-20).

### Reading it

One function gives a version's writer, and every surface reads through it: the
stamp's `name` when its `at` equals the version's `created_at`, else None
(unknown). A row written before this release reads None.

Since 0.11.0 (d736c39, 2026-10-26) every edit, committed or not, gives the head a
fresh `created_at` and moves the version it replaces to a kept id (measured
2026-11-20 in a scratch store), so a stamp copied onto a later version fails the
match.

Limits, noted, not handled (my call: each needs two installs writing one row):

- `created_at` is to the second. An older install's edit within the same second as
  the version it replaces keeps a matching `at`.
- 0.9.0 and 0.10.0 keep unknown keys but rewrite an uncommitted draft in place,
  keeping its `created_at`: a same-author edit, before a commit, in the same agent
  session or with neither in one. Such an edit keeps the stamp it found, so a GUI
  draft that a 0.10.0 agent with no session key amended under the same author
  still reads `gui`.

### Writing it

Three places mint a version in `store.py`: `add_many`, `_supersede_unlocked` and
the arc seed. Each sets `writer` beside `side`, for every row it mints. Resolve,
tag edits and rename sweeps go through `_supersede_unlocked` and are stamped with
whoever ran them.

The name is read from the environment at write time, as `store.session_key` is.
`api.shell_author` and the stamp read one list of markers, in one function that
`store.py` holds, since `store` cannot import `api` (my call: a second copy of the
order drifts at the next marker).

`serve` sets its own process's writer to `gui` at start, before anything renders,
so `--reload`'s worker inherits it, as it does `SYMBION_READ_ONLY`. How is the
builder's call; it stays undocumented.

`pull` keeps each merged row as written, its stamp included. `add --from-json`
already refuses a `writer` key: its allow-list (`cli._ROW_KEY_ORDER`) takes no key
outside the input shape. Its refusal's hint, "symbion mints id, created_at,
provenance and target_blob itself", names `writer` too.

The twin check (`store._as_written`) drops `writer.at`, as it drops `provenance.at`:
with the time in, a retried add never matches its twin and is stored twice.

Arcs and board moves carry no stamp. An arc keeps its creator, as today.

### Showing it

Beside an author, print `via NAME` when the writer is an agent and differs from the
author (my call on the words: "phreakocious, written by claude" doubles the
`written` that `show`'s head line already opens with). A version's `via` is that
NAME, else None: for an unknown writer, `gui`, `cli`, and an agent writing as
itself. Any other author an agent writes under prints it too, a subagent's role
name or a session label included (`reviewer via claude`; my call: it is true, and
a rule that tells a person's name from a role's would guess).

- `show`, at the terminal and in the agent's lines: `written 2026-11-20 18:01Z by
  phreakocious via claude`, and in `body changed … by` and `… changed … by`
  (`summary.written`).
- The seam lines: `-- phreakocious via claude added below, … --`
  (`summary.seams` carries the writer beside the author).
- Every summary spot that names who wrote a version (my call: the incident was a
  `resolve --author`, which the summary prints as `closed by`): `from`, `last by`,
  `closed by` (the summary's closed block and `cli._print_news`), `X added … :`
  (`added_by`, in `added_earlier` too), `X starred it` (`changed_by`), the
  `people` block's author, and the wrap line `since the wrap: ID what by X`
  (`wrap_changes`), which prints at a terminal too.
- `from` and `last by` print today only where the summary has a reader other than
  the author (the hook, `--json`). A label with `via` prints for every reader: the
  person at a terminal, whose summary has no reader, and the GUI's user, whose
  name the agent used (my call, after Gemini's review: the reader whose name was
  used is the one who must see it).
- The GUI's note view, wherever it prints a version's author.

Where a rule compares names only to decide what to print, `via` joins the
comparison: two versions differ when their author or their `via` does. The raw
writer never joins (my call: compared raw, a row written before this release and
edited after it, unknown then `cli`, and a GUI row amended at a terminal, `gui`
then `cli`, would each print a new label, though neither prints a `via`).
`summary.who` keeps `last` when it differs from `raised` by `via` alone. Example:
the user raised it at the GUI, and an agent answered it under the user's name.
`summary.who` also prints `from` or `last` with `via` where `wrote_it` reads False
for the reader. `summary.written` prints the two-line form when the root's and the
body's authors agree but their `via` differs. A `closed by` drops a `last by` of
the same name (the closed block, `cli._print_news`) only when their `via` agrees
too. `summary.added` keeps one amendment per author and `via`, not per author.

`via` compares the writer with the version's author as stored, before any
decoration. In a shared store, `summary.seen_from` rewrites another side's author
to `claude (B)` on the row itself. Compared after that, an agent's own write would
print `claude (B) via claude`. So the comparison reads the stored author, and the
decorated one only prints: `phreakocious (B) via claude` where that side's agent
wrote under the user's name.

`via` changes no rule about whose a row is (my call, following the user's pick on
2026-11-20 to keep relaying under their name). `wrote_it`, waits, asks, the
reader's `since`, the `people` and `closed` counts, and the GUI's badge of rows
changed by others (`chrome.py`'s `store_state`) read the author alone. So an
agent's write under the GUI user's name lights no badge; the user sees its `via`
where the row prints (my call). This is unlike `seen_from`, which folds a shared
store's side into the author.

`list --json` carries `writer`: the name as read above, or null (my call: a program
wants the verdict, not the raw stamp; the raw stamp stays in `notes.jsonl`).
`summary --json` keeps every name string as it is. Where `via` prints, a sibling
key holds the writer; where it does not, the key is absent, as `row_digest` omits
an absent `from` (my call: a consumer that compares `from == NAME` keeps
matching). The keys: `from_via`, `last_via`, `closed_by_via`, `added_by_via` (in
`added_earlier`'s entries too), `changed_by_via`, `people[].author_via` and
`wrap.changed[].by_via`.

`term.py` is what a person sees, `cli._print_note` the lines agents parse: after the
change, diff `list --all --full --limit 0` against HEAD's on an existing store:
no row there has a stamp, so expect no diff. Then write one relayed row and check
that its lines change only by `via`.

### Docs

- SKILL.md, Authors: one sentence. symbion stamps who ran each write, and `show`
  prints `NAME via claude` where an agent wrote under another name.
- mechanics.md: the stamp, its three kinds of name, and when it reads unknown.
  Its `Arcs:` trailer text says an arc edit counts "for its writer", meaning
  `changed_by`, an author name: that sentence says "author" after this change, so
  the file gives "writer" one meaning.
- docs/reference.md: the row key, the `--json` key, and the older-install case.
- `--author`'s help: the writer is stamped beside it.

### Tests

Each one fails with its code broken (CLAUDE.md):

- Each mint path stamps: add, supersede of a committed and of an uncommitted row,
  resolve, a tag-only edit (the CLI's fast path to `api.retag`), arc seed, a rename
  sweep.
- A retried add is still refused as a twin.
- A GUI write reads `gui` with `CLAUDECODE` set in the serve's environment.
- `$SYMBION_AUTHOR` changes the author, not the writer.
- A stamp whose `at` differs from its version's time reads unknown: a row line
  with a copied stamp, as an older install's edit writes it.
- `via` prints only where an agent wrote under another name.
- Two same-author chains print nothing new in `show` or the summary: a row
  written before this release (a line with no stamp) and edited after it, and a
  GUI row amended at a terminal.
- A row the user raised at the GUI and an agent answered under the user's name
  prints `last by phreakocious via claude`, and still does not wait on the user.
- `resolve --author` by an agent prints `closed by phreakocious via claude`, and
  keeps a `last by` that differs from it by writer alone.
- A row an agent raised under the reader's own name prints `from phreakocious via
  claude` to that reader, and in a terminal summary.
- `context`'s text output prints `via` (it reads `json_row` back through
  `note_from_dict`).
- In a shared store, another side's agent writing as itself prints `claude (B)`
  with no `via`; writing under the user's name, `phreakocious (B) via claude`.
- `add --from-json` with a `writer` key prints a hint that names `writer`. The
  refusal alone passes before the build.

## Traps for the builder

- `writer.at` copies the row's `created_at` once it is set, never a second
  `_now_iso()`: a write across a second's boundary would read unknown at once.
- `seen_from` overwrites `Note.author`. Keep the stored author on the Note, as an
  unserialized field like `first_written`, or decorate another way. Never strip
  ` (B)` from a name.
- `json_row` serves `list`, `context` and `arc todo --json`, and `context`'s text
  mode reads its output back through `store.note_from_dict`. Either the round-trip
  carries the stamp, or the reader takes a name string as well as a stamp.
- `serve.main` leaves `CLAUDECODE` and `AI_AGENT` in its environment: its `gui`
  setting goes ahead of every agent marker.
- The GUI tests build the app with `build_page`, never through `serve.main`. An
  override set only in `serve.main` is out of their reach: the `gui` test reaches
  it another way.
- `tests/conftest.py` clears every agent marker for each test. A test of an agent's
  stamp sets its marker with `monkeypatch`.

## Left to the builder

- Function names, and where serve's override lives.
- Each GUI spot that prints an author.
- Whether `list`'s one-line rows need the writer (today they print no author).

## Follow-ups, not in this build

- `commit --author NAME`, so a script without an agent session commits what it
  wrote (2026-11-17).
- With `gui` on a pending row, `commit` could say taking it is safe: a GUI write is
  complete when made (2026-11-19).
- A script that gates on the author checks the writer too. `writer ==
  gui` holds against any agent that does not drive the GUI, and `writer in (gui,
  cli)` only against the agents symbion has markers for. Neither holds in the two
  cases under Limits: an edit by a 0.9.0 or 0.10.0 install before a commit, or
  within the same second, keeps a `gui` stamp it did not earn. Neither case arises
  while every writer of the store runs one symbion of 0.11.0 or later.
