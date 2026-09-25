---
name: symbion
description: "Use when recording or retrieving a durable, dated, object-attached conclusion about this project — a test/build check (checked/result), an ADR-style decision, a known-broken bug, or a ticket task — instead of letting it evaporate in chat. Also for running a project-wide campaign as a per-object checklist (arcs), or on explicit /symbion. Keywords: notebook, ticket registry, audit trail, queryable notes, bootstrap an existing project."
---

# symbion — this project's notebook and ticket registry

Durable, dated, object-attached notes (a commit, a file, an arc, or the
project as a whole), kept queryable instead of evaporating in chat. Backed by
a separate git repo of JSONL beside the project, auto-created on first write.

## Invocation

Run `symbion`, the installed console script, never through `python -m` or a
venv path. It works from any cwd, and `--dir` may go anywhere in the argv.
If `command -v symbion` fails, it was installed into a venv only and the
SessionStart hook is silent: see Install at
https://github.com/phreakocious/symbion. This file is symbion's own package
data, linked once per user at `~/.claude/skills/symbion` by `symbion init`,
so it is always the installed symbion's; no project holds a copy.

## Picking a kind

This store's kinds, with their bits and a row count per label (`symbion
schema`; the SessionStart hook does not print it):

!`symbion schema`

A kind is a label on three bits, and the bits are all symbion knows:

- **`status`** — the row carries open/resolved, defaults to open, and
  `resolve <id>` closes it. Open rows count in arc progress, `arc todo`,
  the session-start line and the default `context` view.
- **`parked`** — open rows are hidden from every open view. `resolve <id>
  --add-tag adopted` or `--add-tag retired`, with a `--body` saying why.
- **`verdict`** — `--checked "pytest -q"` (what ran) and `--result "412
  passed"` (what it said); provenance is stamped at write, so `list` reports
  `current`, `behind N` or `diverged` against today's HEAD. The body is
  optional.
- **`status` + `verdict`** is a pre-registration: file it before the data
  exists, with `--checked` naming what will run and its control and the body
  stating the prediction and what would falsify it. `resolve <id> --result
  "…"` closes it; without `--result` it is refused, on every path. Amend
  it with `supersede <id> --append --body-file -`: the registered text stays
  a byte-identical prefix, and the new text follows a blank line. Give it
  `--due` the date its window closes. Open ones list first at session
  start, ahead of the newer rows. No default kind has both bits: declare
  one, e.g. `prediction = { status = true, verdict = true }`, in a `[kinds]`
  table. The table replaces the defaults entirely, so copy in the ones you
  keep.
  A finished result is not a pre-registration: it goes on a verdict-only
  kind, and a pending half of it ("not yet verified until …") is its own
  status row. Measured 2026-09-24: stores that gave a result kind both
  bits minted their finished results open, which buried the one real open
  prediction.

A kind with no bits is plain: a dated body on a target. Every kind is a
note; the plain ones are the ones with no extra structure. A plain head has
no status to resolve, so when it stops being context (a progress log, a
"built at" marker) `supersede <id> --add-tag retired` takes it out of the
default `context` view; it stays on its object and in `list`. The table lives
in `symbion.toml` under `[kinds]`; absent, it is the seven defaults.

**`--due DATE`** goes on any status row: `YYYY-MM-DD` (due through the end
of that day) or an ISO datetime (`2026-10-01T22:30Z`). An open row past due,
or due within 7 days, opens the session-start summary (`overdue 2d`, `due in
3d`), in an arc or not, parked or not, so it is also how an `idea` gets a
revisit date. `list --overdue` is every open row past due; `supersede <id>
--due ''` clears it. A date written only in the body reaches neither.

**Guard against write-only use:** record what a future session needs in order
to *not redo* or *not misread* the work. A note that merely restates a
committed result ("tests pass") is noise — a `check` with `--checked`/
`--result` earns its place because it's dated and re-checkable; a bare
restatement doesn't.

**A revision updates its head.** `add` names the rows already open on its
target (`note: 2 open on item:x: …`, on stderr). If the new row settles or
revises one, `resolve` or `supersede` that one: an open head beside the row
that answered it prints at every session start. Before asserting what the
store does or does not hold, search it: `list --grep 'a|b'`.

`--body` is markdown. Use code fences and links; `list` and `context` print
it as written on a pipe (and render it on a terminal with the `[tty]` extra),
`summary` flattens it to one line. A body with backticks goes through
`--body-file -` and a QUOTED heredoc (`<<'EOF'`): in `--body "…"` or an
unquoted `<<EOF` the shell runs each backtick span as a command, silently.

The examples here use `--type file`, a catalog type: a store has it only
when its `symbion.toml` declares it (the starter file has it commented out),
and `symbion schema` above lists this store's types.

```bash
symbion add --kind check --type commit --name HEAD \
  --checked "pytest -q" --result "412 passed"
symbion add --kind decision --type file --name src/x.py --body "kept the O(n) scan; n is bounded by config, not input"
symbion add --kind bug --type item --name "flaky upload test" --body "fails ~1/20 on CI, not locally" --tag ci
```

`--type` is `commit`, `item`, `project` (no `--name`), `arc`, or a
project-configured catalog type (e.g. `file`). `--tag` is repeatable.
`--ref TYPE:NAME` (repeatable) cross-references a second object: `context
--target` on that object surfaces the note. Use it instead of naming the
second object in prose. `supersede <id> --ref TYPE:NAME` takes the same flag,
so a note already written can be attached to an object afterwards; it replaces
the inherited refs the way `--tag` replaces the inherited tags.

**Many rows: `add --from-json PATH`** (`-` for stdin), one JSON object per
line in the `list --json` shape: `kind`, `target: {type, name}`, and any of
`body`, `status`, `checked`, `result`, `arc_id`, `due`, `tags`, `refs` (the
JSON form of `--ref TYPE:NAME`: `[{"type": …, "name": …}]`), `author`. Ids,
dates and provenance are minted, and a row carrying one is refused by line
number, as is any bad row; nothing is written unless every row passes. It
prints one id per line, in input order.

```bash
symbion add --from-json - <<'EOF'
{"kind": "decision", "target": {"type": "file", "name": "src/x.py"}, "body": "kept the O(n) scan"}
{"kind": "task", "target": {"type": "item", "name": "flaky upload test"}, "tags": ["ci"], "refs": [{"type": "commit", "name": "dd1e48b"}]}
EOF
```

**`add`, `resolve`, `supersede`, and `arc create` print the new id on stdout** —
capture it (e.g. `nid=$(symbion add …)`) to `resolve` or `supersede` that
same note later. A note id is a timestamp; an arc id is the slug of its
name, suffixed on collision.

## Reading

`--json` is the default for agent use — every row-producing command
(`list`, `summary`, `context`, `arc list`, `arc todo`) takes it.
A note is one shape everywhere: `id`, `kind`, `target: {type, name}` (`name`
is `null` on a `project` target, so guard it before a jq `test()`),
`created_at`, `author`, `body`, `status`, `checked`, `result`, `arc_id`,
`due`, `supersedes`, `tags`, `refs`, plus `state` and `distance` on verdict
kinds. There is no `ts` or `title`: a guessed key reads as a silent `null`.

The ENVELOPE differs, though: `list`, `arc list` and `arc todo` return a bare
array, while `summary` and `context` return an object (`context` puts its
rows under `notes`). Index the array; reach for `.notes` only on `context`.
`arc reconcile --json` is a report, not notes: a bare array of `id`,
`target_type`, `target_name`, `status` (`live`, `renamed`, `stale`,
`uncheckable`), `suggestion` and `needs_result`, as found before any
`--apply`, whose counts go to stderr. `summary --json` also carries `store`:
the resolved path, or `null` when no store exists. Gate on that key, never on
the counts or the exit code: an absent store and an empty one both print
zeros at exit 0.

A human may be browsing the same store at `symbion serve`. Notes authored by a
person rather than `claude` came from there; they are not a different shape.

```bash
symbion list --status open --json
symbion show <id>                        # one row by id; same as `list --id <id>`
symbion summary --json
symbion context --target file:src/x.py --json
```

`show` implies `--all`, so a superseded id still resolves and names its
chain's head: `superseded -> <id> [resolved]` (`head` in `--json`).

Search bodies with `list --grep PATTERN` (a case-insensitive regex over
body, target name, checked and result), never `list | grep`: the pipe reads
one page, drops its header, and the silence reads as absence. Measured
2026-09-24: two rows in one store claimed "no row about this" beside the row
about exactly that.

Text `list` is a page, not the whole: the 25 newest rows, one clipped body
line each, under a first line that carries the total, what is shown, and each
hidden set with the flag that reveals it (`+N resolved (--status resolved)`,
`+N superseded (--all)`). With a filter the first line reads `N of M match
--kind bug --tag x`, M being the rows scanned, so a 0 never reads as 0-of-0;
`--all` always says how many superseded rows it included. `--limit N` widens
(0 for all); `--full` prints bodies as stored; `--id` is always the whole row. `--json` is never a page
unless `--limit` is given, and then says `showing N of M` on stderr.
Text `summary` is capped the same way, and every `+N more ... (--full)` line
names the one flag that lifts all of its caps.
`arc todo` and `context` open with the same kind of line (`0 open of 4 items;
+4 resolved (list --arc ID)`, `0 notes for file:x`), an empty store names the
verb that fills it, and a miss names the legal values (`no arc 'x'; did you
mean y? (arc list)`).

## Checklists: `add`, not `seed`, unless there is a catalog

A heterogeneous checklist needs no catalog and no config:

```bash
aid=$(symbion arc create --name "Adoption" --scope item --desc "…")
symbion add --kind task --type item --name "write the README" --arc-id "$aid" --body "why; what done looks like"
symbion arc todo "$aid" --json     # open items, same note rows as `list --json`
symbion resolve <id>                      # tick the box
symbion list --arc "$aid" --json    # every item, resolved included: how a finished campaign reads back
```

A checklist item is any row in the arc that carries a status, so a `bug`
added with `--arc-id` is a box too, not just context beside one; several
items may share a target, and each is its own box. `arc todo` and
`list --arc` are the same set, the first filtered to what is still open.

`seed` mints one *bodiless* task per name; it exists for fanning out
over a catalog, where the name is the whole ticket.

## First use of any catalog type: dry-run the seed

Before running `arc seed` against a catalog type for the first time,
run it with `--dry-run` and read the output. No arc is needed: the id is
optional under `--dry-run`, so probing a catalog writes nothing.

```bash
symbion arc seed --scope file --dry-run
```

A catalog runs in the root of the current worktree.

A catalog is just a configured shell command, and its output depends on the
tool's version and on the project's own config: you cannot know what it
emits from documentation, or from what it did in another project, or from
what it did here last month. You have to run it, in this project, at this
version, today. Measured: `pytest --collect-only -q` prints one test node id
per line, but in a project whose pytest config puts `-q` in `addopts` the
flags stack to `-qq`, and the same command prints per-file counts
(`tests/test_store.py: 39`) instead. Seeding on the wrong one of those mints
dozens of tasks against garbage targets. `--dry-run` prints the resolved names and count and creates
nothing; check the names before dropping the flag.

## Resolvers: when substring is the wrong match, and for every store-derived catalog

The built-in rule is exact, else unique substring, else your typed string
(a miss says so on stderr and stores as typed). A **numeric** catalog
fragments under it: `3.1416` is not a substring of `3.14159`, so it
silently becomes a second target and the investigation gets two heads. And a
**store-derived** catalog (`symbion list … --type X | jq …`) is empty until
its first `X` row exists, while that row cannot be written against an empty
catalog: without a resolver the first `add` is refused and names this. Declare
a resolver for the type, beside its catalog:

```toml
[catalogs]
reading = "set -o pipefail; symbion list --all --json --type reading | jq -r '.[].target.name' | sort -u"
[resolvers]
reading = "python3 tools/resolve_reading.py"
```

The resolver reads the query on stdin line 1 and one catalog name per line
after it. Exit 0 and print the name to store (in or out of the list — that is
how a first sighting mints its canonical form; the first non-blank line is
taken). Exit 2 and print the matches to refuse as ambiguous. Anything else is
an error and stores nothing — a resolver never falls through to the
substring rule. It runs while the store lock is held, so it must not write
to the store. `set -o pipefail` on a piped catalog is load-bearing: without
it a failed producer reads as an empty catalog. `symbion schema` shows which
types have one.

Within one write (a batch `add --from-json`, an `arc seed --name …`), names
already resolved by earlier rows are candidates for later rows, resolver or
not — so a batch that names `src/parser.py` and then `p` against a catalog
holding `src/pipe.py` is refused as ambiguous where two separate adds would
store both.

## Adopting an existing project

The store starts empty and the project does not. The procedure (which
sources, what to strike, the kind mapping, the owner's review, retiring
what migrated) is `adoption.md` beside this file: read it in full before
a bootstrap writes its first row.

## Footguns that remain

- **A write is not in git history until `symbion commit`, and not off this
  disk until a push.** `add` never commits, so a git failure can't block a
  write — but an uncommitted note is invisible to anyone reading the store
  elsewhere. `commit` never pushes; when the store has an `origin` it prints
  the unpushed count and the `git -C <store> push` to run, and with no remote
  it says the store exists on one disk.
- **Name resolution falls back to your typed string on a catalog miss.** A
  substring match against a configured catalog expands to the full name; a
  typo that matches nothing becomes its own target, silently — no error. A
  numeric type needs a `[resolvers]` entry or it fragments (see Resolvers).
- **`--apply` on `arc reconcile` will not close stale tasks.** It
  moves each `renamed` item's old name onto the live one across the whole
  store: every row on it and every `--ref` to it, in the arc or not, as
  `rename` does. A task with no
  live target and no rename evidence needs `--resolve-stale` explicitly —
  deliberate, so an unconfigured `[renames]` can't silently close real open
  work.

## The read loop

`session_start.sh`, beside this file, runs `symbion summary` — open counts
per kind outside arcs, every open row past due or due within 7 days, arc
progress, anything tagged `priority`, and the open rows outside arcs, one line
each (open pre-registrations first, then the newest of the rest; each row
prints once) — from a SessionStart hook in `~/.claude/settings.json`, so it
runs at the start of every session (`symbion init` writes that file when it
is absent, and otherwise prints the block to add); in a
project with no store it says nothing. From there:

- `symbion context --branch <ref>` — every note attached to a commit reachable
  from `<ref>` since the default branch.
- `symbion context --target TYPE:NAME` — every note on one object.
- `symbion supersede <id> --add-tag priority` stars a note, whoever writes
  it, so the next session's summary lists it under `priority`. Star what the
  next session must act on, not a result that is only news. The star stays
  until the row is resolved or `supersede <id> --rm-tag priority` takes it
  off, and a row with no status (a note, a finished measurement) has only the
  second way. Measured 2026-09-24: 6 of one store's 8 live stars were finished
  measurements, starred as news and never taken off.

## Session end, and a handoff file

The store is the open list. A handoff file (`NEXT_SESSION.md`, or whatever a
session-continuity tool writes) is orientation. At the end of a session, or
whenever a wrap runs: `resolve <id> --body …` what the session finished,
`add` what it opened, then `symbion commit`. The handoff file keeps its prose
— start here, read first, don't forget — and where its open-threads section
would be it says that the list lives here and how to read it (`symbion
summary`, `symbion list --status open`). Never write the list into that file,
and never copy its bullets into rows a second time: two copies of one list is
how the stale one ends up read as authoritative. Measured 2026-09-20: 33
items in both a handoff file and one store, nothing pointing either way, and
each wrap re-copied items the store had already resolved. The `/wrap` and
`/next` of the continuity plugin (0.8.5 and later) honour a file that says
so, without naming any tool, so that one sentence in the file is the whole seam.

A workaround for symbion itself ("`show` does not exist", "never run it from
inside the store") does not go into the handoff or CLAUDE.md either: it
outlives the fix. File it in this project's store as a row tagged `symbion`,
and tell the user, who can take it to
https://github.com/phreakocious/symbion/issues. Measured 2026-09-24: four
repos still told their agents `show` did not exist after it shipped.

## Quick reference

| command | does |
|---|---|
| `add KIND --target T:N` or `add --kind K --type T [--name N]`, then `[--body …] [--status open\|resolved] [--checked …] [--result …] [--arc-id ID] [--due DATE] [--tag …]… [--ref T:N]… [--author …]` | append a note; prints its id |
| `add --from-json PATH [--author …]` | append one note per JSON line (`-` = stdin); all rows validated first; prints each id |
| `list [--type T] [--name N] [--kind K] [--status S] [--tag TAG] [--arc ID] [--author A] [--grep PAT] [--overdue] [--all] [--limit N] [--full] [--json]` | list heads, newest-first; text is a 25-row page with a count header; `--overdue` is open rows past their due date |
| `show <id> [--json]` | one row by id: `list --id <id>` |
| `resolve <id> [--body …] [--body-file PATH] [--append] [--add-tag …]… [--result …]` | supersede with `status=resolved`; `--result` is required on a status+verdict kind; `--append` adds the body after the current one instead of replacing it |
| `supersede <id> [--body …] [--body-file PATH] [--append] [--status …] [--tag …] [--add-tag …] [--rm-tag …] [--ref T:N] [--checked …] [--result …] [--due DATE]…` | append a correction, prints the new id (`--tag` and `--ref` replace what was inherited; `--due ''` clears the date; `--add-tag`/`--rm-tag` edit the tags; `--checked`/`--result` correct a check's verdict; `--append` adds the body after the current one, which stays byte-identical) |
| `commit [-m MSG]` | `git add -A` + commit the store |
| `rename <old> <new> --type T` | re-target all notes for a renamed object and re-point every `--ref` to it; exits 1 when nothing carried the name |
| `tags` | tag vocabulary with counts |
| `summary [--full] [--json]` | bounded session-start summary |
| `schema [--json]` | this store's kinds with their bits and row counts, then its target types |
| `context [--target T:N \| --commit SHA \| --branch REF [--since REF]] [--json]` | pull-based detail |
| `arc create --name … --scope SCOPE` / `seed <id> --scope T [--name …] [--kind LABEL]` / `seed --scope T --dry-run` / `list` / `todo <id>` / `rename <id> <new_name>` / `archive <id>` / `reconcile <id> [--apply] [--resolve-stale] [--json]` | campaign registry |

`--dir PATH` overrides the store, anywhere in argv; default is `SYMBION_DIR`,
else a `.symbion` file in the repo root naming the store (one line, relative
or absolute), else the sibling repo `../<project>-notes`. A store whose name
does not match its repo NEEDS that file: without it a bare command finds no
store and the next write starts a second one, silently. A `<project>-notes`
store named with `--dir` from another repo, or from outside any repo,
resolves catalogs and check state in `<project>` and says so on stderr;
`init` alone keeps the cwd's project. A cwd inside a store uses that store
and resolves the same way; `init` there refuses. Any read of an absent store
prints `no store at <path>` on stderr and exits 1 (`summary`: stdout, 0).
