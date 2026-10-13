---
name: symbion
description: "Use when recording or retrieving a durable, dated, object-attached conclusion about this project — a test/build check (checked/result), an ADR-style decision, a known-broken bug, or a ticket task — instead of letting it evaporate in chat. Also for running a project-wide campaign as a per-object checklist (arcs), or when explicitly invoking symbion. Keywords: notebook, ticket registry, audit trail, queryable notes, bootstrap an existing project."
---

# symbion — this project's notebook and ticket registry

Dated notes on a commit, a file, an item, an arc or the whole project, kept in
a git repo of JSONL beside the project, so a conclusion outlives the chat.

Run the installed `symbion`, never `python -m` or a venv path. If `command -v
symbion` (PowerShell: `Get-Command symbion`) fails, see Install at
https://github.com/phreakocious/symbion.
`symbion VERB -h` lists a verb's flags.

At the start of a session, run `symbion summary` if no summary was supplied
by the hook, including after compaction. A `last wrap by …` line in it is the
previous session's handoff: read its paragraph first, then the relevant rows,
before starting work. If a write is blocked because the sibling store is outside the writable
workspace, use the runtime's approval mechanism or have the owner add that
store to the workspace permissions; a `.symbion` pointer does not grant access.

## `/symbion KIND TEXT`: a row from chat

When the skill loads with arguments and their first word is a kind that
`symbion schema` lists (`/symbion idea retries should back off`), file one
row of that kind now, with no round trip to confirm. With any other first
word, the arguments are an ordinary request (`/symbion what is open`), except
`wrap`: `/symbion wrap` is the session end, in `wrap.md` beside this file.

- **Author: the person who typed it**, never you: `--author "$(git config
  user.name)"` (PowerShell: `--author (git config user.name)`). The words are
  theirs, and a parked row you write as yourself never prints at your own
  session start.
- **Body: their text, verbatim.** Context you add goes after a blank line,
  opened with your name and the UTC date the row is stamped with
  (`Claude, 2026-10-11: …`).
- **Target: the file or item under discussion**, else `project`.
- **A verdict kind** (`check`) also needs `--checked` and `--result`: ask the
  person for both, never make them up.
- **Report it in one line**: the kind, the target and the id, so a wrong
  target shows at once.

## Kinds

This store's kinds, their bits and row counts, as `symbion schema` prints
them (where no table follows, run it before a write):

!`symbion schema`

A kind is a label on three bits, and the bits are all symbion knows:

- **`status`**: the row is open until `resolve <id>`. Open rows count in the
  session-start summary, arc progress and `arc todo`.
- **`parked`**: open rows stay out of those views, except that the summary
  shows one due soon or written by someone other than the reader. Close one
  with `resolve <id> --add-tag adopted` or `--add-tag retired`, and a `--body`
  saying why.
- **`verdict`**: `--checked "pytest -k upload --count 50"` (what ran) and
  `--result "50 passed"` (what it said). The row is stamped with HEAD, and `list` reports it
  `current`, `behind N`, `ahead N` or `diverged` (on another branch), or
  `dangling` when no branch or tag holds its commit (an amend or a rebase
  after the run): re-run it. The stamp says the run was at
  this HEAD: a result the user reports from this tree is a `check` whose
  `--checked` says who ran it; one from another tree or day is a `note` on its
  commit. A check on something outside the repo (DNS, a host's logs, a live
  database) takes `--external`: it is stamped with when it ran, since
  otherwise any later commit makes it read `behind N`. A supersede keeps the
  stamp, so it corrects the record of that run: a new run is a new row. A
  row migrated from a store that predates stamping reads `unstamped`, with
  its age. A number a later row will be compared with also goes in
  `--measure passed=50` (repeatable, any kind): `--json` gives it under
  `measurements` as a number, where `--result` prose holds only text.
  `resolve` and `supersede` take `--measure` too: a name given replaces that
  name, and the others are inherited.
- **`status` + `verdict`** is a pre-registration, and `prediction` is the
  default kind with both. A `[kinds]` table written before it may hold none:
  if `symbion schema` lists no kind with both bits, add
  `prediction = { status = true, verdict = true }` under `[kinds]` in
  `symbion.toml`. File it before the data exists: `--checked` names the run and its control, the body
  states the prediction and what would falsify it, and `--due` is the date its
  window closes. Open with no result, it lists as `pending`; `resolve <id>
  --result "…"` closes it and stamps the commit it was judged at, and
  `--measure` there carries the counts it scored. A resolve with no result
  is refused. Amend it with `supersede <id> --append --body-file -`, which
  keeps the registered text as an unchanged prefix. If the window closes with
  the test unrun, amend it with a new `--due` and say what did not happen; do
  not resolve it, because any result scores claims the run never reached. A
  finished result is not a pre-registration: it goes on a verdict-only kind,
  and a part still to verify is its own status row.

A kind with no bits is plain: a dated body on a target. When a plain row stops
being context (a progress log, a "built at" marker), `supersede <id> --add-tag
retired` takes it out of the default `context` view.

`--due DATE` (`YYYY-MM-DD` or an ISO datetime) goes on any status row. A date
is due through the end of its day. `--json` gives `due` as a date, or as a
datetime with its offset (UTC's, or TZ's when set, if none was typed), not
as typed, so let `list --overdue` or `list --due-by DATE` compare them. An open
row past due or due within 7 days opens the session-start summary, parked or
not, so it is also how an `idea` gets a revisit date. A date only in the body
reaches nothing. `supersede <id> --due ''` clears it. Dates and times are UTC,
in what symbion prints and in what it reads, unless `TZ` is set.

## Writing

**Record what a future session needs to not redo or not misread the work.** A
row that restates what the commit says is noise: a suite run that only says
"412 passed" goes in the commit message, not a `check`. A `check` is evidence
for a claim, dated and re-checkable: the flaky test passed 50 runs, the new
test failed with the fix reverted, the suite passed on Linux too.

**A claim about why names what showed it:** the file and line you read, or
the command you ran. Why a thing breaks is a cause. Why the project is as it
is (a choice, a policy, "deliberate") is an intent, and only the owner can
say it: name the file that says so, or ask it as a `question`. A cause you
reasoned to and did not test goes in with `untested:` before it and the run
that would test it. This holds in a wrap message too. A later session reads
a row as fact, so a wrong row costs more than a missing one.

**Search before you write, and before you say the store lacks something:**
`list --grep 'a|b'`.

**A job still running needs an open row.** A verdict-only row (`check`)
reaches no open view, so one that records a launch (a day-long capture, a
long benchmark) is forgotten when the job ends. Pair it with a `task` whose
`--due` is the expected finish, and resolve that with the result.

**A trap goes on the file it concerns, tagged `gotcha`.** A trap is what a
session pays for learning late: a test that passes alone and fails in its
file's run, a step that must come before an edit. In Claude Code, the first
time a session's Read, Edit, Write or Bash call names that file, a hook gives
the agent the row's first 200 characters, so lead with the trap. It matches a
target or `--ref` of a catalog type such as `file`, never an `item`.

**A revision updates its head.** `add` names the rows already open on its
target, on stderr. If the new row settles or revises one, `resolve` or
`supersede` that one; otherwise both print at every session start.

**Lead a status row with its claim or its ask.** Session start prints an open
row's first 100 characters. Evidence and provenance follow the point.
Text added with `--append` never reaches that line, which then reads `+1
amendment`. To correct the lead of a row that is not a pre-registration,
put a new one above it with `supersede <id> --prepend --body …`, or rewrite
the whole body with `--body`. Every edit keeps the row's id. Until
`symbion commit`, a supersede of a row you wrote in this session that no
other row cites changes it in place; a resolve, and any other edit, keeps
the earlier version under a new id. To keep your draft's earlier version,
commit first. `undo <id>` takes back your last write whole, an edit in place
included, while it is the store's last write and no commit has run since.

**A review you hand to another model or a subagent writes under its own
name.** The default author names the harness (`claude`, `codex`), never the
model, so its rows and the rows of the session that checks them read alike.
Tell it to pass `--author` (`--author sonnet-review`).

**While your question is open, the user's instructions decide** whether the
decision it asks about waits for the answer or ships with a default. If it
ships, lead the question with what shipped and how to flip it.

`--body` is markdown. A body with backticks or `$` goes through `--body-file -`
and a quoted heredoc (`<<'EOF'`): in `--body "…"` or an unquoted `<<EOF` the
shell expands each `$name` and `$(…)` and runs each backtick span as a command,
silently.

```bash
symbion add check --target "item:flaky upload test" --checked "pytest -k upload --count 50" --result "50 passed"
symbion add decision --target file:src/x.py --body "kept the O(n) scan; n is bounded by config, not input"
symbion add bug --target "item:flaky upload test" --body "fails ~1/20 on CI, not locally" --tag ci
```

A target is `commit:SHA`, `item:NAME`, `project`, `arc:ID`, or a catalog type
the store's `symbion.toml` declares (`file` above; `symbion schema` lists this
store's). `--tag` is repeatable and matches exactly. `--ref TYPE:NAME`
(repeatable) attaches the row to a second object, so `context --target` on
that object finds it: use it instead of naming the object in prose. `resolve
<id> --ref commit:SHA` records the commit that closed a row.

Many rows: `add --from-json -`, one JSON object per line, with the keys
`add -h` lists. Nothing is written unless every line passes, and every
failing line is named.

```bash
symbion add --from-json - <<'EOF'
{"kind": "task", "target": {"type": "item", "name": "flaky upload test"}, "tags": ["ci"], "refs": [{"type": "commit", "name": "dd1e48b"}]}
EOF
```

`add`, `resolve`, `supersede`, `arc create` and `arc seed` print the ids
they wrote on stdout: capture them (`nid=$(symbion add …)`). `show`, `list --id`,
`resolve` and `supersede` take a unique tail of an id, and an earlier
version's id acts on its chain's head, so a cited id does not go stale. Cite the
10-character tail (`412907-3be`); 3 characters are often shared. To link a
row to another row, cite its id in the body: `show` lists each cited row
with its current status, and each row that cites this one.

## Reading

**Find rows with `list --grep PATTERN`, and read one with `show <id>`.**
`--grep` is a case-insensitive regex over body, target name, checked, result,
refs and `#tags`; `-F` takes it literally. A pattern that is an id or its
tail (`412907-3be`), or several joined by `|`, also lists each row. Never
`list | grep`: text `list` is one 25-row page, so the pipe misses rows and the
silence reads as absence.
The page's first line gives the total and the flag that shows each hidden
set; `--limit 0` shows all. `--json` is paged only by `--limit`. `context
--target TYPE:NAME` is every row on one object.

**A row is what was true when it was written.** A `check` says how far HEAD
has moved since (`behind N`). A row on a file in this checkout says how many
commits have changed that file since the row saw it (`commits_since`, printed
above 0); any other row says nothing. Before you give the user a row's count or
state as current, re-run the check it names.

Use `--json` on `list`, `summary`, `context`, `arc list` and `arc todo`. A row
has `id`, `kind`, `target: {type, name}` (`name` is `null` on a `project`
target), `created_at`, `author`, `body`, `status`, `checked`, `result`,
`arc_id`, `due`, `supersedes`, `tags`, `refs`, `provenance`, `measurements`,
`target_blob`. `list`, `context` and `arc todo` add
`commits_since` (null unless the target is a file in this checkout) and
`raised_by` and `raised_at`, the author and time of the row's first version
(`author` and `created_at` are its newest's); `list`
also adds `state` and `distance` on verdict kinds, and `head` on a superseded
row. There is no `ts` or `title`: a guessed key reads as a silent `null`.
`list`, `arc list` and `arc todo` return a bare array; `summary` and
`context` return an object, and `context` puts its rows under `notes`.
`summary --json` rows are digests (`target` is a `type:name` string, `body` is
clipped). `summary` and `context` have `store`, which is `null` when no store
exists: gate on it, not on counts or the exit code.

```bash
symbion list --status open --json
symbion show <id> --json                 # list --id <id>: its current row; --all, that version
symbion context --target file:src/x.py --json
```

A person may browse the same store at `symbion serve`. Rows retain their
authors whether written through the GUI, a terminal, or another agent. While
one runs, `summary` names its URL and a row's page, `<url>/notes?id=<id>`: give the user that
link when you point them at a row. `summary --json` and `context --json` carry the URL as
`gui`, `null` while none runs.

## Checklists: arcs

```bash
aid=$(symbion arc create --name "Adoption" --scope item --desc "…")
symbion add task --target "item:write the README" --arc-id "$aid" --body "why; what done looks like"
symbion arc todo "$aid" --json     # the open boxes
symbion resolve <id>               # tick one
symbion list --arc "$aid" --json   # every row in the arc, resolved included
```

Any unparked status row in the arc is a box, a `bug` too, and several may
share a target. `arc archive ID` takes an arc off the session-start summary.
Leave a finished arc for the person to archive: its done line there is how
they see it finish. `arc seed` mints one bodiless task per name of a catalog;
before seeding, reconciling, or configuring a catalog or resolver, read
`catalogs.md` beside this file.

## The read loop

A SessionStart hook runs `symbion summary`: open counts, rows due or overdue,
arc progress, rows tagged `priority`, resolved rows you raised or closed that
someone else resolved or wrote to since your last write (`closed by alice`),
the open rows outside arcs, and open rows
someone else wrote. When someone else amended or resolved a row, the summary
and `list` print the first line they added below it (`alice added 3h ago:
…`), or the first line they changed when they wrote no paragraph of their own
(`alice edited 3h ago: …`). An answer to your question arrives this way. `alice added 3h ago, lead
rewritten since:` means the lead changed after it: the line answered an
earlier question, so read the row before you relay it.

An open row someone else wrote is a request to you. Name it to the user
before other work, even when the prompt asks for another kind of row.

To read further:

- `symbion context --target TYPE:NAME`: every row on one object.
- `symbion context --branch <ref>`: every row on a commit reachable from
  `<ref>` since the default branch.
- `--tag priority` at `add`, or `supersede <id> --add-tag priority` later,
  stars a row for the next session. Star what it must act on, not news. The
  star stays until the row is resolved or `supersede <id> --rm-tag priority`
  takes it off; a row with no status has only the second way. From a day
  on, the summary prints each star's age (`starred 12d ago`): unstar a
  stale one.

## Session end

The store is the open list, and the last wrap's commit message is the
handoff. The person decides when a session ends. When they ask for a wrap
(`/symbion wrap`), or whenever any tool's wrap runs, follow `wrap.md`,
beside this file: rows for what the session learned, changed and
finished, then `symbion commit --wrap -m` with the day as its message. A
session-continuity tool's handoff file (`NEXT_SESSION.md`) is that tool's.
Never write the open list into it, and never copy its bullets into rows a
second time: with two copies, the stale one gets read as the truth. With the
continuity plugin (https://github.com/nullphase-net/enfurbish), its `/wrap -j`
writes the retro and the journal and no handoff file (0.21.0 and later). With
no handoff file in the cwd, its bare `/wrap` asks first which was meant, and
its `/next` briefs from the summary (0.8.5 and later). To retire a project's
file, see `adoption.md`, "The handoff file".

A workaround for symbion itself ("`show` does not exist") goes in neither the
handoff nor the project's agent instructions: it outlives the fix. Draft an
issue for
https://github.com/phreakocious/symbion/issues and offer it to the user, who
decides whether to file it. When the user's own instructions name another
place for symbion reports, use that.

## Footguns

- **A write is not in git history until `symbion commit`, and not off this
  disk until `symbion push`.** `commit` never pushes; it says how many
  commits are not on the remote, or that the store has none (unless its
  `symbion.toml` sets `local_only = true`). It takes your pending rows (the
  ones this agent session wrote, as whoever; outside a session, your
  author's) and every changed file in the store, and names them:
  `committed: rows claude 2; 1 file: tools/x.py`. It names the rows it left
  too, and how new the newest is: another session's, still running or
  ended. `commit --all` takes them as well; use it for a session that has
  ended. `commit --dry-run` lists both first and commits nothing.
- **A catalog miss stores your typed string.** A name that matches nothing
  becomes its own target, with only a note on stderr and exit 0.

## Adopting an existing project

The store starts empty and the project does not. Read `adoption.md`, beside
this file, in full before a bootstrap writes its first row.

## Which store

`--dir PATH`, anywhere in the argv; else `$SYMBION_DIR`; else a `.symbion`
file in the repo root naming the store; else `../<repo>-notes`. A store whose
name does not match its repo needs that file. A write to a store `init` never
made is refused, naming the path.

## Quick reference

| command | does |
|---|---|
| `add KIND --target T:N [--body …] [--tag …]… [--ref T:N]… [--due DATE] [--arc-id ID]` | append a row; prints its id |
| `list [--kind K] [--target T:N] [--status S] [--tag T] [--arc ID] [--grep PAT [-F]] [--overdue] [--due-by DATE] [--since 2h] [--all] [--limit N] [--full] [--json]` | heads, newest first; `--all` adds superseded rows |
| `show <id>... [--json] [--all]` | rows by id, as `list --id <id>...` (an array in `--json`); a superseded id shows its head, `--all` that version |
| `resolve <id> [--body …] [--result …] [--measure K=V]… [--ref T:N] [--add-tag …]` | close a row; `--body` goes below the current body, after a blank line it inserts |
| `supersede <id> [--body …] [--append] [--prepend] [--add-tag …] [--rm-tag …] [--tag …] [--add-ref T:N] [--ref T:N] [--checked …] [--result …] [--measure K=V]… [--due DATE]` | correct a row; `--tag` and `--ref` replace the inherited ones, `--add-tag` and `--add-ref` add to them, `--measure` sets one name and keeps the rest; `--append` adds the body after a blank line it inserts, `--prepend` above one |
| `commit [-m MSG] [--dry-run] [--all]` | commit your rows and the store's files; `--all` every writer's rows; `--dry-run` lists what it would take |
| `push` | push the store's commits to its remote |
| `undo <id>` | take back your last write, before a commit (one level) |
| `rename <old> <new> --type T [--to-type T2]` | move every row and ref on a renamed object; `--to-type` changes its type too |
| `summary`, `schema [--toml]`, `tags`, `context` | read |
| `arc create`, `list`, `todo`, `archive`, `seed`, `reconcile` | campaigns (`catalogs.md` for the last two) |
