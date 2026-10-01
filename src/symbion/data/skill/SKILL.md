---
name: symbion
description: "Use when recording or retrieving a durable, dated, object-attached conclusion about this project — a test/build check (checked/result), an ADR-style decision, a known-broken bug, or a ticket task — instead of letting it evaporate in chat. Also for running a project-wide campaign as a per-object checklist (arcs), or on explicit /symbion. Keywords: notebook, ticket registry, audit trail, queryable notes, bootstrap an existing project."
---

# symbion — this project's notebook and ticket registry

Dated notes on a commit, a file, an item, an arc or the whole project, kept in
a git repo of JSONL beside the project, so a conclusion outlives the chat.

Run the installed `symbion`, never `python -m` or a venv path. If `command -v
symbion` fails, see Install at https://github.com/phreakocious/symbion.
`symbion VERB -h` lists a verb's flags.

## Kinds

This store's kinds, their bits and row counts:

!`symbion schema`

A kind is a label on three bits, and the bits are all symbion knows:

- **`status`**: the row is open until `resolve <id>`. Open rows count in the
  session-start summary, arc progress and `arc todo`.
- **`parked`**: open rows stay out of those views, except that the summary
  shows one due soon or written by someone other than the reader. Close one
  with `resolve <id> --add-tag adopted` or `--add-tag retired`, and a `--body`
  saying why.
- **`verdict`**: `--checked "pytest -q"` (what ran) and `--result "412
  passed"` (what it said). The row is stamped with HEAD, and `list` reports it
  `current`, `behind N`, `ahead N` or `diverged`. The stamp says the run was at
  this HEAD: a result the user reports from this tree is a `check` whose
  `--checked` says who ran it; one from another tree or day is a `note` on its
  commit. A check on something outside the repo (DNS, a host's logs, a live
  database) takes `--external`: it is stamped with when it ran, since
  otherwise any later commit makes it read `behind N`.
- **`status` + `verdict`** is a pre-registration. No default kind has both:
  declare one under `[kinds]` in `symbion.toml`, e.g. `prediction = { status
  = true, verdict = true }`. That table replaces the defaults:
  `symbion schema --toml` prints the current one to paste and extend. File it
  before the data exists: `--checked` names the run and its control, the body
  states the prediction and what would falsify it, and `--due` is the date its
  window closes. Open with no result, it lists as `pending`; `resolve <id>
  --result "…"` closes it and stamps the commit it was judged at, and a
  resolve with no result is refused. Amend it with `supersede <id> --append --body-file -`, which
  keeps the registered text as an unchanged prefix. If the window closes with
  the test unrun, amend it with a new `--due` and say what did not happen; do
  not resolve it, because any result scores claims the run never reached. A
  finished result is not a pre-registration: it goes on a verdict-only kind,
  and a part still to verify is its own status row.

A kind with no bits is plain: a dated body on a target. When a plain row stops
being context (a progress log, a "built at" marker), `supersede <id> --add-tag
retired` takes it out of the default `context` view.

`--due DATE` (`YYYY-MM-DD` or an ISO datetime) goes on any status row. An open
row past due or due within 7 days opens the session-start summary, parked or
not, so it is also how an `idea` gets a revisit date. A date only in the body
reaches nothing. `supersede <id> --due ''` clears it. Dates and times are UTC,
in what symbion prints and in what it reads, unless `TZ` is set.

## Writing

**Record what a future session needs to not redo or not misread the work.** A
row that restates a committed result ("tests pass") is noise; a `check` earns
its place because it is dated and re-checkable.

**Search before you write, and before you say the store lacks something:**
`list --grep 'a|b'`.

**A revision updates its head.** `add` names the rows already open on its
target, on stderr. If the new row settles or revises one, `resolve` or
`supersede` that one; otherwise both print at every session start.

**Lead a status row with its claim or its ask.** Session start prints an open
row's first 100 characters. Evidence and provenance follow the point.
Text added with `--append` never reaches that line, which then reads `+1
amendment`. To correct the lead of a row that is not a pre-registration,
rewrite it with `supersede <id> --body`.

`--body` is markdown. A body with backticks goes through `--body-file -` and a
quoted heredoc (`<<'EOF'`): in `--body "…"` or an unquoted `<<EOF` the shell
runs each backtick span as a command, silently.

```bash
symbion add check --target commit:HEAD --checked "pytest -q" --result "412 passed"
symbion add decision --target file:src/x.py --body "kept the O(n) scan; n is bounded by config, not input"
symbion add bug --target "item:flaky upload test" --body "fails ~1/20 on CI, not locally" --tag ci
```

A target is `commit:SHA`, `item:NAME`, `project`, `arc:ID`, or a catalog type
the store's `symbion.toml` declares (`file` above; the schema above lists this
store's). `--tag` is repeatable and matches exactly. `--ref TYPE:NAME`
(repeatable) attaches the row to a second object, so `context --target` on
that object finds it: use it instead of naming the object in prose. `resolve
<id> --ref commit:SHA` records the commit that closed a row.

Many rows: `add --from-json -`, one JSON object per line. The keys are
`list --json`'s, less the ones symbion mints (`id`, `created_at`,
`provenance`) and the ones it computes; `add -h` lists them. Nothing is
written unless every line passes.

```bash
symbion add --from-json - <<'EOF'
{"kind": "task", "target": {"type": "item", "name": "flaky upload test"}, "tags": ["ci"], "refs": [{"type": "commit", "name": "dd1e48b"}]}
EOF
```

`add`, `resolve`, `supersede`, `arc create` and `arc seed` print the new ids
on stdout: capture them (`nid=$(symbion add …)`). `show`, `list --id`,
`resolve` and `supersede` take a unique tail of an id. Cite the 10-character
tail (`920030-8ca`); 3 characters are often shared.

## Reading

**A row is what was true when it was written.** A `check` says how far HEAD
has moved since (`behind N`); a plain row says nothing. Before you give the
user a row's count or state as current, re-run the check it names.

Use `--json` on `list`, `summary`, `context`, `arc list` and `arc todo`. A row
has `id`, `kind`, `target: {type, name}` (`name` is `null` on a `project`
target), `created_at`, `author`, `body`, `status`, `checked`, `result`,
`arc_id`, `due`, `supersedes`, `tags`, `refs`, `provenance`, `measurements`,
`evidence`; `list` adds `state` and `distance` on verdict kinds, and `head` on
a superseded row. There is no `ts` or `title`: a guessed key reads as a silent
`null`. `list`, `arc list` and `arc todo` return a bare array; `summary` and
`context` return an object, and `context` puts its rows under `notes`.
`summary --json` rows are digests (`target` is a `type:name` string, `body` is
clipped), and it has `store`, which is `null` when no store exists: gate on
it, not on counts or the exit code.

```bash
symbion list --status open --json
symbion show <id> --json                 # list --id <id>: one row, superseded or not
symbion context --target file:src/x.py --json
```

Search with `list --grep PATTERN`, a case-insensitive regex over body, target
name, checked, result and refs; `-F` takes it literally. Never `list | grep`:
text `list` is one 25-row page, so the pipe misses rows and the silence reads
as absence. The page's first line gives the total and the flag that shows
each hidden set; `--limit 0` shows all. `--json` is paged only by `--limit`.

A person may browse the same store at `symbion serve`; rows whose author is
not `claude` came from there, in the same shape.

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
`arc seed` mints one bodiless task per name of a catalog; before seeding,
reconciling, or configuring a catalog or resolver, read `catalogs.md` beside
this file.

## The read loop

A SessionStart hook runs `symbion summary`: open counts, rows due or overdue,
arc progress, rows tagged `priority`, the open rows outside arcs, and open
rows someone else wrote. From there:

- `symbion context --target TYPE:NAME`: every row on one object.
- `symbion context --branch <ref>`: every row on a commit reachable from
  `<ref>` since the default branch.
- `--tag priority` at `add`, or `supersede <id> --add-tag priority` later,
  stars a row for the next session. Star what it must act on, not news. The
  star stays until the row is resolved or `supersede <id> --rm-tag priority`
  takes it off; a row with no status has only the second way.

## Session end, and a handoff file

The store is the open list; a handoff file (`NEXT_SESSION.md`, or whatever a
session-continuity tool writes) is orientation. At the end of a session, or
whenever a wrap runs: `resolve <id> --body …` what the session finished, `add`
what it opened, then `symbion commit`. The handoff keeps its prose (start
here, read first), and where its open-threads section would be, it says the
list lives in the store and how to read it (`symbion summary`, `symbion list
--status open`). Never write the list into the file, and never copy its
bullets into rows a second time: with two copies, the stale one gets read as
the truth. The `/wrap` and `/next` of the continuity plugin (0.8.5 and later,
https://github.com/nullphase-net/enfurbish) honour a file that says so.

A workaround for symbion itself ("`show` does not exist") goes in neither the
handoff nor CLAUDE.md: it outlives the fix. Draft an issue for
https://github.com/phreakocious/symbion/issues and offer it to the user, who
decides whether to file it. When the user's own instructions name another
place for symbion reports, use that.

## Footguns

- **A write is not in git history until `symbion commit`, and not off this
  disk until `symbion push`.** `commit` never pushes; it says how many
  commits are not on the remote, or that the store has none.
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
| `list [--kind K] [--status S] [--tag T] [--arc ID] [--grep PAT [-F]] [--overdue] [--since 2h] [--all] [--limit N] [--full] [--json]` | heads, newest first; `--all` adds superseded rows |
| `show <id> [--json]` | one row, as `list --id <id>` (an array in `--json`) |
| `resolve <id> [--body …] [--result …] [--ref T:N] [--add-tag …]` | close a row; `--body` goes below the current body, after a blank line it inserts |
| `supersede <id> [--body …] [--append] [--add-tag …] [--rm-tag …] [--tag …] [--ref T:N] [--checked …] [--result …] [--due DATE]` | correct a row; `--tag` and `--ref` replace the inherited ones; `--append` adds the body after a blank line it inserts |
| `commit [-m MSG]` | commit the store |
| `push` | push the store's commits to its remote |
| `rename <old> <new> --type T [--to-type T2]` | move every row and ref on a renamed object; `--to-type` changes its type too |
| `summary`, `schema [--toml]`, `tags`, `context` | read |
| `arc create`, `list`, `todo`, `archive`, `seed`, `reconcile` | campaigns (`catalogs.md` for the last two) |
