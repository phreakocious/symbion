# symbion

A per-project notebook and ticket registry for small projects, driven by a
person and a coding agent through one CLI. The CLI stands alone; the agent side
(a skill and a SessionStart hook) is written for Claude Code.

A note is dated, attributed, retractable, and attached to something stable: a
commit, a file, a free-form item, or the whole project. Seven kinds by default:
`check` (a dated verification), `decision` (an ADR), `bug`, `task`, `question`
(needs the owner's answer), `idea` (parked) and `note`. A kind is a label on
three bits (`status`, `parked`, `verdict`); declare your own under `[kinds]` in
`symbion.toml`, and `symbion schema` prints the table. An **arc** is a named
line of work (an epic), shown as a checklist of rows across objects. The store is
a separate git repo of JSONL beside the project, so checking out an old branch
never time-travels your tickets.

Design and rationale: [`docs/superpowers/specs/2026-09-04-symbion-design.md`](https://github.com/phreakocious/symbion/blob/main/docs/superpowers/specs/2026-09-04-symbion-design.md).
The best command reference is the agent's: `src/symbion/data/skill/SKILL.md`
(linked at `~/.claude/skills/symbion`).

## Install

Python 3.11+ and git.

```bash
pipx install 'symbion[gui]'     # or: uv tool install 'symbion[gui]'
# [gui] adds the web UI (symbion serve); drop it for the core alone.
# Unreleased main: pipx install 'symbion[gui] @ git+https://github.com/phreakocious/symbion'
```

The core's three dependencies serve a person at a terminal: `rich` and
`rich-argparse` colour and align the output (a pipe gets plain text), and
`argcomplete` does TAB completion.

To work on symbion itself, install a clone editable:
`pipx install -e /path/to/symbion` (or `uv tool install -e`). Without pipx or uv:

```bash
cd /path/to/symbion
python3 -m venv .venv && .venv/bin/pip install -e .
ln -s "$PWD/.venv/bin/symbion" ~/.local/bin/symbion   # any directory on PATH
```

Then run `command -v symbion` from another directory. The SessionStart hook
finds `symbion` through PATH. Without it, the hook prints only
`symbion: store exists at … but 'symbion' is not on PATH` where a store
exists, and nothing where none does, which reads like a project that never
adopted symbion.

**TAB completion.** Add your shell's line to its rc file:

```bash
eval "$(symbion completion zsh)"     # ~/.zshrc, after compinit
eval "$(symbion completion bash)"    # ~/.bashrc
symbion completion fish | source     # ~/.config/fish/config.fish
```

In zsh you can instead save the output as `_symbion` in a directory on
`$fpath` (oh-my-zsh: `$ZSH_CUSTOM/completions`). It stays out of a shared rc
file and loads on the first TAB, not at every shell start. Regenerate it after
an argcomplete upgrade.

TAB completes verbs, flags and the store's values: kinds, target types, the
`TYPE:NAME` targets its rows use, row ids (only open ones for `resolve`), arc
ids and tags. zsh and fish describe each value. A `--dir` on the line picks
the store.

## Adopt in a fresh repo

From the repo's root:

**1. Create the store and link the agent files.**

```bash
symbion init
```

This creates `../<repo>-notes`, a git repo with no remote, holding a commented
`symbion.toml` whose every value has a default. The first `init` on a machine
also links `~/.claude/skills/symbion` to the skill directory in the installed
package (SKILL.md, adoption.md, catalogs.md and the hook script), so an upgrade
reaches every project. It registers the hook in `~/.claude/settings.json` when
that file does not exist. When it does, `init` says the hook is already there,
or prints the entry to append to `hooks.SessionStart` (it merges nothing), or
says the file is not valid JSON. A `~/.claude/skills/symbion` that is not the
link is left alone and named.

The only file `init` writes into the project is a `.symbion` pointer, when the
store is not the default sibling. It marks the pointer `(git: ignored)` or
`(git: not ignored)`; a not-ignored one goes in with your next `git add -A`.
The hook runs in every project and says nothing where there is no store.

**2. Check the read side.** Start a session, or run the hook by hand:

```bash
CLAUDE_PROJECT_DIR=$PWD bash ~/.claude/skills/symbion/session_start.sh
```

| output | meaning |
|---|---|
| `symbion: open outside arcs: bug 0, task 0, question 0` (`; N open in arcs` when arcs hold any), then a line per arc and the open rows | working |
| `symbion --dir ../other-notes: open outside arcs: …` | working, on a store this repo's tree does not name (`--dir`, `SYMBION_DIR`, or a cwd inside a store); the header names it, so two summaries in one session can be told apart |
| either, then `  N notes not yet in the store's git (symbion commit)` | working; run `symbion commit` at session end |
| either, then `  N per-project symbion copies from an older init: …` | an old `init` left the skill, hook or registration in this project; remove them (the link replaces them) |
| `symbion: store exists at … but 'symbion' is not on PATH` | see Install |
| `symbion: no store at …; run \`symbion init\`` | no store yet, or `.symbion`/`SYMBION_DIR` names a missing path |
| nothing | no store, no `.symbion` and no `SYMBION_DIR`, or the hook is not registered in `~/.claude/settings.json` |

**3. Tell the agent.** One line in `CLAUDE.md`:

```
- We use symbion for durable notes and tickets. Surface friction with it so it can be addressed.
```

If that `CLAUDE.md` is gitignored or its owner reviews every change to it,
propose the line to the owner instead of writing it.

## First session

A repo with history (known issues, TODOs, docs, a handoff file) starts with
`adoption.md`, installed beside `SKILL.md`. It says which catalogs to turn on
and what to bring in; its rows go in as one `symbion add --from-json -`,
validated together before any is written. Either way:

**The first note is a check, written now.** It pays first: dated, re-checkable,
and visibly stale once HEAD moves. A check stamps HEAD and whether the tree is
dirty, untracked files included, so on a dirty tree (the `CLAUDE.md` line from
step 3) it reads `unverifiable (dirty tree)`. Commit, write it again, and it
reads `current`. Files the checked command writes count too, so git-ignore
`__pycache__/` before checking `pytest`. A check on something outside the repo
(DNS, a host) takes `--external` and shows its age instead.

```bash
symbion add check --target commit:HEAD --checked "pytest -q" --result "147 passed"
symbion list --kind check    # unverifiable when dirty, current when clean, behind N once HEAD moves
```

**The first checklist is an arc over free-form items.** `add` each item with a
body. `seed` mints one bodiless task per name and is for fanning out over a
catalog.

```bash
aid=$(symbion arc create --name "Adoption" --scope item --desc "Usable by someone who was not in the room.")
symbion add task --target "item:write the README" --arc-id "$aid" --body "Done: a stranger can install and record a note."
symbion arc todo "$aid" --json      # open items
symbion resolve <id>                # tick one
symbion arc list                    # done/total per arc
symbion list --arc "$aid" --json    # every item, resolved included
```

**Commit the store at session end.** Writes never commit, so a git failure
never blocks a note; `symbion summary` shows the uncommitted count until you do.

```bash
symbion commit -m "adoption: README ticket closed"
symbion push         # when the store has a remote
```

## Browse it (optional)

```bash
symbion serve        # the one command that needs the gui extra
```

A local page at `http://127.0.0.1:43210` (or another free port; `serve` prints
it, `--port` picks one): boards; a note list where every tag, kind, author and
target is a filter link; a search box (`/` focuses it) whose text filters the
page as you type, and Enter searches the whole store (every word, literally and
in any case, or a pasted note id); a
new note from any page (`n`; `#tag` and `!kind` in its body set its tags and
kind); a page per note with its earlier versions; a tag index; and arc
checklists you tick. `?` lists the keys. It writes as
**you**, never as `claude`: `SYMBION_AUTHOR`, else git `user.name`, shown at
the foot of the sidebar, or in the top bar on a window too narrow for one.
`--author NAME` overrides.

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

Names resolve exact, then unique substring, else as typed. `cli.py` matches
both `src/symbion/cli.py` and `tests/test_cli.py`, so it is refused: use the
full path.

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
  `list --kind idea` is the shelf; `list --tag idea` finds rows written before
  `idea` was a kind.
- **Kinds are the project's.** Add or rename labels under `[kinds]` in
  `symbion.toml`; renaming one that has rows also takes a one-line `sed` over
  `notes.jsonl`, which `init` writes into the toml. A kind with both `status`
  and `verdict` is a pre-registration, closed by `resolve --result`.
- **`priority` stars a row** for the next session's summary:
  `supersede <id> --add-tag priority`, until the row is resolved or
  `--rm-tag priority` takes the star off.
- **Due dates.** `--due 2026-10-01` (or an ISO datetime) on any status row. An
  open row past due or due within 7 days leads the session-start summary
  (`overdue 2d`, `due in 3d`). `list --overdue` lists those past due, and
  `supersede <id> --due ''` clears one.
- **Time is UTC.** Rows are stamped in UTC, and symbion prints times and reads
  a bare date in UTC. Set `TZ` to print and read them in that zone instead.
- **Author.** `claude` inside a Claude Code session, else git `user.name`;
  `--author` or `SYMBION_AUTHOR` overrides. A person typing `! symbion add …`
  in a session is recorded as `claude` unless they pass one. Session start
  lists open rows a person wrote, parked ones included, as `from <author>`, so
  the owner's word reaches the agent.
- **Record finished work as a resolved task**:
  `add task --status resolved --body "done: …"` for work done before it had a
  ticket.

## Where things live

- **Store:** `../<repo>-notes`, beside the *main* worktree; linked worktrees
  share it. `SYMBION_DIR` or `--dir PATH` (anywhere in the argv) override. A
  store named from another repo, or a command run inside a store (to edit
  `symbion.toml`, say), resolves in the store's own repo and says so on stderr.
  `init` inside a store refuses.
- **A store not named after its repo:** put its path in a `.symbion` file in
  the repo root (first non-blank line; relative to the repo, or absolute).
  `symbion --dir ../other-notes init` writes it. Commit it; a relative path
  survives a clone. It cannot live in `symbion.toml`, which is inside the store
  it has to find. Set it whenever the names differ: after a repo directory is
  renamed, every command, writes included, reports
  `no store at ../<new-name>-notes` until a `.symbion` names the old store.
- **Files:** `notes.jsonl` (append-only: a correction is a new row that
  supersedes the old one), `arcs.jsonl`, `symbion.toml`.
- **Reading:** `symbion summary` (what the hook prints),
  `symbion context --target TYPE:NAME`, `symbion context --branch REF`,
  `symbion list --json`.
  Every command that prints rows takes `--json`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q
```

The specs in `docs/superpowers/specs/` are dated design records: why each
decision was made. `SKILL.md` (in `src/symbion/data/skill/`, beside
`adoption.md` and the hook) is what an agent reads; keep it short and its
footgun list honest. If this clone is your installed symbion (an editable
install, then `symbion init`), `~/.claude/skills/symbion` links to that
directory, so an edit there reaches every session on the machine at once, as
an edit to `src/` reaches every `symbion` call.
