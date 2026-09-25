# symbion

A per-project notebook and ticket registry for small projects, driven by a
human and a coding agent through one CLI.

A note is dated, attributed, retractable, and attached to something stable: a
commit, a file, a free-form item, or the project as a whole. Seven kinds by
default — `check` (a dated verification), `decision` (an ADR), `bug` (known
broken), `task` (a next action), `question` (needs the owner's answer), `idea`
(parked), `note` (everything else) — and a project may declare its own labels
in `symbion.toml` under `[kinds]`; every kind is a label on three bits
(`status`, `parked`, `verdict`), and `symbion schema` prints the table. An
**arc** is a named line of work — an epic, if you like — rendered as a
per-object checklist with its decisions and notes filed under it. The store is
a private sibling git repo of JSONL, so checking out an old branch never
time-travels your tickets.

Design and rationale: `docs/superpowers/specs/2026-09-04-symbion-design.md`.
The agent-facing surface, which is also the best command reference:
`src/symbion/data/skill/SKILL.md` (linked at `~/.claude/skills/symbion`).

## Install

Python 3.11+ and git. The core has no dependencies. Two optional extras:
`pip install 'symbion[gui]'` for the web UI (`symbion serve`), and
`'symbion[tty]'` to render note bodies as markdown on a terminal.

```bash
pipx install -e /path/to/symbion        # or: uv tool install -e /path/to/symbion
```

Without pipx or uv, install into a venv and put the console script on PATH:

```bash
cd /path/to/symbion
python3 -m venv .venv && .venv/bin/pip install -e .
ln -s "$PWD/.venv/bin/symbion" ~/.local/bin/symbion
```

Verify from any other directory:

```bash
command -v symbion
```

This matters more than it looks. The SessionStart hook below finds `symbion`
through PATH. A venv-only install leaves the hook silent, and a silent hook is
indistinguishable from a project that never adopted symbion.

## Adopt in a fresh repo

Run from the repo's root.

**1. Create the store and link the agent files.**

```bash
symbion init
```

This creates `../<repo>-notes` (a git repo, no remote) with an annotated
`symbion.toml`; nothing requires the toml, every value has a default. The
first `init` on a machine also links `~/.claude/skills/symbion` to the skill
directory inside the installed package (SKILL.md, adoption.md and the
SessionStart hook script), so an upgrade reaches every project with nothing
to refresh, and registers the hook in `~/.claude/settings.json` when that file
does not exist. If it exists, `init` prints the block to add instead of
merging. A `~/.claude/skills/symbion` that is not that link is left alone and
named. Nothing is written into the project except a `.symbion` pointer when
the store is not the default sibling. The hook runs in every project and says
nothing in one without a store.

**2. Check the read side works.** Start a session, or run the hook by hand:

```bash
CLAUDE_PROJECT_DIR=$PWD bash ~/.claude/skills/symbion/session_start.sh
```

`init` marks a `.symbion` pointer it writes `(git: ignored)` or `(git: not
ignored)`; one marked not ignored goes in with the next `git add -A`.

| output | meaning |
|---|---|
| `symbion: open outside arcs: bug 0, task 0, question 0`, then arc progress and the open heads | working |
| the same, then `  N notes uncommitted` | working; `symbion commit` when the session ends |
| the same, then `  N per-project symbion copies from an older init: …` | an `init` from before the user-level link left the skill, the hook or its registration in this project; remove them (the link replaces them) |
| `symbion: store exists at … but 'symbion' is not on PATH` | fix Install |
| `symbion: no store at …; run \`symbion init\`` | no store yet, or `.symbion`/`SYMBION_DIR` names a path that does not exist |
| nothing | no store, no `.symbion` pointer and no `SYMBION_DIR` (a project that never adopted symbion), or the hook is not registered in `~/.claude/settings.json` |

**3. Tell the agent.** One line in `CLAUDE.md`, for example:

```
- We use symbion for durable notes and tickets. Surface friction with it so it can be addressed.
```
If that repo's `CLAUDE.md` is gitignored or gated by `/affirm`, propose the
line to its owner instead of writing it.

## First session

**The first note is a check, and it goes in now.** It is the kind that earns
its place fastest: dated, re-checkable, and it goes stale visibly. A check
stamps HEAD and whether the tree was dirty, and untracked files count, so on
the tree step 1 left behind it reads `state=unverifiable (dirty tree)`. That
is honest, not blocked: write it, commit the adoption files, write it again
and it reads `current`. A bootstrap that ends with no check row skipped the
step that pays first.

```bash
symbion add --kind check --type commit --name HEAD --checked "pytest -q" --result "147 passed"
symbion list --kind check          # unverifiable (dirty tree) now; current after the commit; behind N once the tree moves
```

**The first checklist is an arc over free-form items.** Use `add` per
item, not `seed`. `seed` mints one bodiless task per name and exists for
fanning out over a catalog; a hand-picked list wants a body on each ticket.

```bash
aid=$(symbion arc create --name "Adoption" --scope item --desc "Make this usable by someone who was not in the room.")
symbion add --kind task --type item --name "write the README" --arc-id "$aid" --body "What done looks like: a stranger can install and record a note."
symbion arc todo "$aid" --json      # open items, same rows as `list --json`
symbion resolve <id>                     # tick the box
symbion arc list                    # done/total per arc
symbion list --arc "$aid" --json    # every item, resolved included
```

**Commit the store at the end of a session.** Writes never auto-commit, so a
git failure can never block a note. `symbion summary` shows the uncommitted
count until you do.

```bash
symbion commit -m "adoption: README ticket closed"
```

**Adopting a repo with history?** The store starts empty and the project does
not. `adoption.md`, installed beside `SKILL.md`, is the procedure, written
from the adoptions it cites. The rows it produces go in as one `symbion add
--from-json -`, one JSON object per line, validated together before any is
written.

## Browse it (optional)

```bash
pipx install -e '/path/to/symbion[gui]'
symbion serve
```

A local page at `http://localhost:43210`: boards, a filtered note list where
every tag/kind/author/target chip is a link, a tag index, and arc
checklists you tick. It writes as **you** — `SYMBION_AUTHOR`, else
`git user.name` — never as `claude`, and shows the resolved name in the top
bar. `--author NAME` overrides.

The core still has no dependencies; `serve` is the only thing that needs the
extra.

## Catalogs, when a homogeneous set exists

A catalog is a shell command that prints one name per line and nothing else.
Declare it in `../<repo>-notes/symbion.toml`:

```toml
[catalogs]
file = "git ls-files --cached --others --exclude-standard '*.py'"
test = "git ls-files --cached --others --exclude-standard 'tests/test_*.py'"

[renames]
file = "git"     # so reconcile can follow renamed files
```

Each key becomes a target type: `--type file --name src/x.py`. An arc
seeded over it gets one task per member, and `arc reconcile` reports
which are live, renamed, or stale as the tree changes.

Scope the command to where notes will attach: no pathspec when docs and
results matter, a glob when only code does. A broad catalog costs nothing
except a refusal on an ambiguous substring. `--others --exclude-standard`
adds files not yet committed: without it, a note on a file made this session
warns `stored as typed` on a correct name, and that warning is the only thing
that catches a typo.

**Always dry-run the first seed of any catalog type.** A command's output
format can change between patch releases of the same tool; the only way to
know what it prints is to run it here, today.

```bash
symbion arc seed --scope file --dry-run     # no arc needed; writes nothing
```

Names resolve exact, then unique substring, else verbatim. `cli.py` is
ambiguous between `src/symbion/cli.py` and `tests/test_cli.py` and is refused;
use the full path.

A catalog type whose names are **measured** (a reading, a serial) needs a
resolver too, or a near-miss mints a second target: substring matching cannot
see that `3.1416` is `3.14159`. So does every **store-derived** catalog
(`symbion list … --type X | jq …`): it is empty until its first `X` row
exists, and that row cannot be written against an empty catalog, so the first
`add` is refused until a resolver decides what a first sighting stores.
Declare one beside the catalog:

```toml
[resolvers]
reading = "python3 tools/resolve_reading.py"   # stdin: query, then names; exit 2 = ambiguous
```

The starter `symbion.toml` carries the full protocol. A failing resolver
stores nothing; it never falls back to the substring rule.

Within one write (a batch `add --from-json`, an `arc seed --name …`), names
already resolved by earlier rows are candidates for later rows, resolver or
not — so a batch that names `src/parser.py` and then `p` against a catalog
holding `src/pipe.py` is refused as ambiguous where two separate adds would
store both.

## Conventions

- **Bodies are markdown.** `list` and `context` print them as written on a
  pipe, and render them on a terminal when the `[tty]` extra is installed;
  `summary` flattens them to one line.
- **`idea` is a kind, and parked means parked.** `add --kind idea --type project
  --body …` shelves a thought; it never prints in `summary`, `arc todo` or
  `context`, unless it carries a `--due` date that is near or past: that is how
  a thought comes back on a date. Close it with `resolve <id> --add-tag adopted --body "…"` or
  `--add-tag retired`. `list --kind idea` is the shelf; `list --tag idea` still
  finds rows written before it was a kind.
- **Kinds are the project's.** Rename or add a label in `symbion.toml`
  `[kinds]`; a label that already has rows also needs a one-line `sed` over
  `notes.jsonl` (`init` writes the recipe into the toml). `status` +
  `verdict` is a pre-registration: `resolve --result` closes it.
- **`priority` tag.** `symbion supersede <id> --add-tag priority` stars a note,
  whoever writes it, so the next session's summary lists it under `priority`
  until the row is resolved or `--rm-tag priority` takes the star off.
- **Due dates.** `--due 2026-10-01` (or an ISO datetime) on any status row.
  Past due or due within 7 days, an open row leads the session-start summary
  as `overdue 2d` or `due in 3d`, in an arc or not; `list --overdue` lists
  them all, and `supersede <id> --due ''` clears one.
- **Author.** Inside a Claude Code session the author is `claude`; otherwise
  git `user.name`. `--author` or `SYMBION_AUTHOR` override. A human typing
  `! symbion add …` inside a session is recorded as `claude` without one.
- **Close done work as a resolved task**, not by deleting the ticket.
  `add --kind task --status resolved --body "done: …"` records work that
  was finished before it was ever ticketed.

## Where things live

- **Store:** `../<repo>-notes`, beside the *main* worktree. Linked worktrees
  share it. `SYMBION_DIR` or `--dir PATH` override; `--dir` may sit anywhere
  in the argv. A `<repo>-notes` store named from another repo resolves in
  `<repo>`, and says so on stderr. So does a command run from inside the
  store itself (to edit `symbion.toml`, say): the store is the one you are
  in, and `init` there refuses.
- **A store that is not named after the repo:** put its path in a
  `.symbion` file in the repo root — one line, relative to the repo or
  absolute, first non-blank line wins. `symbion --dir ../other-notes init`
  writes it for you. Commit it; a relative pointer survives a clone.
  This knob cannot live in `symbion.toml`, because that file is inside the
  store you are trying to find.

  It is worth setting whenever the two names differ, because the failure is
  not a lost store, it is a **forked** one: rename a repo directory and
  `symbion summary` reports `no store at ../<new-name>-notes` (the old store
  is still beside the old name), while the next `symbion add` creates a
  second store there and prints an ordinary id.
- **Files:** `notes.jsonl` (append-only; corrections are new rows that
  supersede old ones), `arcs.jsonl`, `symbion.toml`.
- **Reading:** `symbion summary` (what the hook prints), `symbion context
  --target TYPE:NAME`, `symbion context --branch REF`, `symbion list --json`.
  Every row-producing command takes `--json`.

## Development

```bash
.venv/bin/python -m pytest -q
```

The spec is binding and records why each decision was made. `SKILL.md` is
what an agent reads; keep it short and keep its footgun list honest. It lives
at `src/symbion/data/skill/SKILL.md`, beside `adoption.md` and the hook, and
`~/.claude/skills/symbion` links to that directory: an edit reaches every
session on this machine at once, the same way an edit to `src/` reaches every
`symbion` call through the editable install.
