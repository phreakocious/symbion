# symbion

A notebook and ticket registry for agents: dated rows attached to a commit, a file,
an item, an arc or the project, kept in a sibling git repo of JSONL.

## How we work here

- This is _our_ project, and _we_ are collaborators. Your opinions matter here: say when
  an idea is weak, and propose the better one.
- You will often run autonomously. Enjoy it! Act on anything reversible; ask before
  anything destructive or outward-facing.
- Everything we make must be easy to use and to understand, for humans and agents both.
- IMPORTANT: we dogfood symbion here. Each new thread or idea is a candidate arc or row,
  and later work on it goes into the same rows, so we both have its history. When the
  tool gets in your way, file the friction as a row tagged `dogfood` and say so in chat.
  The store is the default sibling, `../symbion-notes`, created on the first write. It
  is local to your machine: if you are not the maintainer, friction worth fixing also
  goes to the repo's issue tracker, where the maintainer can see it.

## Where things are

- `src/symbion/`: `store.py` (rows, invariants, the lock), `api.py` (the door every
  writer uses), `cli.py`, `summary.py` (the session-start text), `gui/` (`symbion serve`).
- `src/symbion/data/skill/`: `SKILL.md`, `adoption.md` and `session_start.sh`, the agent
  surface. `symbion init` links `~/.claude/skills/symbion` to the installed copy.
- `README.md`: the bootstrap guide for a new project.
- `docs/superpowers/specs/`: the designs, amended inline with dated measurements. They
  are binding: a change that departs from one amends it inline, dated.
- Implementation plans stay out of the repo, including `docs/superpowers/plans/`, where
  the superpowers plugin writes them by default. They are execution scripts full of
  local detail; keep yours beside your store, and put what a reviewer needs in the
  change's description.

## Setup and tests

- `python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui,tty]'`, then run tests
  as `.venv/bin/python -m pytest -q`. With `.[test]` alone the GUI and rich tests skip.
  A bare `pytest` is some other Python: its failures and errors mean the wrong
  interpreter, not a bug.
- To dogfood, `symbion` must be on PATH: install this checkout editable as README's
  Install says, then run `symbion init`. The section below then applies.
- To test an old commit in a `git worktree`, run with `PYTHONPATH=$WT/src`. Without it
  the editable install tests this checkout, not the commit.

## When this checkout is the installed symbion

With `symbion` on PATH as an editable install of this checkout and `symbion init` run,
every `symbion` call runs `src/`, and every session on the machine reads its skill and
SessionStart hook from `src/symbion/data/skill/`. A broken file here then breaks every
project's session start. Keep each save runnable: edit bottom-up (store, api, cli), and
do not edit `src/` while a subagent runs `symbion`.

## This repo is public

Evidence from other projects goes in a symbion row, never in code, tests, docs or a
commit message: another project's name, a row id, a count that identifies its store, a
quote of its rows, a path or line in its repo. Those state the mechanism: "a bare
`0 rows` read as 0-of-0", not "(measured in <project>)". Unnamed dates are fine.
