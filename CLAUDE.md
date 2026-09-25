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

## Where things are

- `src/symbion/`: `store.py` (rows, invariants, the lock), `api.py` (the door every
  writer uses), `cli.py`, `summary.py` (the session-start text), `gui/` (`symbion serve`).
- `src/symbion/data/skill/`: `SKILL.md`, `adoption.md` and `session_start.sh`, the agent
  surface. `~/.claude/skills/symbion` links to this directory.
- `README.md`: the bootstrap guide for a new project.
- `docs/superpowers/specs/`: the designs, amended inline with dated measurements.
  Plans do not go here: write them to `../symbion-notes/archive/symbion-docs/plans/`.
  They are execution scripts full of field detail, and this repo is published.
- `../symbion-notes`: this project's store. Its open rows are the work list;
  `NEXT_SESSION.md` is orientation only and points there.

## This checkout is live on the whole machine

- `symbion` on PATH is an editable install of `src/`, and every session's skill and
  SessionStart hook read `src/symbion/data/skill/`. A broken file here breaks every
  project's session start. Keep each save runnable: edit bottom-up (store, api, cli),
  and do not edit `src/` while a subagent runs `symbion`.
- Run tests as `.venv/bin/python -m pytest -q`. Bare `pytest` is a system Python without
  the test deps: 4 hook failures and 35 GUI errors mean the wrong interpreter, not a bug.
- To test an old commit in a `git worktree`, run with `PYTHONPATH=$WT/src`. Without it
  the editable install tests this checkout, not the commit.

## This repo is published

- Field provenance goes in a symbion row, never in code, tests, docs or a commit
  message: another project's name, a row id, a count that identifies its store, a
  quote of its rows, a path or line in its repo. Those state the mechanism: "a bare
  `0 rows` read as 0-of-0", not "(measured in <project>)". Unnamed dates are fine.
- In the maintainer's clone, `.git/hooks/pre-commit` and `commit-msg` run a
  private-name check whose list lives in `../symbion-notes/tools/`, because the list
  is private too. A refusal means rewrite the line, never `--no-verify`.

## Adjacent stores

Other projects' stores are the field data for this tool. Read them when a question needs
context: how agents use a feature, whether a fix reached them, what a friction report
looked like at its source.

- The stores are the `../*-notes` siblings of this checkout. Most sit beside a repo of
  the same name; a `.symbion` file in a repo names any other. The store's rows tagged
  `adjacent-stores` record the exceptions.
- Read from the project's directory, `(cd ../<project> && symbion summary)`, so its
  catalogs and check state resolve against its own repo.
- The project's `NEXT_SESSION.md`, `CLAUDE.md` and the store's `symbion.toml` are fair
  reading too.
- Read only. Another session may be live there: a write to another project's store,
  config or files waits for the owner's word.
- Cite what you read by project and row id in the row you write here, and only there.
