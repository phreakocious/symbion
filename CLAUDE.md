# symbion

A notebook and ticket registry for agents: dated rows attached to a commit, a file,
an item, an arc or the project, kept in a sibling git repo of JSONL.

## Conventions

- Everything symbion prints or documents must be easy to use and to understand, for
  humans and agents both.
- The specs are dated design records: why symbion is built as it is, and the design as
  of that date. The code, tests, README and SKILL.md say what it does now. Where a spec
  and the code disagree and the spec's reason still holds, the code has a bug. A change
  that reverses a spec's decision adds a dated note at that decision; renames and
  signatures need none.
- Before you trust a new or changed test, break the code it guards and watch the test
  fail. In a checkout that is your installed symbion, break a copy: copy `src`, `tests`
  and `pyproject.toml` to a scratch dir and patch there. `tests/conftest.py` puts that
  copy's `src/` first.
- A `# ponytail: <limit, upgrade path>` comment marks a deliberate simplification.
- State the mechanism, not where you saw it: no names, paths or data from your other
  projects in code, tests, docs or commit messages. No count from any store, this one
  included; a fixture's zone is never your own.

## Where things are

- `src/symbion/`: `store.py` (rows, invariants, the lock), `api.py` (the write path:
  `gui/` and `cli.py` write through it, and `tests/test_gui_seam.py` names the two
  exceptions), `cli.py`, `summary.py` (the session-start text), `term.py` (what a
  person sees at a terminal: `list`, `show` and `context` in columns, and the palette
  every other command, `--help` and the GUI print in), `gui/` (`symbion serve`;
  `gui/theme.py` adds only its surfaces to that palette).
- `src/symbion/data/skill/`: `SKILL.md`, `adoption.md`, `catalogs.md` and
  `session_start.sh`, the agent surface. They ship in the package; the Claude Code
  plugin reaches the installed copy through `symbion skill`, and `symbion init --agent
  codex` or `--agent hermes` links it.
- `plugin/`: the Claude Code plugin, which holds no copy of the skill: a stub skill,
  `/symbion:setup`, `/symbion:wrap` and two `sh` hooks that run the package's scripts.
  `.claude-plugin/marketplace.json` makes the repo its marketplace. `package.json`
  makes the repo a Pi package whose one extension, `plugin/pi/symbion.ts`, runs the
  same hooks and sends the setup and wrap skills as `/symbion-setup` and
  `/symbion-wrap`.
- `README.md`: what symbion does and how to install it. `docs/guide.md`: install
  in full and adopting symbion in a project. `docs/reference.md`: how it behaves, and
  development.
- `docs/superpowers/specs/`: the designs, dated.

## Setup and tests

Python 3.11 or later:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q -n auto
```

Run the tests with the venv's Python, not a bare `pytest`: `tests/test_hook.py` runs
the `symbion` script installed beside the interpreter. With `.[test]` alone, the GUI
tests skip. `-n auto` runs the suite on every core (pytest-xdist): each test already
isolates `HOME`, the cwd and the cache. One test runs faster without it.

To try a change by hand, make a scratch store and name it with `--dir`:

```bash
d=$(mktemp -d)/s-notes      # a fresh parent: every command scans a store's siblings
.venv/bin/python -c 'import sys; from symbion import store; store.ensure_store(sys.argv[1])' "$d"
.venv/bin/symbion --dir "$d" …
```

SKILL.md's rule to run the installed `symbion` is for using symbion, not for
developing it. A write refuses a store `init` never made, and without `--dir` a
command uses `../<repo>-notes` beside the current repo. Run `.venv/bin/symbion init
--yes` only when the user asks: it creates that store, with `--agent codex` or
`--agent hermes` it can point that agent's user-wide skill and hook at this checkout
(`docs/guide.md`, "Adopt in a fresh repo"), and with `--dir` it can write a `.symbion`
pointer into the checkout. Without `--yes` it only lists them. A scratch `init --yes`
in a test sets `HOME` (on Windows, `USERPROFILE` too) to a scratch dir on the same
command line; without it, init acts on your real skill links and hooks.

## If this checkout is your installed symbion

When the `symbion` on PATH is this checkout's editable install and the symbion plugin
is installed, every `symbion` call and the plugin's hooks run this `src/`, and every
Claude Code session on the machine reads its skill and hooks from
`src/symbion/data/skill/`. A broken save then prints a traceback at the start of every
session, in every project, and a save that compiles can still break another session's
write: a call saved before its import passed `py_compile` and raised NameError there
until the import landed (2026-11-02).

So edit `src/` in a git worktree, never in this checkout. `git worktree add
.worktrees/NAME` makes one on branch NAME, and `.gitignore` covers it. In the worktree,
run the tests with this checkout's venv, `../../.venv/bin/python -m pytest -q -n auto`:
`tests/conftest.py` puts the worktree's `src/` first. `.venv/bin/symbion` and
`.venv/bin/python -c` run this checkout's `src/` unless you prefix them with
`PYTHONPATH=$PWD/src`. When the branch's tests pass, run `git merge --ff-only NAME`
here, then `git worktree remove .worktrees/NAME`.
