# symbion reference

How symbion behaves. To set it up, start with the [guide](guide.md).

## Notes, kinds and arcs

A note is dated, attributed, retractable, and attached to something stable: a
commit, a file, a free-form item, or the whole project. Seven kinds by default:
`check` (a dated verification), `decision` (an ADR), `bug`, `task`, `question`
(needs the owner's answer), `idea` (parked) and `note`. A kind is a label on
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
file = "git ls-files --cached --others --exclude-standard '*.py'"
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
warning is the only thing that catches a typo.

**Dry-run the first seed of every catalog type.** What a command prints depends
on the tool's version and the project's config:

```bash
symbion arc seed --scope file --dry-run     # no arc needed; writes nothing
```

Names resolve exact, then unique substring, then the one name equal but for
case, else as typed. `parser.py` matches both `src/parser.py` and
`tests/test_parser.py`, so it is refused: use the full path. A read first takes
a name the store already holds as itself, so a deleted file's rows stay
readable by its old name.

A catalog of **measured** names (a reading, a serial) needs a resolver, or a
near-miss mints a second target: substring matching cannot see that `3.1416`
is `3.14159`. So does every **store-derived** catalog
(`symbion list … --type X | jq …`): it is empty until its first `X` row
exists, so without a resolver that first `add` is refused.

```toml
[resolvers]
reading = "python3 tools/resolve_reading.py"   # stdin: query, then names; exit 2 = ambiguous
```

The starter `symbion.toml` documents the protocol. A failing resolver stores
nothing; it never falls back to substring matching.

Within one write (`add --from-json`, `arc seed --name …`), names resolved by
earlier rows are candidates for later ones. A batch naming `src/parser.py` and
then `p`, against a catalog holding `src/pipe.py`, is refused as ambiguous
where two separate adds would store both.

## Conventions

- **Bodies are markdown.** `list --full`, `show` and `context` render them on a
  terminal and print them as written on a pipe; a plain `list` page and
  `summary` flatten each to one clipped line.
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
  and `verdict` is a pre-registration, closed by `resolve --result`. A kind
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
  (`overdue 2d`, `due in 3d`). `list --overdue` lists those past due, and
  `supersede <id> --due ''` clears one.
- **Time is UTC.** Rows are stamped in UTC, and symbion prints times and reads
  a bare date in UTC. Set `TZ` to print and read them in that zone instead.
- **Author.** `claude` inside a Claude Code session, `codex` inside a Codex
  one, `hermes` inside a Hermes Agent one, else git `user.name`; `--author`
  or `SYMBION_AUTHOR` overrides. A person typing `! symbion add …` in a
  session is recorded as the agent unless they pass one. Session start lists
  the open rows someone other than the reader raised or amended, parked ones
  included, so the owner's word reaches the agent: `from <author>` names who
  raised a row, and `last by <author>` who wrote its newest version when that
  is someone else. An answer appended to a row sits below the line the
  summary prints, so the summary and `list` print the first line the newest
  amendment by someone else added below the row (`alice added 3h ago: …`).
  They skip it when the clipped line already shows it. `lead rewritten since`
  after the age says the row's lead changed after that line, so it answered
  an earlier question.
- **Record finished work as a resolved task**:
  `add task --target … --status resolved --body "done: …"` for work done
  before it had a ticket.

## Where things live

- **Store:** `../<repo>-notes`, beside the *main* worktree; linked worktrees
  share it. `SYMBION_DIR` or `--dir PATH` (anywhere in the argv) override. A
  store named from another repo, or a command run inside a store (to edit
  `symbion.toml`, say), resolves in the store's own repo. A write or a catalog
  run says so on stderr.
  `init` inside a store refuses.
- **A store not named after its repo:** put its path in a `.symbion` file in
  the repo root (first non-blank line; relative to the main checkout, or
  absolute; a file, not a link to the store). A linked worktree's pointer
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
- **Files:** `notes.jsonl` (append-only once committed: a correction is a
  new row that supersedes the old one; before `symbion commit`, an edit to
  your own row rewrites it), `arcs.jsonl`, `symbion.toml`.
- **Secrets:** a store is a git repo, so any pre-commit hook guards it;
  symbion ships none. [gitleaks](https://github.com/gitleaks/gitleaks), for
  one, scans only what a commit adds, from the store's `.git/hooks/pre-commit`:

  ```sh
  exec gitleaks git --pre-commit --staged --redact --no-banner --no-color -v -l warn .
  ```

  A refusal names a line of `notes.jsonl`, and `symbion commit` commits
  nothing. `symbion supersede <id> --body …` rewrites your own uncommitted row
  in place, so the secret never reaches history. A row someone else wrote, or
  one another row cites, gets a new row instead: edit that line out of
  `notes.jsonl` by hand.
- **Reading:** `symbion summary` (what the hook prints),
  `symbion context --target TYPE:NAME`, `symbion context --branch REF`,
  `symbion list --json`.
  Every command that prints rows takes `--json`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q
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
