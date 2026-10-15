---
name: symbion
description: "Use when the project has a symbion store (the session-start summary names it) and this session learns, decides, finds broken, leaves undone or asks something a later session will need. Also when asked what is open or what the last session left, at a session's end (wrap, handoff), for a checklist across many files (an arc), on /symbion, or when adopting symbion in a project. Keywords: notebook, ticket registry, audit trail."
---

# symbion

A notebook and ticket registry for this project. An entry is a dated body by an
author, on one or more objects: a commit, a file, an item, an arc or the
project. Entries are read and written only through `symbion`, and live in a git
repo beside the project. A store has several writers: you, the user (at a
terminal or in a browser), other agents, other sessions. A session starts on
the summary of what is open (`symbion summary`; a hook prints it), and ends
with a store commit whose message is the next session's handoff.

`symbion` is on PATH. `symbion -h` lists the verbs, and `symbion VERB -h` a
verb's flags. A command finds its store by `--dir PATH`, else `$SYMBION_DIR`,
else a `.symbion` file in the repo root naming it, else `../<repo>-notes`. A
store outside your writable workspace needs the runtime's approval or the
user's permission: a `.symbion` pointer grants none. A new store in a project
with history starts from `adoption.md`, beside this file.

## Entries

An entry has a kind, a target, a body, an author and a time. It may also carry
tags, refs to other objects, a due date, an arc, and named numbers
(`--measure passed=50`) that a later entry is compared with. This store's kinds:

!`symbion schema`

A kind is a label on three bits, and the bits are all symbion knows:

- **`status`**: the entry is open until `resolve`. Open entries count in the
  session-start summary, arc progress and `arc todo`.
- **`parked`**: an open entry stays out of those views, except a summary line
  when it comes due or someone else wrote it.
- **`verdict`**: the entry records a run, `--checked` what ran and `--result`
  what it said. It is stamped with HEAD, and `list` gives its state:
  - `current`: HEAD has not moved.
  - `behind N`: N commits have landed since. `ahead N`: this checkout is N
    commits short of the stamp. `diverged`: the stamp is on another branch.
  - `dangling` or `unverifiable`: no branch holds the stamp, or git has lost
    it (an amend, a rebase, a squash).
  - `unverifiable (dirty tree)`: the tree had uncommitted changes at the
    write. This never clears.
  - `external`, with its age: `--external` stamps a run on something outside
    the repo (a host, a live database) with its time, not HEAD.

  A supersede keeps the stamp: it corrects the record of that run, and a new
  run is a new entry.
- **`status` + `verdict`**: a pre-registration. It reads `pending` until
  `resolve --result` closes it, stamped at the commit it was judged at. Its
  body and `--checked` as registered are fixed: a supersede may only add
  below them.
- No bits: a plain entry. `symbion context` with no target lists every plain
  entry; `supersede <id> --add-tag retired` takes one out of it, and out of the
  gotcha hook.

`--due DATE` goes on a status entry, and the date is due through its end. An
open entry past due or due within 7 days opens the session-start summary, and
a parked one does from its due date on. Times are UTC unless `TZ` is set.

Tags match exactly. Three mean something to symbion: `gotcha` (the hook
below), `priority` (a star at session start until the entry is resolved or
untagged) and `retired`. `--ref TYPE:NAME` puts an entry on a second object as
well as its target.

## Targets

A target is `TYPE:NAME`:

- `file:PATH`, or another type under `[catalogs]` in `symbion.toml`: a name
  from that type's catalog, a shell command that lists its names. `symbion
  schema` lists this store's.
- `commit:SHA`.
- `item:NAME`: anything with no catalog, such as a feature, a host or a test.
  The name is the key: a near spelling is a new item.
- `arc:ID` and `project`.

A catalog name resolves to the exact name, else a unique substring, else the
name equal but for case. Several substring matches refuse. A name that matches
nothing is a new target, stored as typed: a catalog guides a name, and does not
limit it. `add` names each of these on stderr.

A `file:` path (`./x.py`, `../x.py`, absolute) is read from where you stand. A
bare name (`x.py`, `src/x.py`) is matched from the repo's root. A target is a
name: after a file moves, `symbion rename OLD NEW --type file` moves its entries.
A store over several repos has a type per repo instead of `file`: see
`catalogs.md`.

## Ids and edits

`add` prints an entry's id, and the entry keeps it through every edit: show, cite,
supersede and resolve it by that id. Commands take a unique tail of an id:
cite the 10-character tail (`412907-3be`). An id cited in a body is a link:
`show` lists each cited entry with its state, and each entry that cites this one.

`supersede <id>` edits an entry: `--body` rewrites it, `--append` adds below,
`--prepend` adds above, and tags, refs, the target and the due date change alike.
`resolve <id>` closes a status entry, and its `--body` goes below. A committed
entry is never deleted, and `list --all` shows its earlier versions. `undo <id>`
takes back this session's last write while no commit has run since.

`mechanics.md`, beside this file, says which edits keep an earlier version and
what each summary line and label means. Read it when symbion's output shows
something this file does not explain.

## Authors

You write as your harness's name (`claude`, `codex`), as every session of it
does, so another session's entries count as yours. `$SYMBION_AUTHOR` overrides
that, and `--author NAME` sets it for one write. Every other author is someone
else: the user (as their `git config user.name`), other agents, a subagent you
name. The session-start summary sets someone else's entries apart, and leaves
your parked entries out until they come due.

An entry in the user's words is theirs: `--author "$(git config user.name)"`. A
subagent, or another model you hand work to, writes under its own name
(`--author sonnet-review`).

## Saving

A write is pending until `symbion commit`, and on this disk only until
`symbion push`. `commit` takes the entries this session wrote, under any author,
and the store's changed files; `--all` takes other sessions' entries too, and
`--dry-run` lists what it would take.

## Reading

`list --grep PATTERN` finds entries, `show <id> <id>…` reads them in one call,
and `context --target TYPE:NAME` gives every entry on one object. `list --overdue`
and `--due-by DATE` compare due dates. With `--json`, `list -h` names the keys; a
guessed key reads `null`.

An entry is what was true when it was written. An entry on a file says how many
commits have changed the file since (`commits_since`), and a check says how
far HEAD has moved. Before you give the user an entry's count or state as
current, re-run what it names.

## Writing

An entry holds what a later session needs to not redo or misread the work. What
a commit already says is noise there. A `check` is evidence for a claim: the
flaky test passed 50 runs, the new test failed with the fix reverted.

A later session reads an entry as fact, so a wrong entry costs more than a missing
one. A why names what showed it (the function read, the command run, a commit
in `--ref`), or carries `untested:` and the run that would test it. Why the
project is as it is, a choice or a policy, is the user's to say: cite where
they said it, or ask a `question`.

Every later session reads an entry whole, so each write to it, a new entry or an
addition, is a few sentences, about 500 characters: the claim, what showed it,
what to do. It names a function, not a line. Detail goes in a file you write
under the store's `docs/`; the entry names it by its path from the store's root
(`--ref doc:docs/NAME.md`), and `symbion commit` takes the file. When the fact changes,
`supersede --body` rewrites the entry; `--append` adds to it. `list --grep`
finds an entry before you write its twin.

A trap, such as a test that passes alone and fails in its file's run or a step
that must come before an edit, goes on its file tagged `gotcha`: in Claude
Code, the first call in each session that names the file prints its first 200
characters, so the trap and its fix fit in them. One trap is one entry; a
how-to is a plain note.

A `check` reaches no open view, so a running job also needs a `task` due at
its end, resolved with the result. A result the user reports from this tree
is a `check` whose `--checked` names who ran it; one from another tree or day is a `note`
on its commit. A pre-registration whose window closes unrun takes a new
`--due`, not a resolve.

An open `question` holds the work only if the user's instructions say so; a
default that ships goes in its first 100 characters beside the ask, with how to
flip it. When someone answers your question below it, act on the answer and
resolve the question. `--ref` puts an
object in reach of `context --target`, where prose does not. `priority` marks
what the next session must act on.

`--body` is markdown. A body with backticks or `$` goes through `--body-file -`
and a quoted heredoc (`<<'EOF'`): inside double quotes, the shell runs each
backtick span and expands each `$`.

## Arcs

An arc is a checklist across many objects. `arc create --name NAME` prints its
id. Each unparked status entry filed with `--arc-id ID` is a box, open until
resolved; `arc todo ID` lists the open ones, and the session-start summary
shows each arc's progress. Archiving a finished arc is the user's: until then
the summary shows it done. `arc -h` lists the rest. Before `arc seed`, `arc
reconcile`, or a change to `[catalogs]` or `[resolvers]`, read `catalogs.md`
beside this file.

## Session end

The store is the project's one open list. A session ends when the user says
so, and its wrap (`/symbion wrap`, or any tool's wrap) follows `wrap.md`,
beside this file.

## Bugs in symbion

A bug in symbion, or a workaround for one, goes in an issue drafted for
https://github.com/phreakocious/symbion/issues, which the user decides to file,
unless their instructions name another place. Neither goes in an entry or in the
project's instructions.
