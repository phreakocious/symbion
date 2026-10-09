# symbion

![symbion: a project's notebook in the browser, and `symbion list` in a terminal over it](https://raw.githubusercontent.com/phreakocious/symbion/main/docs/images/banner.png)

A notebook and ticket registry for one project, shared by you and your coding
agent. Bugs, tasks, decisions and verified results go in as dated notes, so
what a session learned is still there when the next one starts.

<img src="https://raw.githubusercontent.com/phreakocious/symbion/main/docs/images/session.svg" width="100%" alt="A Claude Code session: symbion's hook hands the agent the open rows; asked to work them, it fixes the bug, resolves its row, and hands the question back.">

## What it does for you

- **Each session starts where the last one stopped.** A session-start hook
  gives the agent the open bugs, tasks and questions, the rows due soon, and
  the ones you wrote or answered. You do not brief it again.
- **A result knows its age.** A `check` records what ran, what it said, and
  the commit it ran at. Once the code moves on, it reads `behind 12`, not
  "tests pass".
- **Questions go both ways.** The agent files a `question` for you. Your
  answer, from a terminal or the browser, reaches its next session.
- **Notes attach to things.** A note sits on a commit, a file, a named item
  or the whole project, so `symbion context --target file:src/x.py` finds
  every note on that file.
- **Checklists across a codebase.** An arc is a named line of work with one
  box per item: a file, a test, a host. `symbion arc reconcile` reports the
  boxes whose file was renamed or deleted.
- **Plain files you own.** The notes are JSONL in a git repo beside the
  project. There is no server, account or database. Checking out an old
  branch does not change your tickets.
- **A local web page, if you want one.** `symbion serve` gives boards,
  search, and a page per note.

symbion works with Claude Code, Codex and Hermes Agent, and the CLI works
without any of them.

## Install

Python 3.11+ and git. On Windows, use Git for Windows.

```bash
pipx install 'symbion[gui]'     # or: uv tool install 'symbion[gui]'
```

`[gui]` adds the web page (`symbion serve`); leave it out for the CLI alone.
Run `command -v symbion` from another directory to check that it is on PATH:
the session-start hook finds `symbion` through PATH.

## Quick start

From your project's root:

```bash
symbion init          # lists what it would do, and does nothing
symbion init --yes    # creates the store, ../<repo>-notes, and links the agent skill
```

`init --yes` registers the session-start hook too when `~/.claude/settings.json`
does not exist. When it does, `init` prints the entry to add, or says the hook is
already there.

Then add one line to `CLAUDE.md`, and start a session:

```
- We use symbion for durable notes and tickets.
```

`init --agent codex`, `--agent hermes` or `--agent both` sets up the other
agents, which take a step more: see the
[guide](https://github.com/phreakocious/symbion/blob/main/docs/guide.md#adopt-in-a-fresh-repo).

## Docs

- [Guide](https://github.com/phreakocious/symbion/blob/main/docs/guide.md):
  install on each platform, TAB completion, adopting a repo with each agent,
  the first session, and the web page.
- [Reference](https://github.com/phreakocious/symbion/blob/main/docs/reference.md):
  kinds and arcs, catalogs, conventions, where things live, and development.
- [SKILL.md](https://github.com/phreakocious/symbion/blob/main/src/symbion/data/skill/SKILL.md):
  what the agent reads, and the most complete command reference.
- [Design](https://github.com/phreakocious/symbion/blob/main/docs/superpowers/specs/2026-09-04-symbion-design.md):
  why symbion is built as it is.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q
```

Windows, and making a clone your installed symbion:
[Development](https://github.com/phreakocious/symbion/blob/main/docs/reference.md#development).
