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
- Before you trust a new test, break the code it guards and watch the test fail.
- A `# ponytail: <limit, upgrade path>` comment marks a deliberate simplification.
- State the mechanism, not where you saw it: no names, paths or data from your other
  projects in code, tests, docs or commit messages.

## Where things are

- `src/symbion/`: `store.py` (rows, invariants, the lock), `api.py` (the write path:
  `gui/` and `cli.py` write through it, and `tests/test_gui_seam.py` names the two
  exceptions), `cli.py`, `summary.py` (the session-start text), `term.py` (what a
  person sees at a terminal: `list`, `show` and `context` in columns, and the palette
  every other command and `--help` print in), `gui/`
  (`symbion serve`).
- `src/symbion/data/skill/`: `SKILL.md`, `adoption.md`, `catalogs.md` and
  `session_start.sh`, the agent surface. They ship in the package; `symbion init` links
  `~/.claude/skills/symbion` to the installed copy.
- `README.md`: install, and the guide to adopting symbion in a project.
- `docs/superpowers/specs/`: the designs, dated.

## Setup and tests

Python 3.11 or later:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q
```

Run the tests with the venv's Python, not a bare `pytest`: `tests/test_hook.py` runs
the `symbion` script installed beside the interpreter. With `.[test]` alone, the GUI
tests skip.

To try a change by hand, make a scratch store and name it with `--dir`:

```bash
d=$(mktemp -d)
.venv/bin/python -c 'import sys; from symbion import store; store.ensure_store(sys.argv[1])' "$d"
.venv/bin/symbion --dir "$d" …
```

SKILL.md's rule to run the installed `symbion` is for using symbion, not for
developing it. A write refuses a store `init` never made, and without `--dir` a
command uses `../<repo>-notes` beside the current repo. Run `.venv/bin/symbion init`
only when the user asks: it creates that store, it can point the user-wide skill and
hook at this checkout (README, "Adopt in a fresh repo"), and with `--dir` it writes a
`.symbion` pointer into the checkout.

## If this checkout is your installed symbion

When the `symbion` on PATH is this checkout's editable install and
`~/.claude/skills/symbion` links into it, every `symbion` call runs this `src/`, and
every Claude Code session on the machine reads its skill and hook from
`src/symbion/data/skill/`. A broken save then prints a traceback at the start of every
session, in every project. Keep each save runnable: edit bottom-up (store, api, cli),
and do not edit `src/` while a `symbion` command runs in another session.
