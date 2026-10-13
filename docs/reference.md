# symbion reference

How symbion behaves. To set it up, start with the [guide](guide.md).

## Notes, kinds and arcs

A note is dated, attributed, retractable, and attached to something stable: a
commit, a file, a free-form item, an arc, or the whole project. Eight kinds by
default: `check` (a dated verification), `prediction` (a pre-registration,
written before the data), `decision` (an ADR), `bug`, `task`,
`question` (needs the owner's answer), `idea` (parked) and `note`. A kind is a label on
three bits (`status`, `parked`, `verdict`); declare your own under `[kinds]` in
`symbion.toml`, and `symbion schema` prints the table. An **arc** is a named
line of work (an epic), shown as a checklist of rows across objects. The store is
a separate git repo of JSONL beside the project, so checking out an old branch
never time-travels your tickets.

## Catalogs, when a homogeneous set exists

A catalog is a shell command that prints one name per line. Declare it in the
store's `symbion.toml`:

```toml
[catalogs]
file = "git ls-files --cached --others --exclude-standard"
test = "git ls-files --cached --others --exclude-standard 'tests/test_*.py'"

[renames]
file = "git"     # so reconcile can follow renamed files
```

Each key becomes a target type (`--target file:src/x.py`). An arc seeded over
it gets one task per member, and `arc reconcile` reports which are live,
renamed or stale as the tree changes.

Scope the command to where notes attach: no pathspec when docs and results
matter, a glob when only code does. A broad catalog costs only a refusal on an
ambiguous substring. Keep `--others --exclude-standard`: without it, a note on
a file made this session warns `taken as typed` on a correct name, and that
warning is the only thing that catches a typo. A gitignored file (a local
`CLAUDE.md`) still misses, and its note adds `it is a gitignored file in this
worktree`.

**Dry-run the first seed of every catalog type**
([catalogs.md](../src/symbion/data/skill/catalogs.md#seed-add-unless-there-is-a-catalog) says why):

```bash
symbion arc seed --scope file --dry-run     # no arc needed; writes nothing
```

Names resolve exact, then unique substring, then the one name equal but for
case, else as typed. `parser.py` matches both `src/parser.py` and
`tests/test_parser.py`, so it is refused: use the full path. A read first takes
a name the store already holds as itself, so a deleted file's rows stay
readable by its old name.

A catalog of **measured** names (a reading, a serial) needs a resolver, and
so does every **store-derived** one (`symbion list … --type X | jq …`).
[catalogs.md](../src/symbion/data/skill/catalogs.md#resolvers-when-substring-is-the-wrong-match) says why, and
gives the protocol.

Within one write (`add --from-json`, `arc seed --name …`), names resolved by
earlier rows are candidates for later ones. A batch naming `src/parser.py` and
then `p`, against a catalog holding `src/pipe.py`, is refused as ambiguous
where two separate adds would store both.

## Conventions

- **Bodies are markdown.** `list --full`, `show` and `context` render them on a
  terminal and print them as written on a pipe; a plain `list` page and
  `summary` flatten each to one clipped line. `list --full` and `show` put a
  line where each amendment joined the body, naming who added it and when
  (`-- ada added below, 2026-10-10 04:22:11Z --`); `--json` gives the body as
  stored.
- **A row's id in a body is a link.** `show` ends a row with a line for each
  row of this store its body cites, by whole id or its 10- or 17-character
  tail, read through that row's head (`cites 412907-3be -> 520114-a1b bug
  [resolved]: …`), then a line for each row whose body cites any version of
  it (`cited by …`). Another store's id, or a tail two rows share, names no
  row.
- **A terminal and a pipe get different text.** A terminal gets columns: the
  id's last 10 characters (which `show` takes), age, a `○`/`✓` status mark,
  kind, target, tags, a ref count, and one body line cut to fit. A pipe, which
  scripts and agents read, gets the plain line.
- **Parked means parked.** An `idea` never prints in `arc todo` or `context`,
  and prints in `summary` only when its `--due` date is near or past, or when
  someone other than the reader wrote it (`from <author>`). Close one with
  `resolve <id> --add-tag adopted --body "why"` or `--add-tag retired`.
  `list --kind idea` is the shelf.
- **Kinds are the project's.** Add or rename labels under `[kinds]` in
  `symbion.toml`; renaming one that has rows also takes a one-line `sed` over
  `notes.jsonl`, which `init` writes into the toml. A kind with both `status`
  and `verdict` is a pre-registration, closed by `resolve --result`: the
  default `prediction` is one, and a store whose table predates it adds
  `prediction = { status = true, verdict = true }`. A kind
  can pick its colour by a palette name, `color = "teal"`, in the terminal
  and in `serve`; a declared kind without one shares lavender, and a name
  outside the palette is refused with the list. symbion 0.3.0 and older
  refuse the key, so every machine that reads the store needs a newer one.
- **`priority` stars a row** for the next session's summary:
  `supersede <id> --add-tag priority`, until the row is resolved or
  `--rm-tag priority` takes the star off. From a day on, the summary prints
  its age.
- **Due dates.** `--due 2026-10-01` (or an ISO datetime) on any status row. An
  open row past due or due within 7 days leads the session-start summary
  (`overdue 2d`, `due in 3d`). `list --overdue` lists those past due, `list
  --due-by DATE` those due by then, past due included, and `supersede <id>
  --due ''` clears one.
- **Time is UTC.** Rows are stamped in UTC, and symbion prints times and reads
  a bare date in UTC. Set `TZ` to print and read them in that zone instead.
- **Author.** `claude` inside a Claude Code session, `codex` inside a Codex
  one, `hermes` inside a Hermes Agent one, else git `user.name`, else `user`; `--author`
  or `SYMBION_AUTHOR` overrides. The default names the harness, not the model:
  no harness tells its shell which model runs, so a review by another model
  passes `--author` to read apart. A person typing `! symbion add …` in a
  session is recorded as the agent unless they pass one. Session start lists
  the open rows someone other than the reader raised or amended, parked ones
  included, so the owner's word reaches the agent. The header counts neither
  a parked row nor one in an arc, so such a row says so in its brackets
  (`[idea, parked]`, `[task, arc release]`), as a due one does. `from <author>` names who
  raised a row, and `last by <author>` who wrote its newest version when that
  is someone else. An answer appended to a row sits below the line the
  summary prints, so the summary and `list` print the first line the newest
  amendment by someone else added below the row (`alice added 3h ago: …`),
  or `alice edited 3h ago: …` and the first line it changed when it wrote no
  paragraph of its own (a rewrite, a sentence fixed in place); the amendment
  count counts only the first. They skip it when the clipped line already
  shows it. `lead rewritten since`
  after the age says the row's lead changed after that line, so it answered
  an earlier question. A resolved row the reader raised or closed prints as
  `closed by <author>`, with that line, when someone else resolved it or
  wrote to its body after the reader's newest row: an answer given by
  `resolve --body` closes the question, a correction can follow a close, and
  every other block lists open rows only. A re-tag is neither. A reply that
  lands mid-session reaches the agent on its next write: `add`, `resolve`
  and `supersede` end by naming, on stderr and in the summary's form, each
  row someone else wrote or amended since the writer's previous write, or
  since the session's start summary if that came later.
- **Record finished work as a resolved task**:
  `add task --target … --status resolved --body "done: …"` for this
  session's work that had no ticket, when a later session would otherwise
  redo or misread it. A bootstrap mints no row for work git history already
  shows closed (`adoption.md`, step 3).

## Where things live

- **Store:** `../<repo>-notes`, beside the *main* worktree; linked worktrees
  share it. `SYMBION_DIR` or `--dir PATH` (anywhere in the argv) override. A
  store named from another repo, or a command run inside a store (to edit
  `symbion.toml`, say), resolves in the store's own repo. A write or a catalog
  run says so on stderr.
  `init` inside a store refuses.
- **A store not named after its repo:** put its path in a `.symbion` file in
  the repo root (first non-blank line; relative to the main checkout,
  absolute, or under `~/`; a file, not a link to the store). A linked worktree's pointer
  counts while the main checkout has none, as on a branch that adopts symbion.
  `symbion --dir ../other-notes init --yes` writes it. If the repo already
  reads a store that exists, init creates the new store and leaves the
  pointer alone; `--repoint` changes it. Commit it; a relative path
  survives a clone. It cannot live in `symbion.toml`, which is inside the store
  it has to find. Set it whenever the names differ: after a repo directory is
  renamed, every command, writes included, reports
  `no store at ../<new-name>-notes` until a `.symbion` names the old store.
  A note under that error names any store beside the repo that no project
  claims, by its name or by a pointer.
- **Files:** `notes.jsonl` (an edit appends the new version under the
  row's id, and the version it replaces stays on its line under a new id
  stamped with that version's own time;
  before `symbion commit`, an edit to a row you wrote in this session
  rewrites it), `arcs.jsonl`,
  `symbion.toml`, and the
  `README.md` and `.gitignore` that `init` writes. `archive/` holds files
  copied in to keep (`adoption.md`); `commit` names any path there git ignores.
- **Two copies of one store:** writes in both between pulls conflict in
  `notes.jsonl`, since each appends at its end. Keep both sides. If both
  copies edited one row, two lines then share its id: `summary` names the
  later one as unreadable, reads skip it, and a write to that id is refused.
  Change the last 3 characters of one line's id to other hex digits, and
  both edits read as rows.
- **Undo:** each write first copies `notes.jsonl`, `arcs.jsonl` and the
  sessions file into the store's git dir (`symbion-undo/`, which git never
  sees). `symbion undo ID` puts them back: the whole of the store's last
  write (a batch too), if ID is a row or arc it touched, the write was yours
  (this agent session's, or with no session your author's), no commit has run
  since, and nothing outside symbion changed the files. One level: an undo
  leaves nothing more to undo. In the GUI, an undo icon sits in the top bar
  while the store's last write is yours and uncommitted; it asks first,
  showing each row the undo removes or puts back.
- **Session end:** `symbion commit --wrap -m MSG` marks the commit that ends a
  session, with a `Wrap: <author>` trailer beside `Rows:`; it refuses without
  `-m`. With nothing pending it commits the message alone (`committed: the
  message alone, no rows or files`), where a plain `commit` exits 1. `summary` prints the newest such message as `last wrap by <author>,
  <age>, N commits since: <first line>` and its paragraph below, before the
  `symbion serve` line; `--json` gives it as `wrap: {gist, body, author, at,
  commits_since}`, `null` when no commit carries the trailer, and
  `commits_since` is `null` outside a repo. The store's git log
  is the journal: `git -C <store> log --grep '^Wrap: '`. The procedure is
  `wrap.md` in the skill.
- **Settings:** besides the tables above, `symbion.toml` takes
  `default_branch`, `local_only = true` (no remote on purpose: `commit` stops
  saying "no remote"), `command_timeout` (the seconds a configured command
  may run, default 30; raise it when a slow catalog is killed), `git_name`
  and `git_email` (the author of the store's own commits, default
  `symbion-notes` and `symbion-notes@local`; set your own where a remote
  refuses a made-up author), and `[provenance] command`, which stamps a
  verdict row with the JSON object it prints instead of HEAD. That command
  must exit 0; an object with a `sha` reads `behind N` as a HEAD stamp does.
- **Trust:** the commands in `[catalogs]`, `[resolvers]`, `[renames]` and
  `[provenance]` run as shell at every write and many reads, and the
  session-start hook runs `summary` in every project with a store (and writes
  the gotcha hook's list, `~/.cache/symbion/gotchas/<session>`). A store
  cloned beside a repo runs its author's commands from then on: read its
  `symbion.toml` as you would code you clone.
- **Secrets:** a store is a git repo, so any pre-commit hook guards it;
  symbion ships none. [gitleaks](https://github.com/gitleaks/gitleaks), for
  one, scans only what a commit adds, from the store's `.git/hooks/pre-commit`:

  ```sh
  exec gitleaks git --pre-commit --staged --redact --no-banner --no-color -v -l warn .
  ```

  A refusal names a line of `notes.jsonl`, and `symbion commit` commits
  nothing. `symbion supersede <id> --body …` rewrites your own uncommitted row
  in place, so the secret never reaches history. A row someone else wrote, or
  one another row cites, keeps its earlier version on its line: edit that line
  out of `notes.jsonl` by hand.

  A committed secret stays in the store's history. Rotate it first: a
  rewrite does not reach a copy someone already has. Then
  [git filter-repo](https://github.com/newren/git-filter-repo)
  `--replace-text` replaces it with `***REMOVED***` in every commit. The row
  keeps its id, so each cite of it still holds. If the store has a remote,
  force-push it and re-clone each other copy. Last, add a `decision` row that
  cites the row and says who removed what, when and why, without the secret.
  `show` on the row lists that decision, so a reader who finds `***REMOVED***`
  finds the reason too.
- **Reading:** `symbion summary` (what the hook prints),
  `symbion context --target TYPE:NAME`, `symbion context --branch REF`,
  `symbion list --json`.
  Every command that prints rows takes `--json`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q -n auto
```

On Windows, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test,gui]'
.\.venv\Scripts\python.exe -m pytest -q
```

To make the clone the `symbion` on PATH, link the venv's script into any
directory on PATH, `ln -s "$PWD/.venv/bin/symbion" ~/.local/bin/symbion`, or
install it with `pipx install -e /path/to/symbion` (or `uv tool install -e`).

The specs in `docs/superpowers/specs/` are dated design records: why each
decision was made. `SKILL.md` (in `src/symbion/data/skill/`, beside
`adoption.md` and the hook) is what an agent reads; keep it short and its
footgun list honest. If this clone is your installed symbion (an editable
install, then `symbion init --yes`), `~/.claude/skills/symbion` and, with
`--agent codex` or `--agent hermes`, `~/.agents/skills/symbion` link to that
directory, so an edit there reaches every session on the machine at once, as
an edit to `src/` reaches every `symbion` call.
