"""The CLI. A console script deletes the reference's invocation footguns
outright: no `-m`, no repo-root cwd requirement, no `--dir` position trap
(the store resolves before subcommand dispatch), and `--json` removes the
need to parse a text line, whose layout is for a reader and may change.

This is the one module allowed to import every other symbion module.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import errno
import functools
import io
import ipaddress
from importlib import metadata, resources
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
from collections import Counter
from datetime import date
from pathlib import Path

from . import api, catalog, config, gitref, kinds as K, merge, store
from . import summary as summ


# ---- author resolution ----
def _resolved_author(args) -> str:
    # One rule for every write, as add's: resolve stored `unknown`, arc create "".
    if args.author is not None and not args.author.strip():
        raise SystemExit("error: author is required: --author names who writes it; "
                         "leave the flag out for the default")
    return args.author if args.author is not None else api.author_default()


# ---- init ----
# The agent surface is linked ONCE per user, into the package:
# ~/.claude/skills/symbion -> <symbion>/data/skill. Per-project copies drifted
# within the hour (2026-09-04) and re-staled every adopter on every SKILL.md
# edit; a link into the install cannot drift.
_SKILL_LINK = Path(".claude/skills/symbion")          # under $HOME
_CODEX_SKILL_LINK = Path(".agents/skills/symbion")


def _hermes_home() -> Path:
    """Hermes's profile home: its skills/ and config.yaml. A named profile
    has its own, named by HERMES_HOME."""
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
_HOOK_COMMAND = 'sh "$HOME/.claude/skills/symbion/session_start.sh"'
# Codex runs a hook in the session's cwd (its hooks docs, 2026-10-02). The
# project is named so a CLAUDE_PROJECT_DIR inherited from a Claude Code parent
# cannot pick another one, and the author so `summary` knows its reader:
# whether a hook's process carries CODEX_THREAD_ID is not measured.
_CODEX_HOOK_COMMAND = ('SYMBION_PROJECT_DIR="$PWD" SYMBION_AUTHOR="${SYMBION_AUTHOR:-codex}" '
                       'sh "$HOME/.agents/skills/symbion/session_start.sh"')


HOOK_TIMEOUT = 10                   # seconds Claude Code gives the session-start hook


def _hook_entry(command: str) -> dict:
    return {"matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": command,
                       "timeout": HOOK_TIMEOUT, "statusMessage": "Reading symbion entries..."}]}


def _codex_hook_entry(link: Path) -> dict:
    entry = _hook_entry(_CODEX_HOOK_COMMAND)
    # Codex's Windows override runs without Git Bash on PATH. An absolute
    # path avoids shell-specific HOME expansion; -File handles spaces.
    if os.name == "nt":
        entry["hooks"][0]["commandWindows"] = (
            'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{link / "session_start.ps1"}"')
    return entry


# The script's path, not the command: JSON escapes the command's quotes, and a
# hand-written entry may spell it `~/...`; either way it runs this file.
_HOOK_SCRIPT = "skills/symbion/session_start.sh"
_WINDOWS_HOOK_SCRIPT = "skills/symbion/session_start.ps1"
# The gotcha hook (gotchas.sh), after each tool call that can name a file.
# Claude Code reads a matcher of plain names as an exact list (its hooks docs).
_GOTCHA_COMMAND = 'sh "$HOME/.claude/skills/symbion/gotchas.sh"'
_GOTCHA_SCRIPT = "skills/symbion/gotchas.sh"


def _gotcha_entry() -> dict:
    return {"matcher": "Read|Edit|Write|NotebookEdit|Bash",
            "hooks": [{"type": "command", "command": _GOTCHA_COMMAND, "timeout": 5}]}


def _skill_dir() -> Path:
    return Path(str(resources.files("symbion") / "data" / "skill"))


# Each change init makes, printed in the past tense once made, and as
# `will <verb>` in the list a run without --yes prints instead.
_PAST = {"create": "created", "write": "wrote", "link": "linked", "relink": "relinked",
         "set": "set", "keep": "kept", "merge": "merged"}


def _act(write: bool, verb: str, what: str) -> bool:
    """Print one line of what init does or would do; True unless it keeps."""
    print(f"{_PAST[verb] if write else 'will ' + verb} {what}")
    return verb != "keep"


def _link_user_skill(write: bool = True, agent: str = "claude", merge: bool = False,
                     missing: list | None = None) -> bool:
    """Link the skill, and register the hook in the user's settings only when
    that file is absent: merging someone's JSON is not this tool's business
    unless they ask, so an existing file without the hook gets the block
    printed instead, or merged under `merge` (--merge-hook), with or without
    `write`: the person typed that flag for that one change. An agent's own
    merge was allowed in one run and denied as self-modification in the next
    (2026-10-10), and a command the person types is not the agent's. A skill
    dir that is not this link is someone's; it is named, never replaced.
    Returns whether it changes anything."""
    codex = agent == "codex"
    # Hermes searches its own skills/ first and reads a link there, where
    # ~/.agents/skills took an edit to config.yaml, which cost a tester most
    # of 45 minutes (hermes-agent v0.22.1, 2026-10-15; the user's choice).
    link = (_hermes_home() / "skills" / "symbion" if agent == "hermes"
            else Path.home() / (_SKILL_LINK if agent == "claude" else _CODEX_SKILL_LINK))
    target = _skill_dir()
    command = _CODEX_HOOK_COMMAND if codex else _HOOK_COMMAND
    entry = _codex_hook_entry(link) if codex else _hook_entry(command)
    command = entry["hooks"][0].get("commandWindows", command)
    changed = False
    # resolve() follows a symlink or a junction, and a real directory resolves
    # to itself, never to the package.
    # A reinstall moved the package: the link pointed nowhere, and was left.
    dangling = link.is_symlink() and not link.exists()
    if link.exists() and link.resolve() == target.resolve():
        changed |= _act(write, "keep", f"{link} -> {target}")
    elif os.path.lexists(link) and not dangling:   # a dangling junction too, which is no symlink
        print(f"note: {link} exists and is not a link to {target}; left alone")
    else:
        if write:
            if dangling:
                link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                # A symlink needs Developer Mode or an elevated shell; a
                # junction needs neither.
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
        changed |= _act(write, "relink" if dangling else "link", f"{link} -> {target}")
    if agent == "hermes":
        # Hermes has no hook whose output reaches the model as a summary: its
        # on_session_start return is ignored, and pre_llm_call fires every turn
        # and wants JSON (hermes-agent docs, 2026-10-06). init cannot see the
        # AGENTS.md line was added, so it names it every run.
        print(f"note: this is the skill of the Hermes profile at {_hermes_home()}; for "
              f"another profile, run init again with its HERMES_HOME")
        print("note: Hermes runs no symbion hook; for the session-start summary, add this "
              "line to the project's AGENTS.md:\n- At the start of a session, run "
              "`symbion summary` unless a hook already printed it.")
        if merge:
            print("note: --merge-hook merges nothing for Hermes, which runs no symbion hook")
        return changed
    settings = ((Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
                 / "hooks.json") if codex else Path.home() / ".claude" / "settings.json")
    # Codex skips a new or changed hook until the person trusts it, and says
    # nothing in the session that skipped it. init cannot read the trust (a
    # hash of Codex's own scheme), so a run that keeps the hook says it too.
    trust = "Codex runs this hook only after you trust it: in Codex, run /hooks to review it"
    # Codex also reads hooks inline in config.toml; one there is not doubled.
    inline = settings.with_name("config.toml")
    if codex and inline.exists() and _hook_registered(inline, toml=True):
        changed |= _act(write, "keep", f"hook in {inline}")
        print(f"note: {trust}")
        return changed
    # Claude Code also runs the gotcha hook after each call that can name a
    # file, and after each that fails: a Bash call that exits non-zero never
    # reaches PostToolUse (2026-10-13). Codex edits through apply_patch, which
    # this matcher does not name.
    hooks = [("SessionStart", entry, f"runs {command} in every session",
              (_HOOK_SCRIPT, _WINDOWS_HOOK_SCRIPT))]
    if agent == "claude":
        hooks.append(("PostToolUse", _gotcha_entry(), f"runs {_GOTCHA_COMMAND} after each "
                      "Read, Edit, Write, NotebookEdit and Bash call", (_GOTCHA_SCRIPT,)))
        hooks.append(("PostToolUseFailure", _gotcha_entry(), "runs it after each of "
                      "those calls that fails", (_GOTCHA_SCRIPT,)))
    if not settings.exists() and not merge:
        if write:
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({"hooks": {ev: [e] for ev, e, _, _ in hooks}},
                                           indent=2) + "\n", encoding="utf-8")
        changed |= _act(write, "write", f"{settings} ("
                        + "; ".join(f"a {ev} hook that {what}" for ev, _, what, _ in hooks) + ")")
        if write and codex:
            print(f"note: {trust}")
        return changed
    advised = False
    for event, e, _, scripts in hooks:
        label = "hook" if event == "SessionStart" else f"{event} hook"
        registered = _hook_registered(settings, event=event, scripts=scripts) \
            if settings.exists() else False
        # The entry, not a whole settings object: pasted over a file that has
        # SessionStart hooks already, the object replaced them (adopter report).
        add = (f'append this entry to the "{event}" list under "hooks" '
               f'(create either if missing):\n{json.dumps(e, indent=2)}'
               + (f"\n{trust}" if codex else ""))
        if registered:
            # Said out loud, as the skill link is: a re-run printed nothing here,
            # so "hook checked and present" read the same as "hook not checked"
            # (adopter report, 2026-09-28).
            changed |= _act(write or merge, "keep", f"{label} in {settings}")
            if codex:
                print(f"note: {trust}")
        elif registered is None:
            if merge:
                raise SystemExit(f"{settings} cannot be read as a JSON object with a list under "
                                 f"hooks.{event}, so --merge-hook left it unchanged; fix it, "
                                 f"or {add}")
            print(f"note: {settings} cannot be read as a JSON object, so whether it "
                  f"registers the symbion {event} hook is unknown; to register it, {add}")
        elif merge:
            if not (write or os.path.lexists(link)):
                # The hook runs its script through the link: merged first, it
                # fails at every session start in every project.
                raise SystemExit(f"--merge-hook left {settings} unchanged: the hook runs its "
                                 f"script through {link}, which is not there yet; run "
                                 f"`symbion init --yes --merge-hook"
                                 f"{' --agent codex' if codex else ''}` to make both")
            _merge_hook(settings, e, event=event)
            _act(True, "merge", f"the symbion {event} hook into {settings}")
            if codex:
                print(f"note: {trust}")
        else:
            cmd = "symbion init --merge-hook" + (" --agent codex" if codex else "")
            print(f"{settings} does not register the symbion {event} hook. To register it, "
                  f"run `{cmd}`, or {add}")
            if missing is not None:
                missing.append((event, cmd))
            if not advised:
                print(f"An agent whose edit of that file was denied does not run --merge-hook "
                      f"itself, which would go around the denial: it asks the person to run "
                      f"it (in Claude Code, by typing `! {cmd}` at the prompt).")
                advised = True
    return changed


def _merge_hook(settings: Path, entry: dict, *, event: str = "SessionStart") -> None:
    """Append `entry` to hooks[event] and keep the rest of the file.
    Only after _hook_registered read it as an object whose `hooks` and
    `SessionStart` are absent or of the right type; an absent file is `{}`.
    Written through a symlink to its target, as a dotfiles repo links it."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    hooks = data["hooks"] = data.get("hooks") or {}
    hooks[event] = [*(hooks.get(event) or []), entry]
    settings.parent.mkdir(parents=True, exist_ok=True)
    store._replace_atomically(settings.resolve(),
                              json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _hook_registered(settings: Path, *, toml: bool = False, event: str = "SessionStart",
                     scripts=(_HOOK_SCRIPT, _WINDOWS_HOOK_SCRIPT)) -> bool | None:
    """Does `hooks[event]` run our script? None means the file could not
    be read as the JSON object it must be, so neither answer is available.

    The test used to be a substring match over the whole file, which the
    command under another event key -- or the path quoted in a note -- also
    passes, and an unreadable file failed it, reading as "not registered"
    (adopter report, 2026-09-28). A file with no hooks at all IS the negative
    case; a shape this cannot walk is not."""
    try:
        text = settings.read_text(encoding="utf-8")
        data = tomllib.loads(text) if toml else json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks")
    if hooks is None:
        return False
    if not isinstance(hooks, dict):
        return None
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        return None
    return any(script in str(h.get(key, "")).replace("\\", "/")
               for e in entries if isinstance(e, dict)
               for h in (e.get("hooks") or []) if isinstance(h, dict)
               for key in ("command", "commandWindows", "command_windows")
               for script in scripts)


def _git_says(root: Path, path: Path) -> str:
    """Whether the next `git add -A` takes `path`. In a public repo that did
    not ignore `.claude/`, init wrote three files there and said nothing
    (2026-09-23). A tracked path is taken whatever the ignore rules say."""
    git = ["git", "-C", str(root)]
    if subprocess.run([*git, "ls-files", "--error-unmatch", "--", str(path)],
                      capture_output=True).returncode == 0:
        return "git: tracked"
    rc = subprocess.run([*git, "check-ignore", "-q", "--", str(path)],
                        capture_output=True).returncode
    return {0: "git: ignored", 1: "git: not ignored"}.get(rc, "git: check-ignore failed")


def _ignored_paths(store_dir, prefix: str) -> list[str]:
    """Paths under `prefix` that `git add -A` skips by a `.gitignore` in the
    store. `--ignored=matching` names each ignored FILE; the default collapses
    them to the directory. -z: a quoted name failed the prefix test. A rule
    from the user's own excludes file ignores their litter (`.DS_Store`), and
    `add -f` would commit it."""
    git = ["git", "-C", str(store_dir), "--no-optional-locks"]
    r = subprocess.run([*git, "status", "--porcelain", "-z", "--ignored=matching"],
                       capture_output=True, text=True, encoding="utf-8")
    paths = [e[3:] for e in r.stdout.split("\0")
             if e.startswith("!! ") and e[3:].startswith(prefix + "/")]
    if not paths:
        return []
    # -v -z: source, line, pattern, path, each ended by a NUL.
    c = subprocess.run([*git, "check-ignore", "-v", "-z", "--stdin"], input="\0".join(paths),
                       capture_output=True, text=True, encoding="utf-8")
    f = c.stdout.split("\0")
    return sorted(path for src, path in zip(f[0::4], f[3::4])
                  if Path(src).name == ".gitignore" and not Path(src).is_absolute()
                  and not src.startswith("~"))


def _say_ignored_archive(store_dir, would=False) -> None:
    """`git add -A` skips ignored paths silently. A tree copied verbatim
    under archive/ can carry its own `.gitignore` of `*`, and then the copy
    is on disk, `diff -r` passes, and the store's git holds none of it
    (2026-09-28). archive/ exists to be committed, so anything ignored there
    is the bug, not a choice. The dry run says it too (a clean-room review,
    2026-10-13)."""
    skipped = _ignored_paths(store_dir, "archive")
    if skipped:
        more = f", +{len(skipped) - 3} more" if len(skipped) > 3 else ""
        print(f"note: {len(skipped)} path(s) under archive/ are gitignored, so this "
              f"commit {'would not hold' if would else 'does not hold'} them: "
              f"{', '.join(skipped[:3])}{more} (git -C {store_dir} add -f archive)",
              file=sys.stderr)


def _init(store_dir, cfg, store_from_env: bool = False, write: bool = False,
          repoint: bool = False, agent: str = "claude", merge_hook: bool = False) -> int:
    """Without `write`, list each change and make none (the user's call,
    2026-10-01): installed from pipx, init is run by someone who has not read
    its source, and the skill link and the hook are instruction surfaces
    every Claude Code session reads. Exit 1 when there is something to
    change, so a run whose output was thrown away still fails."""
    # Under [repos] the roots stand in a listed repo, or in none outside
    # every one (config.load); init installs into the cwd's own.
    root = cfg.cwd_root if cfg.repos else cfg.project_root
    if root is None:
        raise SystemExit(f"init needs a git repository: {os.getcwd()} is in none, and "
                         f"the store is derived from the project")
    if Path(store_dir).resolve() == Path(root):
        raise SystemExit(f"init installs into a project, and {store_dir} is the store; "
                         f"run it from the project (--dir naming the store if it is "
                         f"not ../<project>-notes)")
    changed = False
    new = not store.exists(store_dir)
    if write:
        store.ensure_store(store_dir)       # an existing store gets any piece it lacks
    if new:
        changed |= _act(write, "create", f"{store_dir} (the store: a git repo)")
    up = gitref.set_upstream(store_dir, write)
    if up:
        changed |= _act(write, "set", f"upstream {up}")   # a bare `git push` of the store works
    p = Path(store_dir) / config.CONFIG_FILE
    if p.exists():
        changed |= _act(write, "keep", f"{p}")   # re-run: never the config
    else:
        if write:
            _write_starter_toml(p, cfg)
        changed |= _act(write, "write", f"{p}")
    readme = Path(store_dir) / "README.md"
    if not readme.exists():                     # re-run: never the README either
        if write:
            _write_store_readme(readme, Path(root).name)
        changed |= _act(write, "write", f"{readme}")
    missing = []
    for selected in (("claude", "codex") if agent == "both" else (agent,)):
        changed |= _link_user_skill(write, selected, merge_hook, missing)
    if changed and agent in ("codex", "both"):
        s = shlex.quote(Path(store_dir).resolve().as_posix())
        print(f"note: Codex's sandbox writes only inside the project, and the store is "
              f"outside it: launch `codex --add-dir {s}`, or add {s} to "
              "sandbox_workspace_write.writable_roots in Codex's config.toml. "
              "`symbion commit` may still ask for approval.")
    changed |= _record_pointer(Path(store_dir), Path(root), store_from_env,
                               write, repoint)
    if sys.stdout.isatty():
        # A person's terminal only: an agent has no tty to judge (the user,
        # 2026-10-06). The terminal usually does more than its TERM says.
        from . import term
        if term.SIXTEEN:
            print(f"note: this terminal reports 16 colours (TERM="
                  f"{os.environ.get('TERM', '')}), so symbion prints in plainer ones; "
                  "most terminals do more: set COLORTERM=truecolor, or a TERM ending "
                  "in -256color")
    # Last, where the eye stops: `nothing to change` after a missing hook's
    # paragraph ended an upgrade with the hook unregistered (2026-10-12).
    rest = "nothing else" if merge_hook else "nothing"
    if not write and (changed or not missing):
        print(f"{rest} written: re-run with --yes to make these changes" if changed
              else f"{rest} to change")
    for event, cmd in missing:
        print(f"the symbion {event} hook is not registered: run `{cmd}`")
    return 0 if write else int(bool(changed or missing))


def _record_pointer(store_dir: Path, project_root: Path, store_from_env: bool = False,
                    write: bool = True, repoint: bool = False) -> bool:
    """Make a non-default store durable, so the next bare command finds it.

    Without this, `symbion --dir ../other-notes init` is forgotten the moment
    the process exits: the next `add` started a SECOND store beside the
    first (measured 2026-09-11, the same silent fork a renamed repo
    produced), and since 2026-09-28 every bare command stops at `no store at`. Relative when the store is a sibling, so the pointer survives a
    clone; absolute otherwise.

    NOT under SYMBION_DIR. config.store_dir keeps the env var outermost so "a
    one-off invocation against another store must not be overridden by a file
    in the tree"; the same reasoning forbids WRITING that file. Measured
    2026-09-11: `SYMBION_DIR=/tmp/scratch symbion init`, run with cwd in a
    real project to test something unrelated, pointed that project at the
    scratch store, and every later bare command there would have read an empty
    one -- which prints nothing and reads as "no store here". The env var is
    per-invocation and often set by a wrapper; `--dir` is typed on purpose.

    On the default store it never writes and never deletes: an existing
    pointer naming somewhere else is reported. Removing one is the user's
    call, and silence would leave two stores with nothing saying which the
    next command uses.

    Nor when the project reads another store that exists, unless `repoint`.
    From an adopter, 2026-10-02: `--dir <scratch> init`, run inside a project
    to try a `[kinds]` change, repointed its tracked `.symbion` at the
    scratch store, and deleting the scratch store broke every command there.
    `--dir` means "that store, leave mine alone" for every other verb. A
    pointer whose store is gone is broken already, so it is replaced freely.
    Unless git tracks it (a shared-store trial, 2026-10-27): then it names the store
    for every clone in the team's layout, and missing HERE broke nothing. A
    second person's `--dir <their store> init --yes` rewrote it to an absolute
    path, which the next `git commit -a` would have pushed to everyone.

    Returns whether it changes the pointer."""
    pointer = project_root / config.POINTER_FILE
    default = (project_root.parent / f"{project_root.name}-notes").resolve()
    store_dir = store_dir.resolve()
    # Compared by where it RESOLVES (config's own reader), not by its text:
    # a pointer reading `../<name>-notes` names the default and is fine.
    named = config._pointer(project_root)
    if store_dir == default:
        if named is not None and named != store_dir:
            print(f"note: {pointer} names {pointer.read_text(encoding='utf-8').strip()!r}, not this store; "
                  f"delete it if that is stale")
        return False
    if store_from_env:
        print(f"note: store came from SYMBION_DIR; not writing {pointer}. "
              f"Use `symbion --dir {store_dir} init --yes` to make it durable")
        return False
    if named == store_dir:
        return False
    current = named or default
    if store.exists(current) and not repoint:
        print(f"note: not writing {pointer}: this project reads {current}, a store that "
              f"exists. Name {store_dir} with --dir to use it, or re-run with --repoint "
              f"to make this project read it")
        return False
    if named is not None and not repoint and _git_says(project_root, pointer) == "git: tracked":
        print(f"note: not writing {pointer}: git tracks it, so every clone reads {current}, "
              f"which is missing here. Clone or move the store there, or re-run with "
              f"--repoint to change it for every clone that takes the commit")
        return False
    if store_dir.parent == project_root.parent:
        value = f"../{store_dir.name}"
    else:
        value = store_dir.as_posix()
    if write:
        pointer.write_text(value + "\n", encoding="utf-8", newline="\n")
    was = ""
    if store.exists(current):
        was = f" (was {current}: that store is no longer read from here)"
    elif named is not None:
        was = f" (was {current}, where no store is)"
    says = _git_says(project_root, pointer)
    changed = _act(write, "write", f"{pointer} -> {value}{was} ({says})")
    if says == "git: not ignored":
        # The label alone said nothing of what to do (an adoption report,
        # 2026-10-13). A tracked pointer is a real mode, so init writes
        # neither. --git-path from the cwd: the path the user's shell can use.
        exclude = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"],
                                 capture_output=True, text=True).stdout.strip()
        print(f"note: commit {pointer.name} so each clone reads this store, or keep it out "
              f"of git: echo {pointer.name} >> {shlex.quote(exclude or '.git/info/exclude')}")
    return changed


def _write_store_readme(p: Path, project: str) -> None:
    """What a person who opens the store's repo meets, on a forge or a disk,
    in place of bare JSONL (the user, 2026-10-05)."""
    p.write_text(f'''![symbion](https://raw.githubusercontent.com/phreakocious/symbion/main/docs/images/banner.png)

# {p.parent.name}

A [symbion](https://github.com/phreakocious/symbion) store, first set up for
`{project}`: dated notes, decisions, checks and tickets, each attached to a
commit, a file, an item, an arc or the whole project.

- `notes.jsonl`: the entries, one JSON object per line. An edit keeps the version
  it replaces, on its own line.
- `arcs.jsonl`: the arcs, named checklists of entries.
- `docs/`: an entry's longer detail, one file each; the entry names it `doc:NAME`.
- `symbion.toml`: this store's kinds, catalogs and settings.

Read it from a checkout of a project that uses it, with `symbion summary`,
`symbion list` or `symbion serve`. Write with `symbion add`, not by hand.
''', encoding="utf-8", newline="\n")


def _write_starter_toml(p: Path, cfg) -> None:
    p.write_text(f'''# symbion configuration. Every value has a default; delete what you do not need.
# project_root = "{cfg.project_root}"   # default: first worktree of `git worktree list --porcelain`.
#                                        # Pinning this by hand also pins work_root (below) to
#                                        # the same tree, unless work_root is set explicitly too.
# work_root = "{cfg.work_root}"         # default: the CURRENT worktree (`git rev-parse
#                                        # --show-toplevel`) -- HEAD, branches and dirty state
#                                        # resolve here, never against project_root, since those
#                                        # are per-worktree and project_root is deliberately the
#                                        # MAIN worktree so every linked worktree shares one store.
default_branch = "{gitref.repo_default_branch(cfg) or cfg.default_branch}"
# local_only = true   # no remote on purpose: `commit` stops saying "no remote"
# shared = true       # experimental: one store, a clone per person; session start pulls, `commit` pulls and pushes
# git_name = "symbion-notes"          # the author of the store's own commits: set
# git_email = "symbion-notes@local"   # yours where a remote refuses a made-up one

# A catalog type is a command emitting ONE NAME PER LINE and nothing else. It
# runs in the root of the current worktree. Dry-run each new one before its
# first seed: `symbion arc seed --scope <type> --dry-run`. Why, and the resolver
# protocol, are in catalogs.md, in the skill `init` linked, or at
# https://github.com/phreakocious/symbion/blob/main/src/symbion/data/skill/catalogs.md
[catalogs]
# `--others --exclude-standard` lists files not yet committed, so a note on a
# file made this session resolves instead of warning `taken as typed`. To leave
# a vendored tree out of the catalog, end the command with `-- . ':!vendor'`.
# file = "git ls-files --cached --others --exclude-standard"   # pair with `file = "git"` under [renames] below
# test = "git ls-files --cached --others --exclude-standard 'tests/test_*.py'"
# A STORE-DERIVED catalog is empty until its first entry exists, and that entry
# cannot be written while the catalog is empty: it NEEDS its [resolvers]
# entry below, which decides what a first sighting stores. Uncomment both.
# reading = 'out=$(symbion list --all --json --type reading) && printf "%s\\n" "$out" | jq -r "[.[].target.name] | unique | .[]"'

# A resolver picks the name to store for ONE catalog type, in place of the
# built-in match. A NUMERIC catalog needs one, or it fragments: 3.1416 is not
# a substring of 3.14159, so it silently becomes a second target. A catalog
# that pipes must fail when its producer does, or a failed producer reads as
# an empty catalog. `sh` is dash on Debian and Ubuntu, which refuses
# `set -o pipefail`, so `reading` above checks its producer with `out=$(…) &&`.
# Its protocol is in catalogs.md (above).
[resolvers]
# reading = "python3 tools/resolve_reading.py"   # required by the store-derived catalog above
# host = "closed"   # the built-in match, refusing a name the catalog does not list

[renames]
# file = "git"      # built-in NUL-safe rename detection with transitive chains;
#                   # without it `arc reconcile` cannot follow a renamed file

# Kinds: a label on three bits. status = carries open/resolved and counts
# in arcs and at session start; parked = has a status but is hidden from
# every open view (requires status); verdict = carries --checked/--result,
# is stamped with provenance at write, and has a state derived from HEAD.
# status + verdict is a pre-registration: it cannot be resolved without
# --result. This table REPLACES the defaults when present. Renaming a label
# that already has entries also needs, once, by hand:
#   macOS: sed -i '' -e 's/"kind": "old"/"kind": "new"/g' notes.jsonl
#   GNU:   sed -i    -e 's/"kind": "old"/"kind": "new"/g' notes.jsonl
# `symbion schema` prints this table with an entry count per label.
{K.render_toml(K.DEFAULT_KINDS)}''', encoding="utf-8", newline="\n")


# ---- text rendering ----
def _painter(kinds=None, gui=None):
    """term.painter() at a terminal, else summary.plain. The import sits
    here for the reason _print_note's does: a pipe never loads rich.
    `kinds` is the store's table, whose `color`s paint its kinds; `gui`, a
    running serve's URL, links each entry's id."""
    if not sys.stdout.isatty():
        return summ.plain
    from . import term
    return term.painter(kinds, gui)


def _hook_json(text: str, line: str) -> str:
    """A SessionStart hook's plain stdout reaches only the agent. In this JSON
    `additionalContext` goes to the agent and `systemMessage` to the person's
    terminal. Codex shows `systemMessage` as a warning, so Codex gets none."""
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if api.author_default() != "codex":
        out["systemMessage"] = line
    return json.dumps(out)


def _unknown_keys_note(ctx) -> str | None:
    """The symbion.toml keys this symbion does not read, each with its near
    miss: `local_onyl = true` read as unset in silence (2026-10-25)."""
    if not ctx.cfg.unknown_keys:
        return None
    known = [*config.KNOWN_KEYS, "provenance.command"]
    said = [k + "".join(f" ({m}?)" for m in difflib.get_close_matches(k, known, 1))
            for k in ctx.cfg.unknown_keys]
    return (f"note: {ctx.store_dir / config.CONFIG_FILE} sets {', '.join(said)}, which this "
            f"symbion does not read; a misspelled key reads as unset")


def _hook_session() -> str | None:
    """The session id in the hook input Claude Code writes on stdin. A
    terminal, an empty stdin or another shape has none."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "null")
    except (ValueError, OSError, AttributeError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) else None


def _session_cache() -> Path:
    """Per-session state: the gotcha lists and what each session has seen.
    Session start sweeps out what is a week old."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "symbion" / "gotchas"


def _list_gotchas(ctx, sid: str, deadline: float) -> None:
    """The list gotchas.sh reads on each tool call: per catalog name a
    #gotcha row is on or refers to, the row's id and the line the agent gets
    when a call names it. A trap on a file reached a session only through
    `context --target`, which four sessions skipped (2026-10-02 to 10-10).
    Written at session start, so a compacted session gets each line again.
    # ponytail: a gotcha written mid-session reaches other sessions at their
    # next start; rebuild in the hook when notes.jsonl is newer if that bites."""
    d = _session_cache()
    d.mkdir(parents=True, exist_ok=True)
    week = time.time() - 7 * 86400
    for f in d.iterdir():                  # one per session: keep a week's
        # Another session's sweep may take a file first; it costs that file only.
        with contextlib.suppress(FileNotFoundError):
            if f.stat().st_mtime < week:
                f.unlink(missing_ok=True)
    for spent in ("seen", "edit"):              # what gotchas.sh has printed
        (d / f"{sid}.{spent}").unlink(missing_ok=True)
    lines = []
    side = store.read_side(ctx.store_dir)
    loaded = summ.seen_from(store.load(ctx.store_dir), side)
    by_id = {n.id: n for n in loaded}
    rows = [n for n in store.heads(loaded) if _hook_targets(n)]
    # A gotcha has no status, so a stale one read as true at every touch (the
    # user, 2026-10-14). A count cannot say stale: every file gotcha in one
    # store had changed since, by one commit to hundreds. The agent, in the file at
    # that moment, judges; the line gives it the age, the churn and the retire.
    left = min(HOOK_HISTORY_TIMEOUT, deadline - time.monotonic())
    since = gitref.commits_since(ctx.cfg, rows, timeout=left) if left > 0 else {}
    for n in rows:
        tail = n.id[-10:]
        sym = " ".join(filter(None, ["symbion", _named_store(ctx)]))
        at = n.first_written or n.created_at
        # The line is cached for the session: `6m ago` still read so 45 minutes on.
        age = f"written {store.shown(at) if summ.age_days(at) == 0 else summ.when_added(at)}"
        if _other_side(by_id, n, side):
            age += f" by {summ._root(by_id, n).author}"
        if since.get(n.id):
            age += f"; the file has changed in {summ._count(since[n.id], 'commit')} since"
        body = summ.clip(n.body or "", summ.GOTCHA_CHARS)
        more = f"`{sym} show {tail}` prints all of it; " if _gotcha_len(n) > summ.GOTCHA_CHARS \
            else ""
        text = (f"symbion entry {tail}, tagged gotcha, is on {n.target.type}"
                f"{':' + n.target.name if n.target.name else ''} ({age}): {body} "
                f"({more}if it no longer holds: `{sym} supersede {tail} --add-tag retired`)")
        for t in _hook_targets(n):
            # The root's dir name: gotchas.sh matches `<root>/<name>`, so a
            # row on README.md does not fire on src/README.md.
            root = ctx.cfg.for_type(t.type).work_root
            leaf = Path(root).name if root and not re.search(_UNSAFE, Path(root).name) else ""
            lines.append(f"{n.id}\t{t.name}\t{json.dumps(text, ensure_ascii=False)[1:-1]}"
                         f"\t{leaf}\n")
    store._replace_atomically(d / sid, "".join(lines))


_UNSAFE = r"[\t\n\r\"\\]"           # what a field of gotchas.sh's list cannot hold


def _hook_targets(n) -> list:
    """The objects gotchas.sh matches a tool call against for this row: a
    live #gotcha's target and refs of a catalog type. An item or project
    gotcha has none, and the hook never prints it."""
    if "gotcha" not in n.tags or "retired" in n.tags or n.status == "resolved":
        return []
    return [t for t in (n.target, *n.refs)
            if t.type not in store.BUILTIN_TARGET_TYPES and t.name and len(t.name) >= 4
            and not re.search(_UNSAFE, t.name)]


def _gotcha_len(n, lead=False) -> int:
    """A body's length as the hook line measures it: flattened, emphasis
    dropped. `lead`: its first paragraph only, where the trap and its fix go."""
    s = n.body or ""
    if lead:
        s = re.split(r"\n\s*\n", s.strip(), maxsplit=1)[0]
    return len(summ.clip(s, len(s) + 1))


def _echo_gotcha_notes(written, input_types) -> None:
    """A live gotcha with no name the hook can match is never printed, and
    a store held several on an item or the project, written in silence
    (2026-10-14): say so, with a ref type this store has. The hook gives a
    session a gotcha's first GOTCHA_CHARS, and every gotcha written in its
    first days ran longer, the median three times as long (2026-10-14). The
    trap and its fix go in a first paragraph that fits; evidence may follow
    below it (the user, 2026-10-14). Show the writer the cut whenever a write
    leaves a longer first paragraph."""
    hookable = sorted(set(input_types) - store.BUILTIN_TARGET_TYPES)
    ref = ("`--ref file:PATH`" if "file" in hookable else
           f"`--ref TYPE:NAME`, a type of this store's catalogs ({', '.join(hookable)}),")
    for n in written:
        if "gotcha" in n.tags and "retired" not in n.tags and n.status != "resolved" \
                and not _hook_targets(n):
            print(f"note: {n.id} is tagged gotcha, but the hook never prints it: it prints an "
                  "entry when a tool call names the entry's file, and this entry has none it "
                  f"can match; {ref} gives it one", file=sys.stderr)
    long = [n for n in written if _hook_targets(n) and _gotcha_len(n, lead=True) > summ.GOTCHA_CHARS]
    if not long:
        return
    print(f"note: the gotcha hook shows a session an entry's first {summ.GOTCHA_CHARS} chars; "
          "put the trap and its fix in a first paragraph that fits, evidence below it:",
          file=sys.stderr)
    for n in long:
        print(f"  {n.id}  first paragraph {_gotcha_len(n, lead=True)} chars: "
              f"{summ.clip(n.body, summ.GOTCHA_CHARS)}", file=sys.stderr)


_TEXT_CHARS = 500


def _name_long_text(sized) -> None:
    """`sized`: (id, characters) for each entry this write gave text. SKILL.md
    asks each write to an entry, a new one or an addition, for about 500
    characters, since every later session reads it whole; per write, not per
    entry (the user, 2026-10-14: "each addition should strive for that; some
    accumulation over time is inevitable"). After SKILL.md said so, agents'
    writes still often ran past it (2026-10-16). Say so at the write, as a
    long gotcha's cut is."""
    long = [(i, c) for i, c in sized if c > _TEXT_CHARS]
    if not long:
        return
    if len(sized) == 1:
        what = f"this write's text runs {long[0][1]} chars"
    else:
        more = f", +{len(long) - 5} more" if len(long) > 5 else ""
        what = (f"{len(long)} of {len(sized)} entries run long: "
                + ", ".join(f"{i[-10:]} ({c} chars)" for i, c in long[:5]) + more)
    print(f"note: {what}; an entry, or an addition to one, is about {_TEXT_CHARS}: the claim, "
          f"what showed it, what to do, with detail in a file under the store's docs/ "
          f"(--ref doc:NAME.md)", file=sys.stderr)


def _text_added(old: str, new: str) -> int:
    """Characters a rewrite adds, by words: counted whole, a 60-character fix
    inside a long body read as 1256 (2026-10-16)."""
    a, b = (re.findall(r"\S+\s*", t) for t in (old, new))
    return sum(len("".join(b[j1:j2])) for op, _, _, j1, j2
               in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
               if op in ("insert", "replace"))


def _one_stream() -> bool:
    """stdout and stderr are one open file, as Claude Code's shell gives them:
    a line copied to stderr there printed twice. Windows and OpenBSD give a
    pipe no file id (0), so two pipes would read as one: there, never."""
    try:
        a, b = os.fstat(sys.stdout.fileno()), os.fstat(sys.stderr.fileno())
    except (AttributeError, OSError, ValueError):     # a stream with no file (a test's)
        return False
    return bool(a.st_ino) and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _gui(store_dir):
    """The URL of a serve on this store, for the ids a terminal prints; None
    in a pipe, whose line never changes."""
    if not sys.stdout.isatty():
        return None
    from .gui import servers
    r = servers.serving(store_dir)
    return r and r["url"]


def _serve_all(*, restart: bool) -> int:
    """`serve --restart` and `serve --stop`: one line per serve, exit 1 if
    any did not."""
    if os.name == "nt":
        print("error: serve --restart and --stop need POSIX signals; on Windows, "
              "Ctrl-C each serve and start it again", file=sys.stderr)
        return 1
    from .gui import servers
    results = servers.signal_all(restart)
    if not results:
        print("no symbion serve is running")
    for what, r in results:
        if what == "restarted":
            print(f"restarted {r['store']}: {r['url']}"
                  + (f" at {r['build']}" if r.get("build") else ""))
        elif what == "stopped":
            print(f"stopped {r['store']}: {r['url']}")
        elif what == "exited":
            print(f"{r['store']}: pid {r['pid']} exited instead of restarting; its terminal "
                  "says why. A serve started before `serve --restart` existed stops on it: "
                  "start that one again there.")
        else:
            print(f"{r['store']}: pid {r['pid']} did not {'restart' if restart else 'stop'} "
                  "within 30 s")
    ok = sum(what in ("restarted", "stopped") for what, _ in results)
    if results:
        # Last, so output cut by a pipe shows the cut: `| tail -5` hid a
        # serve, which went unprobed (2026-10-11).
        print(f"{'restarted' if restart else 'stopped'} {ok} of "
              f"{summ._count(len(results), 'serve')}")
    return int(ok < len(results))


def _other_side(by_id, n, side) -> str | None:
    """The side n belongs to when it is not the reader's `side`."""
    return None if summ.on_side(by_id, n, side) else summ.side_of(by_id, n)


def _print_note(n, *, state=None, subject=None, full=True, head=None, gui=None,
                since=None, added=None, seams=(), links=(), written=None,
                successor=None, side=None) -> None:
    """`state` is api.verdict_state's pair for a verdict row, computed by the
    caller (batched per-listing where it needs a subject lookup); `subject`
    is the commit's subject line for a commit-target note, degrading to the
    bare sha when the caller has none. `full=False` clips the body to one
    line. No created_at prefix: the id IS the timestamp, and the row used to
    print the same fact twice. `head` is the head of a superseded row's
    chain: without it an old row's `[open]` read as live (2026-09-23).
    `since` is gitref.commits_since's count for the row, printed above 0.
    `added` is summary.added's pair, printed below a clipped body.
    `seams` are summary.seams' for a full body: a line at each names who
    added the text beside it, and when. `links` are summary.links' lines,
    printed below the body. `written` is summary.written's parts.
    `successor` is gitref.successor's for a `rewritten` state. `side` is the
    side of a shared store's row the reader's side did not raise: the line's
    last field."""
    # A superseded row's own status is history: printed first, `[open]`
    # read as live beside a resolved head (2026-09-28). The head's decides.
    status = store.read_status(head if head is not None else n)
    # The due words only while it can still fire: a resolved row is never
    # late, and a superseded one's state and due phrase are its head's to say:
    # beside them it read as a second open prediction (2026-10-14).
    due = store.due_state(n.due) if n.due and status == "open" and head is None else None
    if sys.stdout.isatty():
        # Imported here, not at the top: a pipe (every agent read, the
        # session-start hook) never pays the ~50 ms of loading rich.
        from . import term
        term.print_note(n, status=status, state=state, due=due, subject=subject,
                        head=head, full=full, gui=gui, since=since, seams=seams,
                        links=links, written=written, successor=successor, side=side)
        return
    tgt = n.target.name or ""          # `project:`, the form --target takes
    if n.target.type == "commit" and subject:
        tgt = n.target.name[:7] + " " + (subject[:60] + "…" if len(subject) > 60 else subject)
    # The head's status after its id, not in the row's own brackets.
    extra = (f" superseded -> {head.id}" + (f" ({status})" if status else "")
             if head is not None else f" [{status}]" if status else "")
    if n.spec.verdict and head is None:
        if state is not None:
            st, dist = state
            if st == "unverifiable":
                st += f" ({api.why_unverifiable(n.provenance)})"
            elif st == "external":
                st += f" ({summ.age_phrase(n.provenance['at'])})"
            elif st == "unstamped":
                st += f" ({summ.age_phrase(n.created_at)})"
            extra += f" state={st}" + (f" distance={dist}" if dist is not None else "")
            if st == "rewritten" and successor is not None:
                # Not `successor_state=`: a grep for `state=current` matched it.
                extra += (f" successor={successor.sha[:7]} successor_reads={successor.state}"
                          + ("" if successor.same_tree is None
                             else f" same_tree={'yes' if successor.same_tree else 'no'}"))
    if since:
        extra += f" commits_since={since}"
    if n.due:
        extra += f" due={store.shown(n.due)}" + (f" ({summ.due_phrase(due)})" if due else "")
    if n.refs:
        extra += " refs=" + ",".join(summ.ref_label(r) for r in n.refs)
    if n.tags:
        extra += " " + " ".join(f"#{t}" for t in n.tags)
    print(f"{n.kind:8} {n.target.type}:{tgt}{extra}  {n.id}"
          + (f" side={shlex.quote(side)}" if side else ""))
    if full:
        # A row in full says when, by whom, and its earlier version: the id is a
        # timestamp no human reads as one (newcomer walk-through, 2026-09-26).
        print("    " + "; ".join(written or [f"written {store.shown(n.created_at)} by {n.author}"])
              + (f"; earlier version saved as {n.supersedes}" if n.supersedes else ""))
    if n.spec.verdict:
        # Labelled lines, not reprs on the head line; the provenance dict
        # was printed raw there too (the v1 CLI's default).
        text = summ.flatten if full else (lambda v: summ.clip(v, summ.LIST_BODY_CHARS))
        sha = store.stamp_sha(n.provenance)
        at = f" at {sha[:7]}" if sha else ""
        if summ.pending(n, state):
            print(f"    to check, registered{at}: {text(n.checked)}")
        elif n.checked is not None or at:
            print(f"    checked{at}: {text(n.checked)}")
        if n.result is not None:
            print(f"    result: {text(n.result)}")
    if n.measurements:
        m = summ.measured(n.measurements)
        print(f"    measured: {m if full else summ.clip(m, summ.LIST_BODY_CHARS)}")
    if n.options:
        o = summ.options_line(n.options)
        print(f"    options: {o if full else summ.clip(o, summ.LIST_BODY_CHARS)}")
    if n.body and full:
        # A body is markdown by contract: printed as written here, so an
        # agent reads exactly what was stored, and symbion.term renders it.
        # Every line takes the row's indent: a second paragraph at column 0
        # read as outside the row to three fresh agents (2026-10-11).
        # `--json` is the verbatim form to copy from.
        # A line at each seam says who added what: a user's appended reply
        # read as the agent's own text, which then signed paragraphs by hand
        # (2026-10-10).
        for piece, line in summ.at_seams(n.body, seams):
            print(textwrap.indent(piece, "    "))
            if line:
                print(f"    {line}")
    elif n.body:
        print(f"    {summ.clip(n.body, summ.LIST_BODY_CHARS)}")
    for line in links:
        print(f"    {line}")
    for a in summ.added_lines(added) if added and not full else ():
        print(f"    {summ.added_label(a)} {a['added']}")


# ---- add: rows in, one write out ----
_ROW_KEY_ORDER = ("kind", "target", "body", "status", "checked", "result",
                  "measurements", "options", "external", "arc_id", "due", "tags", "refs",
                  "author")
_ROW_KEYS = set(_ROW_KEY_ORDER)     # `add -h` prints the order: a copy lost `due`


def _refs_from_flags(refs) -> list[dict]:
    """`TYPE:NAME` strings -> the row shape. Shared by `add` (through
    _row_from_flags) and `supersede`, so both spell a ref the same way."""
    out = []
    for r in refs:
        rt, _, rn = r.partition(":")
        out.append({"type": rt, "name": rn or None})
    return out


def _resolve_body(args) -> None:
    """Collapse --body-file into args.body, once, before anything reads it.

One flag covers the file and the pipe: '-' is stdin, as it is for
    --from-json. Resolving INTO args.body is what keeps every downstream reader
    -- _row_from_flags, supersede's field build, and --from-json's "no other
    note flags" guard -- looking at exactly one attribute.

    argparse.FileType would do the '-' handling, but it is deprecated as of
    3.14 ("open files after parsing") and opens eagerly at parse time, which
    would create the file handle even for a run that errors before using it."""
    if getattr(args, "body_file", None) is None:   # list, resolve, tags, ...
        return
    flag = getattr(args, "body_flag", "--body-file")
    try:
        args.body = (sys.stdin.read() if args.body_file == "-"
                     else Path(args.body_file).read_text(encoding="utf-8"))
    except OSError as e:
        # SystemExit(str) so main() prints it unprefixed and exits 1, the same
        # shape as every other bad-input refusal here. Uncaught, an OSError
        # reaches no handler in main() and the user gets a traceback.
        raise SystemExit(f"{flag}: {e}")
    if not args.body.strip():
        # A producer that failed upstream of a pipe with no pipefail: supersede
        # replaced a long body with "" at exit 0 (2026-10-03). An addition
        # (--append, --prepend) cannot blank one, so it gets no such hint.
        adds = getattr(args, "append", False) or getattr(args, "prepend", False)
        hint = ("; to blank the body on purpose, pass --body ''"
                if args.cmd == "supersede" and not adds else "")
        raise SystemExit(f"{flag} {args.body_file}: read nothing but whitespace, so "
                         f"nothing was written; check the file or the command feeding it{hint}")


def _body_fields(args, append=False) -> dict:
    """--body/--body-file as supersede's keyword: `body` replaces it, and
    under --append, `append_body` goes after the chain tip's body, which
    store reads under the lock -- so an amendment cannot rewrite the text
    it amends. resolve always appends: replacing lost the claim the row was
    about in 111 of 149 adopter resolves (2026-09-26 audit)."""
    append = append or args.append
    prepend = getattr(args, "prepend", False)
    if args.append and prepend:     # --append-file beside --prepend, past argparse's groups
        raise SystemExit("--append and --prepend: pick one")
    if args.body is None:
        if args.append or prepend:
            flag = "--append" if args.append else "--prepend"
            raise SystemExit(f"{flag} needs --body or --body-file: the text to add")
        return {}
    if prepend:
        return {"prepend_body": args.body}
    return {"append_body" if append else "body": args.body}


class _ModeFile(argparse.Action):
    """`--append-file PATH` is `--append --body-file PATH`, and `--prepend-file`
    likewise: the twins agents guess from --body-file. A hint naming the form
    that works went to stderr, which an agent dropped (2026-10-14, a demo
    session that guessed it)."""

    def __call__(self, parser, ns, values, option_string=None):
        ns.body_file, ns.body_flag = values, option_string     # a refusal names it as typed
        setattr(ns, self.const, True)


_MEASURE_MERGE_HELP = ("set a measurement (repeatable), as add --measure takes it: a "
                       "name given replaces that name, and the others are inherited")


def _measures_from_flags(flags) -> dict | None:
    """`--measure KEY=VALUE`s as measurements: an int, else a float, else the
    string; a value in double quotes is the string inside them.
    api.check_measurements judges each value; a repeated name is refused
    here, where the second would silently replace the first.

    A number is the default even when it does not print back as typed:
    `python=3.10` stores 3.1 and `zip=02134` 2134, but text would break the
    comparison a measurement exists for (`"1.50" > 2` is true in jq), and
    `secs=1.50` is what `printf %.2f` prints. So a note names the quoted
    form instead, at the write (2026-10-07)."""
    out = {}
    for f in flags or ():
        k, eq, v = f.partition("=")
        k, v = k.strip(), v.strip()         # `n = 5` stored the name `'n '`
        if not eq or not k:
            raise SystemExit(f"--measure {f!r}: write KEY=VALUE, e.g. --measure passed=412")
        if k in out:
            raise SystemExit(f"--measure {k!r} is given twice; give each name once")
        if len(v) >= 2 and v[0] == v[-1] == '"':
            out[k] = v[1:-1]
            continue
        for parse in (int, float):
            try:
                num = parse(v)
            except ValueError:
                continue
            if str(num) != v and math.isfinite(num):
                print(f"note: --measure {k}={v} is stored as the number {num}; to keep "
                      f"it as typed, write --measure '{k}=\"{v}\"'", file=sys.stderr)
            v = num
            break
        out[k] = v
    return out or None


def _options_from_flags(flags) -> list | None:
    """supersede's `--option TEXT`s: None when none was given, [] for
    `--option ""` alone (the clear), else the list as typed. `add` takes the
    list as typed, so its `--option ""` reads "option 1 is empty".
    api.check_options judges each; the fourth, an empty one among others and
    a repeat refuse there."""
    if not flags:
        return None
    return [] if list(flags) == [""] else list(flags)


def _row_from_flags(args) -> dict:
    """The flag path as a `list --json`-shaped row, so both paths build a
    note the same way. Only the flags actually given are present, which is
    also how `--from-json` tells whether any other note flag was passed."""
    refs = _refs_from_flags(args.refs)
    row = {"kind": args.kind, "target": {"type": args.type, "name": args.name},
           "body": args.body or None, "status": args.status, "checked": args.checked,
           "result": args.result, "measurements": _measures_from_flags(args.measures),
           "options": list(args.options) or None, "external": args.external,
           "arc_id": args.arc_id, "due": args.due,
           "tags": args.tags or None, "refs": refs or None, "author": args.author}
    row = {k: v for k, v in row.items() if v is not None}
    if row["target"] == {"type": None, "name": None}:
        del row["target"]
    return row


def _rows_from_json(path):
    """([(lineno, row)], [(lineno, error)]) from PATH or stdin: one JSON
    object per line, blank lines skipped. Keys outside the input shape are
    refused by name -- an `id`, `created_at` or `provenance` in the input
    would otherwise be silently discarded, and a typo'd key silently dropped."""
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as e:                # as --body-file: a refusal, not a traceback
        raise SystemExit(f"--from-json: {e}") from None
    rows, errors = [], []
    for i, line in enumerate(store.jsonl_lines(text), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append((i, f"line {i}: {e}"))
            continue
        if isinstance(row, list):
            errors.append((i, f"line {i} is a JSON array; it reads one JSON object per line"))
        elif not isinstance(row, dict):
            errors.append((i, f"line {i}: expected a JSON object"))
        elif bad := sorted(set(row) - _ROW_KEYS):
            errors.append((i, f"line {i}: unexpected key(s) {bad}; an entry takes only "
                              f"{', '.join(_ROW_KEY_ORDER)}. symbion mints id, created_at, "
                              f"provenance and target_blob itself"))
        else:
            rows.append((i, row))
    if not rows and not errors:
        # A producer that failed upstream of a pipe with no pipefail read as a
        # bootstrap that worked: exit 0, nothing written (2026-10-09).
        raise SystemExit(f"add: --from-json {path}: read no entries, so nothing was written; "
                         f"check the file or the command feeding it")
    return rows, errors


# ---- --dir extraction: resolved before subcommand parsing ----
def _extract_dir(argv):
    """Pull a `--dir VALUE` / `--dir=VALUE` pair out of argv wherever it sits,
    so the store resolves before the subcommand parser ever runs -- the
    reference's argparse only accepted a leading global flag, and its docs
    had to warn agents never to place --dir after the subcommand."""
    argv = list(argv)
    value = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dir":
            if i + 1 >= len(argv):
                raise SystemExit("--dir requires a value")
            value = argv[i + 1]
            i += 2
            continue
        if a.startswith("--dir="):
            value = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    # The shell expands `--dir ~/x`, never `--dir=~/x`.
    return value and os.path.expanduser(value), rest


# ---- argparse ----
def _version(here: Path = Path(__file__).parent) -> str:
    """pip's `NAME VERSION from PATH`, and in a checkout the commit it runs."""
    line = f"symbion {metadata.version('symbion')} from {here}"
    if sha := gitref.own_commit(here):
        line += f" (git {sha})"
    return line


def _metavar_once(formatter):
    """`formatter`, printing an option's metavar once after its last alias
    (`-m, --message MESSAGE`), as argparse does from Python 3.13. Older ones
    print it per alias, while the terminal's rich_argparse prints it once on
    every version: without this, --help at a terminal was not the pipe's text
    on 3.11 and 3.12."""
    if sys.version_info >= (3, 13):
        return formatter

    class Once(formatter):
        def _format_action_invocation(self, action):
            if not action.option_strings or action.nargs == 0:
                return super()._format_action_invocation(action)
            default = self._get_default_metavar_for_optional(action)
            return ", ".join(action.option_strings) + " " + self._format_args(action, default)
    return Once


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, formatter_class=argparse.HelpFormatter, **kwargs):
        super().__init__(*args, formatter_class=_metavar_once(formatter_class), **kwargs)

    def format_help(self):
        return self._at_terminal(super().format_help)

    def format_usage(self):
        return self._at_terminal(super().format_usage)

    def _at_terminal(self, fmt) -> str:
        """--help and a usage error in the palette at a terminal, through
        term.help_formatter. Chosen at print time, not when the parser is
        built, so that a pipe and every command that prints no help never
        load rich."""
        if not sys.stdout.isatty():
            return fmt()
        from . import term
        plain, self.formatter_class = self.formatter_class, term.help_formatter(self.formatter_class)
        try:
            return fmt()
        finally:
            self.formatter_class = plain

    def error(self, message):
        """argparse's "expected one argument" names the flag, not what it
        takes; a bare `--type` is how a human asks what the types are.
        A bare verb (`symbion`, `symbion arc`) gets its help: the missing
        argument's name is the parser's own field (`cmd`, `acmd`)."""
        if re.fullmatch(r"the following arguments are required: a?cmd", message):
            # `symbion --init`: a verb typed as a flag got 60 lines of help,
            # none naming it (an adoption report, 2026-10-13).
            verbs = next((a.choices for a in self._actions
                          if isinstance(a, argparse._SubParsersAction)), {})
            if verb := next((x[2:] for x in getattr(self, "_argv", ())
                             if x.startswith("--") and x[2:] in verbs), None):
                self.exit(2, f"{self.prog}: error: --{verb} is no flag; did you mean "
                             f"`{self.prog} {verb}`?\n")
            self.print_help(sys.stderr)
            self.exit(2)
        bad = re.match(r"argument cmd: invalid choice: '([^']*)'", message)
        if bad and bad[1] in _VERB_HINTS:        # the usage line lists the verbs
            message = f"no verb {bad[1]!r}: {_VERB_HINTS[bad[1]]}"
        stray = re.match(r"argument KIND: invalid kind '([^']*)'", message)
        if stray and (pair := self._measure_hint([stray[1]])):
            message = f"{stray[1]!r} was read as KIND: {pair}"
        elif stray and " " in stray[1]:
            # Quoted text with no flag before it: its quotes are there, its
            # --body is not, and then the kind is missing too (2026-10-09).
            message = f"{stray[1]!r} was read as KIND: text goes in --body \"…\""
            if "--kind" not in self._argv:
                message += "; a kind is required: `add KIND`"
        elif stray and self._argv[:1] != [stray[1]]:
            # Not the word after `add`: the next word of an unquoted value
            # (`--ref item:a b`) filled the optional KIND, and the list of
            # kinds sent its author to the wrong place (dogfood, 2026-09-29).
            message = (f"{stray[1]!r} was read as KIND because it stands alone: "
                       'a value with spaces needs quotes: --target "item:a b"')
        bare = re.fullmatch(r"argument (\S+): expected one argument", message)
        if bare:
            action = self._option_string_actions.get(bare[1].split("/")[0])
            legal = action and (action.choices or getattr(action.type, "choices", None))
            if legal:
                message += f" (choose from {', '.join(legal)})"
        need = re.fullmatch(r"the following arguments are required: (--[\w-]+)", message)
        if need and not self._get_positional_actions():
            # `arc create "Adoption"` said only that --name is required, and
            # not where its word went (dogfood, 2026-10-09).
            words = " ".join(x for i, x in enumerate(self._argv) if not x.startswith("-")
                             and not (i and self._argv[i - 1].startswith("-")))
            if words:
                message += f'; {words!r} went unused: it goes in {need[1]} "{words}"'
        super().error(message)

    def parse_known_args(self, args=None, namespace=None):
        """A verb's leftover arguments reached the top-level parser, which
        reported them under a usage that lists no flag of that verb. A leaf
        parser owns everything after its name, so it reports them itself."""
        self._argv = list(args or [])            # error() reads where KIND sat
        ns, extras = super().parse_known_args(args, namespace)
        if extras and self._subparsers is None:
            # The first guess this verb takes: `list --text` said `use --body`,
            # which `list` lacks, and the next try failed too (dogfood, 2026-10-14).
            hints = []
            for x in extras:
                h = next((h for h in _FLAG_HINTS.get(x, ())
                          if h.split()[0] in self._option_string_actions), None)
                if h:
                    hints.append(f"{x}: use {h}")
            # A word right after an unknown flag is that flag's value: `-m x`
            # read as an unquoted value that split (dogfood, 2026-10-09).
            bare = [x for i, x in enumerate(extras) if not x.startswith("-")
                    and not (i and extras[i - 1].startswith("-"))]
            if pairs := self._measure_hint(bare):
                hints.append(pairs)
                bare = [x for x in bare if not _PAIR.match(x)]
            if bare and "--body" in self._option_string_actions:
                # A quoted string arrives as one token with its spaces; several
                # space-free words are an unquoted value that split. Under
                # supersede's --append or --prepend, modes read as taking the
                # text (fresh agents, 2026-10-14), they are that text.
                mode = next((f"--{m} " for m in ("append", "prepend") if getattr(ns, m, False)), "")
                # The flag whose value came before the stray word, where a
                # verb has both: `supersede --ref item:login page`.
                at = self._argv.index(bare[0])
                took = self._argv[at - 2] if at >= 2 else None
                flag = took if took in ("--target", "--ref", "--add-ref") else \
                    next((f for f in ("--target", "--ref") if f in self._option_string_actions), None)
                # A body that came is not the stray words (dogfood, 2026-10-15).
                came = getattr(ns, "body", None) or getattr(ns, "body_file", None)
                if (came or not mode) and flag and not any(" " in x for x in bare):
                    hints.append(f'a value with spaces needs quotes: {flag} "item:a b"')
                if not came:
                    hints.append(f'text goes in {mode}--body "…"')
            # `context --status open` asked list's question of the one-object
            # view (dogfood, 2026-10-15): when every stray flag is list's, the
            # same words under `list --target` are the command.
            flags = {x for x in extras if x.startswith("-")}
            if flags and flags <= getattr(self, "_list_flags", set()):
                hints.append("context lists every entry on one object; `list --target "
                             f"{shlex.quote(ns.target or 'TYPE:NAME')} "
                             f"{' '.join(map(shlex.quote, extras))}` filters them")
            hints += self._add_gaps(ns)
            self.error(f"unrecognized arguments: {' '.join(extras)}"
                       + "".join(f"; {h}" for h in hints))
        return ns, extras

    def _add_gaps(self, ns):
        """What `add` would refuse on the next run, named in this one: the
        stray text, then the missing target, then the target's name took
        three runs to learn (dogfood, 2026-10-09)."""
        if self.prog != "symbion add" or ns.from_json:
            return []
        gaps = [] if ns.kind or ns.kind_pos else ["a kind is required: `add KIND`"]
        if not (ns.target or ns.type):
            return gaps + ["a target is required: --target TYPE:NAME"]
        typ, name = ns.target.partition(":")[::2] if ns.target else (ns.type, ns.name)
        try:
            api.check_name(typ, name or None)
        except ValueError as e:
            gaps.append(str(e))
        return gaps

    def _measure_hint(self, words):
        """`--measure a=1 b=2` leaves `b=2` over: a second pair, not an
        unquoted value that split (dogfood, 2026-10-08)."""
        pairs = [x for x in words if _PAIR.match(x)]
        if pairs and "--measure" in self._option_string_actions:
            return "each KEY=VALUE needs its own --measure: " + " ".join(
                f"--measure {shlex.quote(p)}" for p in pairs)
        return None


_PAIR = re.compile(r"[^\s=-][^\s=]*=")

# A first-day user's guesses (newcomer walk-through, 2026-09-26), each
# answered with the command that does it.
_APPEND_ONLY = ("entries are append-only: `resolve ID` closes a status entry, "
                "`supersede ID --add-tag retired` retires a plain one, and `undo ID` "
                "takes back your last write before a commit")
_VERB_HINTS = {
    **dict.fromkeys(("delete", "rm", "remove", "drop"), _APPEND_ONLY),
    **dict.fromkeys(("edit", "update", "amend", "correct"),
                    "`supersede ID --body …` records a correction (--append adds to it)"),
    "append": "`supersede ID --append --body …` adds to an entry's body",
    **dict.fromkeys(("close", "done", "finish", "complete"), "`resolve ID`"),
    **dict.fromkeys(("search", "find", "grep"), "`list --grep TEXT`"),
    **dict.fromkeys(("history", "log"), "`show ID --all` prints that version and names its chain's head"),
    **dict.fromkeys(("new", "create", "note"), "`add KIND --target TYPE:NAME --body …`"),
    **dict.fromkeys(("status", "todo", "open"), "`summary`, or `list --status open`"),
    "ls": "`list`",
    "due": "`list --overdue` (past due), `list --due-by DATE`, or `summary` (due within 7 days)",
    "recent": "`list --since 2h`",
}
_FLAG_HINTS = {"--open": ("--status open",), "--closed": ("--status resolved",),
               "--resolved": ("--status resolved",), "--search": ("--grep",),
               "--message": ("--body", "--grep"), "-m": ("--body", "--grep"),
               "--text": ("--body", "--grep"), "--title": ("--body", "--grep"),
               "--body": ("--grep",), "-F": ("--body-file", '-m "$(cat FILE)"')}


def _choice(legal, what, where, store_dir):
    """argparse `type` for a value set the config extends. `choices=` can only
    say what is legal; a user who typed a name that is not declared needs to
    hear where one is declared. With the store absent, nothing past the
    built-ins is declared, so a miss passes to _dispatch, which names the
    store: `invalid type 'widget'` sent a client to the type (2026-10-03)."""
    legal = sorted(legal)

    def parse(v):
        if v in legal or not store.exists(store_dir):
            return v
        raise argparse.ArgumentTypeError(
            f"invalid {what} {v!r} (choose from {', '.join(legal)}); {where(v)}")
    parse.choices = legal       # for _Parser.error on a bare flag
    return parse


def _row_count(text: str) -> int:
    """`list --limit`: `-1` sliced the oldest row off the page (2026-10-09)."""
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not an entry count: give N, or 0 for all")
    return n


def _network(text: str):
    """`serve --allow`: an address is its own one-address network."""
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _catalog_choice(legal, what, store_dir, kinds=(), as_kind=None):
    """`kinds` and `as_kind` for a --type: a kind typed there (`--type bug`)
    is named as one, with the flag that takes it."""
    parse = _choice(legal, what, lambda v: config.uncomment_hint(store_dir, v) or
                    f"catalog {what}s are declared in {store_dir / 'symbion.toml'} "
                    f"under [catalogs]", store_dir)
    if not kinds:
        return parse

    def typed(v):
        if v not in legal and v in kinds:
            raise argparse.ArgumentTypeError(
                f"{v!r} is a kind: {as_kind.format(v)}" if as_kind else
                f"{v!r} is a kind, not a target type (`symbion schema` lists both)")
        return parse(v)
    typed.choices = parse.choices
    return typed


def _target_choice(legal, store_dir, kinds=()):
    """TYPE:NAME with TYPE checked: an unknown type can hold no notes, so a
    typo would otherwise read as a target with none. A target typed without
    its TYPE (`--target "login page"`) is shown the form."""
    check = _catalog_choice(legal, "type", store_dir)

    def parse(v):
        t, colon, _ = v.partition(":")
        if not colon and t not in legal:
            kind = f"; {v!r} is a kind: `add {v} --target TYPE:NAME`" if v in kinds else ""
            raise argparse.ArgumentTypeError(
                f"needs TYPE:NAME, e.g. --target {shlex.quote('item:' + v)}{kind} "
                f"(types: {', '.join(sorted(legal))})")
        if t not in legal and t in kinds:
            raise argparse.ArgumentTypeError(
                f"invalid type {t!r} (choose from {', '.join(sorted(legal))})"
                + api.kind_as_type(t, kinds, "target"))
        check(t)
        return v
    return parse


def _take_target(args, verb) -> bool:
    """--target TYPE:NAME into --type and --name; False when both forms came."""
    if args.target is None:
        return True
    if args.type or args.name:
        print(f"{verb}: --target or --type/--name, not both", file=sys.stderr)
        return False
    args.type, _, name = args.target.partition(":")
    args.name = name or None
    return True


def _since(v):
    """(as typed, cutoff): the header echoes what was typed. `wrap` has no
    cutoff until the store is known: the last wrap's commit sets it."""
    if v.strip() == "wrap":
        return v, None
    try:
        return v, store.since_cutoff(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _due_by(v):
    """(as typed, date) for `list --due-by`."""
    try:
        return v, date.fromisoformat(v.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a date: use YYYY-MM-DD") from None


def _kind_choice(kinds, store_dir):
    """A default the table lacks gets the line to paste: a table `init` wrote
    keeps the defaults of its day, so a default added later never reaches it."""
    toml = store_dir / "symbion.toml"

    def where(v):
        if v in K.DEFAULT_KINDS:
            line = K.render_toml({v: K.DEFAULT_KINDS[v]}).splitlines()[1]
            return f"{v!r} is a default kind: to use it here, add under [kinds] in {toml}: {line}"
        return f"kinds are declared in {toml} under [kinds]"
    return _choice(kinds, "kind", where, store_dir)


_EPILOG = """\
read:
  symbion summary                    what is open in this project
  symbion list --grep WORD           search every entry
  symbion show ID...                 entries by id; a unique tail of an id works
write:
  symbion add bug --target item:NAME --body "..."
  symbion resolve ID --body "what closed it"
  symbion commit                     an entry is not in the store's git until this
  symbion push                       nor off this disk until this

`symbion schema` lists this store's kinds and target types;
`symbion VERB -h` gives each verb's flags."""


def _build_parser(target_types, arc_scopes, seed_scopes, store_dir, kinds, input_types=None):
    # The types a write or a read takes: `file` too in a store with [repos].
    # rename, arc --scope and --to-type keep the stored types.
    input_types = input_types or target_types
    p = _Parser(prog="symbion", description="a per-project notebook and ticket registry",
                epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=None,
                   help="store dir (default: $SYMBION_DIR, else a .symbion file in the repo "
                        "or, outside one, at or above the cwd, else ../<repo>-notes)")
    # Listed here for -h; main() answers it before the store resolves.
    p.add_argument("-V", "--version", action="store_true",
                   help="print the version and the path it runs from")
    sub = p.add_subparsers(dest="cmd", required=True)

    init_says = ("create this project's store with a starter symbion.toml, and link the "
                 "selected agent's skill and session-start hook outside it: ~/.claude for "
                 "Claude Code, ~/.agents/skills and $CODEX_HOME (default ~/.codex) for "
                 "Codex, $HERMES_HOME/skills (default ~/.hermes/skills) and no hook for "
                 "Hermes. Existing "
                 "settings are preserved; a missing hook entry is printed for you to add, "
                 "or merged with --merge-hook. "
                 "Without --yes it lists each change and makes none, and exits 1 "
                 "if it has one to make")
    i = sub.add_parser("init", help=init_says, description=init_says)
    i.add_argument("--agent", choices=("claude", "codex", "hermes", "both"), default="claude",
                   help="agent integration to install; both is claude and codex "
                        "(default: claude)")
    i.add_argument("--yes", action="store_true", help="make the changes it lists")
    i.add_argument("--merge-hook", action="store_true",
                   help="add the session-start hook entry to the agent's settings file, "
                        "keeping the rest of it; works without --yes once the skill is linked")
    i.add_argument("--repoint", action="store_true",
                   help="make this project read the store --dir names, though it reads "
                        "another store that exists (writes .symbion)")

    a = sub.add_parser("add", help="add an entry of any kind, or many from --from-json")
    # `add KIND --target TYPE:NAME` is the same row as `--kind/--type/--name`:
    # the shape every guessed add took, and how
    # `context --target` already spells a target.
    a.add_argument("kind_pos", nargs="?", default=None, metavar="KIND",
                   type=_kind_choice(kinds, store_dir), help="the same as --kind")
    a.add_argument("--kind", default=None, type=_kind_choice(kinds, store_dir),
                   help="one of: " + ", ".join(kinds) + "; required unless --from-json")
    a.add_argument("--target", default=None, metavar="TYPE:NAME",
                   type=_target_choice(input_types, store_dir, kinds),
                   help="the same as --type TYPE --name NAME; `project` needs no name")
    a.add_argument("--type", dest="type", default=None, metavar="TYPE",
                   type=_catalog_choice(input_types, "type", store_dir, kinds,
                                        "`add {} --target TYPE:NAME`"),
                   help="one of: " + ", ".join(sorted(input_types)) + "; required unless --from-json")
    a.add_argument("--from-json", dest="from_json", default=None, metavar="PATH",
                   help="read entries from PATH ('-' for stdin): one JSON object per line, "
                        "not the array `list --json` prints, with only these keys: "
                        + ", ".join({"tags": "tags[]", "refs": "refs[]"}.get(k, k)
                                    for k in _ROW_KEY_ORDER)
                        + ". A target or ref is {type, name} or TYPE:NAME. Every entry is "
                          "validated before any is written; takes no other note flags")
    a.add_argument("--name", default=None, help="target name (omit for project)")
    ab = a.add_mutually_exclusive_group()
    ab.add_argument("--body", default="", help="the entry's text, markdown")
    ab.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin): safest for prose "
                         "with backticks, $ or several paragraphs, which a shell "
                         "rewrites inside a quoted --body")
    a.add_argument("--status", default=None, choices=sorted(store.STATUSES))
    a.add_argument("--checked", default=None, help="check: what was checked")
    a.add_argument("--result", default=None, help="check: verdict")
    a.add_argument("--measure", dest="measures", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="a number or a string under `measurements` (repeatable), which "
                        "list --json gives as an object, so entries compare: --measure passed=412. "
                        "A value in double quotes stays text: --measure 'python=\"3.10\"'")
    a.add_argument("--option", dest="options", action="append", default=[], metavar="TEXT",
                   help="question: an answer the user can pick (repeatable, up to three), "
                        "the default first; the body is the question in plain words. The "
                        "web page draws each as a button, so it is one line of at most 60 "
                        "characters")
    a.add_argument("--external", action="store_true", default=None,
                   help="verdict kinds: what was checked is outside this repo (DNS, a "
                        "host, a live db), so the entry is stamped with when it ran, not "
                        "HEAD, and lists its age instead of behind N or a dirty tree")
    a.add_argument("--arc-id", "--arc", dest="arc_id", default=None, metavar="ID",
                   help="file the entry under this arc, a step of its checklist")
    a.add_argument("--due", default=None, metavar="DATE",
                   help="YYYY-MM-DD or an ISO datetime, UTC unless TZ is set; status kinds "
                        "only. Past due, an "
                        "open entry prints first at session start and in list --overdue")
    a.add_argument("--tag", dest="tags", action="append", default=[], metavar="TAG",
                   help="repeatable")
    a.add_argument("--ref", dest="refs", action="append", default=[], metavar="TYPE:NAME",
                   help="secondary target this entry also implicates (repeatable), as TYPE:NAME")
    a.add_argument("--author", default=None, help=_AUTHOR_HELP)

    li = sub.add_parser("list", help="list entries, newest first; filter by kind, target, tag, "
                                     "arc or --grep text")
    li.add_argument("--id", dest="note_id", nargs="+", default=None, metavar="ID",
                    help="entries by id, one or more; a superseded id shows its chain's "
                         "head, and with --all that version. Exits 1 if one matches nothing")
    li.add_argument("--type", dest="type", default=None, metavar="TYPE",
                    type=_catalog_choice(input_types, "type", store_dir, kinds,
                                         "`list --kind {}`"),
                    help="one of: " + ", ".join(sorted(input_types)))
    li.add_argument("--name", default=None, help="target name, with --type")
    li.add_argument("--target", default=None, metavar="TYPE:NAME",
                    type=_target_choice(input_types, store_dir),
                    help="the same as --type TYPE --name NAME")
    li.add_argument("--kind", default=None, type=_kind_choice(kinds, store_dir),
                    help="only entries of this kind")
    li.add_argument("--status", default=None, choices=sorted(store.STATUSES),
                    help="only open, or only resolved, entries of a status kind")
    # The guess agents made most (2026-09-21..29). Out of -h, which teaches --status.
    for flag, status in (("--open", "open"), ("--resolved", "resolved"), ("--closed", "resolved")):
        li.add_argument(flag, dest="status", action="store_const", const=status,
                        help=argparse.SUPPRESS)
    li.add_argument("--tag", default=None, help="only entries carrying this tag")
    li.add_argument("--arc", "--arc-id", dest="arc_id", default=None, metavar="ID",
                    help="only entries in this arc, resolved included "
                         "(`arc todo` is the open list)")
    li.add_argument("--grep", default=None, metavar="PATTERN",
                    help="regex, case-insensitive, over body, target name, checked, "
                         "result, refs, #tags, name=value measurements and each option; ^ and $ "
                         "anchor a line; an id "
                         "or its tail, or several joined by |, also lists each entry")
    li.add_argument("-F", "--fixed-strings", action="store_true",
                    help="take --grep as a literal string, not a regex")
    li.add_argument("--overdue", action="store_true",
                    help="only open entries past their --due date")
    li.add_argument("--due-by", default=None, metavar="DATE", type=_due_by,
                    help="only entries due on or before DATE, past due included; "
                         "with --status open, what is still owed by then")
    li.add_argument("--since", default=None, metavar="WHEN", type=_since,
                    help="only entries written since WHEN: 30m, 2h, 3d, 1w, a date or an "
                         "ISO datetime, UTC unless TZ is set, or `wrap`, the last wrap's "
                         "commit (a revision counts as written when it was)")
    li.add_argument("--all", action="store_true", help="include superseded entries")
    li.add_argument("--author", metavar="NAME",
                    help="only entries whose newest version NAME wrote")
    li.add_argument("--side", metavar="NAME",
                    help="in a shared store, only entries raised on side NAME, a clone's "
                         "git user.name")
    li.add_argument("--limit", type=_row_count, default=None, metavar="N",
                    help=f"entries to show, newest first (text default {summ.LIST_CAP}; 0 for "
                         f"all). --json is whole unless this is given")
    li.add_argument("--full", action="store_true",
                    help="print bodies as stored instead of one clipped line (--id implies it)")
    li.add_argument("--json", action="store_true",
                    help="a JSON array of entries. `--limit 1` prints the keys every entry has; a "
                         "verdict kind adds `state` and `distance`, a `rewritten` one "
                         "`successor`, `successor_state` and `same_tree`, and a superseded entry "
                         "(`--all`) `head`")

    # Parsed only for -h: main() rewrites `show ID` to `list --id ID` first.
    # Listed so -h and the invalid-choice error name the verb, and with its
    # arguments so `show -h` documents them instead of erroring as `list --id -h`.
    sh = sub.add_parser("show", help="entries by id; `show ID... [--json] [--all]` runs `list --id ID...`",
                        description="entries by id: `list --id ID...`; a superseded id shows "
                                    "its chain's head, and --all that version")
    sh.add_argument("id", nargs="+")
    sh.add_argument("--json", action="store_true")
    sh.add_argument("--all", action="store_true", help="a superseded id's own version")
    sh.add_argument("--full", action="store_true", help="bodies whole, as show prints them anyway")

    rs = sub.add_parser("resolve", help="mark an open entry resolved",
                        description="mark an open entry resolved")
    rs.add_argument("id")
    rb = rs.add_mutually_exclusive_group()
    rb.add_argument("--body", default=None,
                    help="the answer (question) or the fate (idea), added after the "
                         "current body; to replace that, `supersede ID --status "
                         "resolved --body …`")
    rb.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin)")
    rs.add_argument("--append", action="store_true",
                    help="the default for resolve; accepted so older calls still run")
    rs.add_argument("--add-tag", dest="add_tags", action="append", default=[], metavar="TAG",
                    help="add a tag to the inherited set (repeatable): adopted, retired, …")
    # --add-ref too: supersede's word for adding one, typed here by habit
    # and refused (2026-10-15).
    rs.add_argument("--ref", "--add-ref", dest="refs", action="append", default=[],
                    metavar="TYPE:NAME",
                    help="add a ref, e.g. the commit that closed it (repeatable); the "
                         "inherited refs stay")
    rs.add_argument("--result", default=None, help="the verdict; required on a "
                    "status+verdict kind (a pre-registration)")
    rs.add_argument("--measure", dest="measures", action="append", default=[],
                    metavar="KEY=VALUE", help=_MEASURE_MERGE_HELP)
    rs.add_argument("--author", default=None, help=_AUTHOR_HELP)

    sp = sub.add_parser("supersede", help="record a correction to an entry")
    sp.add_argument("id")
    sb = sp.add_mutually_exclusive_group()
    sb.add_argument("--body", default=None,
                    help="the new body; with --append or --prepend, text added to it")
    sb.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin)")
    for mode in ("append", "prepend"):
        sb.add_argument(f"--{mode}-file", action=_ModeFile, const=mode, metavar="PATH",
                        help=f"--{mode} --body-file PATH")
    spa = sp.add_mutually_exclusive_group()
    spa.add_argument("--append", action="store_true",
                     help="add the --body/--body-file text after the current body, which "
                          "stays byte-identical, instead of replacing it")
    spa.add_argument("--prepend", action="store_true",
                     help="put the --body/--body-file text above the current body, as its "
                          "new lead; refused on a pre-registration")
    sp.add_argument("--status", default=None, choices=sorted(store.STATUSES),
                    help="set the status; `resolve ID` is the usual way to close")
    sp.add_argument("--kind", default=None, metavar="KIND",
                    help="move the entry to a kind with the same status and verdict bits; "
                         "--kind idea parks a task")
    sp.add_argument("--checked", default=None,
                    help="check: correct what was checked")
    sp.add_argument("--result", default=None,
                    help="check: correct the verdict")
    sp.add_argument("--measure", dest="measures", action="append", default=[],
                    metavar="KEY=VALUE", help=_MEASURE_MERGE_HELP)
    sp.add_argument("--option", dest="options", action="append", default=[], metavar="TEXT",
                    help="question: replace its answers (repeatable, up to three), the "
                         "default first; `--option \"\"` alone clears them")
    sp.add_argument("--external", action="store_true",
                    help="a supersede keeps the entry's stamp: accepted on an entry "
                         "add --external wrote, refused on any other")
    sp.add_argument("--tag", dest="tags", action="append", default=None, metavar="TAG",
                    help="replace the inherited tags (repeatable); omit to inherit")
    sp.add_argument("--add-tag", dest="add_tags", action="append", default=[], metavar="TAG",
                    help="add a tag to the inherited set (repeatable)")
    sp.add_argument("--rm-tag", dest="rm_tags", action="append", default=[], metavar="TAG",
                    help="remove a tag from the inherited set (repeatable)")
    sp.add_argument("--ref", dest="refs", action="append", default=None, metavar="TYPE:NAME",
                    help="replace the inherited refs (repeatable), refused if that drops "
                         "one unless '' comes first (`--ref '' --ref X`); '' alone clears "
                         "them; omit to inherit")
    sp.add_argument("--add-ref", dest="add_refs", action="append", default=[],
                    metavar="TYPE:NAME", help="add a ref to the inherited set (repeatable)")
    sp.add_argument("--target", default=None, metavar="TYPE:NAME",
                    help="move this entry to another object (`rename` moves every entry "
                         "on a name); omit to inherit")
    sp.add_argument("--arc-id", "--arc", dest="arc_id", default=None, metavar="ID",
                    help="file this entry under an arc ('' detaches it)")
    sp.add_argument("--due", default=None, metavar="DATE",
                    help="set the due date: YYYY-MM-DD or an ISO datetime ('' clears it)")
    sp.add_argument("--author", default=None, help=_AUTHOR_HELP)

    cm = sub.add_parser("commit", help="commit your pending entries and every changed file")
    cm.add_argument("-m", "--message", default=None,
                    help="the commit message (default: update entries); - reads it from stdin")
    cm.add_argument("--wrap", action="store_true",
                    help="end a session: -m is the handoff, printed by the next session's "
                         "summary (one line, then a paragraph)")
    cm.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="list each pending entry (its id's tail, author, kind and lead) "
                         "and each other changed file; commit nothing")
    cm.add_argument("--all", dest="everyone", action="store_true",
                    help="take every pending entry: by default a commit takes the entries this "
                         "agent session wrote, or with no session, your author's")
    sub.add_parser("push", help="git push the store to its remote; commit first")
    sub.add_parser("pull", help="fetch a shared store's origin and merge it; pending entries "
                                "stay pending, each its session's")
    sub.add_parser("trust", help="show what pulls changed in symbion.toml and accept it; "
                                 "writes and its commands wait until then. The user's, at a "
                                 "terminal")
    ud = sub.add_parser("undo", help="take back the store's last write, if it was yours "
                        "and not yet committed",
                        description="take back the store's last write, one level: an add, "
                        "edit, resolve, star or batch, all of it. Refused unless ID is an entry "
                        "or arc that write touched, the write was yours (this agent session's, "
                        "or with no session your author's), and no commit ran since")
    ud.add_argument("id", help="an entry or arc id (or an entry id's tail) the last write touched")
    ud.add_argument("--author", default=None, help=_AUTHOR_HELP)

    rn = sub.add_parser("rename",
                        help="re-target all entries for a renamed object and re-point every --ref "
                             "to it (old -> new name); with no names, move every entry of --type "
                             "to --to-type, names as stored")
    rn.add_argument("old", nargs="?", help="the name as stored; a commit takes a short sha")
    rn.add_argument("new", nargs="?", help="the new name, resolved as add resolves a target")
    rn.add_argument("--type", required=True, metavar="TYPE",
                    help="a declared type, or one this store holds entries of (how entries leave "
                         "a type the store dropped): " + ", ".join(sorted(target_types)))
    rn.add_argument("--to-type", metavar="TYPE", default=None,
                    type=_catalog_choice(target_types, "type", store_dir, kinds),
                    help="move the object to this type too (default: --type); "
                         "project:NAME entries from 0.1.0 move with --type project --to-type item")
    rn.add_argument("--author", default=None, help=_AUTHOR_HELP)

    sv = sub.add_parser("serve", help="local web UI (requires: pip install 'symbion[gui]')")
    sv.add_argument("--port", type=int, default=None,
                    help="default: the store's own port, derived from its path, so a "
                         "link to it outlives a restart")
    sv.add_argument("--host", default="127.0.0.1", metavar="ADDR",
                    help="the address to listen on (default: 127.0.0.1, this machine "
                         "only; 0.0.0.0 is every address); any other needs --allow")
    sv.add_argument("--allow", action="append", default=[], metavar="CIDR",
                    type=_network,
                    help="an address or network that may connect, e.g. 192.168.1.0/24 "
                         "(repeatable); this machine always may. There is no login: "
                         "each writes as --author")
    sv.add_argument("--author", default=None,
                    help="identity stamped on GUI writes (default: $SYMBION_AUTHOR, "
                         "else git user.name; never an agent's name)")
    sv.add_argument("--no-browser", action="store_true", help="open no browser tab")
    sv.add_argument("--read-only", action="store_true",
                    help="a public demo: every control answers with a notice, and nothing "
                         "writes")
    sv.add_argument("--reload", action="store_true", help="dev: reload on .py changes")
    every = sv.add_mutually_exclusive_group()
    every.add_argument("--restart", action="store_true",
                       help="restart every serve you run on this machine, each in its own "
                            "terminal and on its own port, so an upgrade or a code change "
                            "reaches it; then exit. Needs no store")
    every.add_argument("--stop", action="store_true",
                       help="stop every serve you run on this machine, as Ctrl-C does; "
                            "then exit. Needs no store")

    sub.add_parser("tags", help="list tag vocabulary with counts (catches near-synonyms)")

    sm = sub.add_parser("summary", help="what is open: counts, due entries, arcs, open entries "
                                        "(the session-start text)")
    sm.add_argument("--full", action="store_true", help="lift the display caps")
    smgrp = sm.add_mutually_exclusive_group()
    smgrp.add_argument("--json", action="store_true",
                       help="the same as an object; `store` is null when no store exists")
    smgrp.add_argument("--hook", action="store_true",
                       help="the SessionStart hook's JSON: the text for the agent, "
                            "and one line for the person's terminal")

    sc = sub.add_parser("schema", help="this store's vocabulary: every kind with its bits "
                                       "and entry count, then the target types")
    sgrp = sc.add_mutually_exclusive_group()
    sgrp.add_argument("--json", action="store_true")
    sgrp.add_argument("--toml", action="store_true",
                      help="the kinds as a [kinds] table to paste into symbion.toml; a table "
                           "there replaces the defaults, so keep the ones you use")

    ctx = sub.add_parser("context", help="every entry on one object (--target), commit "
                                         "(--commit) or branch (--branch)")
    grp = ctx.add_mutually_exclusive_group()
    grp.add_argument("--target", default=None, metavar="TYPE:NAME",
                     type=_target_choice(input_types, store_dir, kinds),
                     help="the entries on this object, and the entries that --ref it")
    grp.add_argument("--commit", default=None, metavar="SHA",
                     help="the entries on this commit")
    grp.add_argument("--branch", default=None, metavar="REF",
                     help="the entries on each commit REF has that the base lacks")
    ctx.add_argument("--since", default=None, metavar="REF",
                     help="the base for --branch (default: default_branch in symbion.toml, "
                          "so --branch on that branch lists nothing)")
    ctx.add_argument("--json", action="store_true",
                     help="an object with the entries under `notes`")
    ctx._list_flags = set(li._option_string_actions)     # for its refusal's hint

    act = sub.add_parser(
        "arc", help="campaigns: a checklist of entries across objects",
        description="An arc is a checklist. `arc create --name NAME` prints its id; then "
                    "`add task --arc ID --target TYPE:NAME --body …` adds a step with a "
                    "body, or `arc seed ID --name STEP` adds bodiless ones. `resolve ID` "
                    "ticks a step; `arc todo ID` is what is left.")
    asub = act.add_subparsers(dest="acmd", required=True)

    says = "start an arc; prints its id"
    ac = asub.add_parser("create", help=says, description=says)
    ac.add_argument("--name", required=True, help="the arc's name, as the summary prints it")
    ac.add_argument("--desc", default="", help="what the arc is for; arc list prints it")
    ac.add_argument("--scope", default="mixed", metavar="SCOPE",
                    type=_catalog_choice(arc_scopes, "scope", store_dir),
                    help="what the arc's entries are about (default mixed): item, project, "
                         "or a catalog type, which `arc seed` and the web UI can sweep; "
                         "one of: " + ", ".join(sorted(arc_scopes)))
    ac.add_argument("--author", default=None, help=_AUTHOR_HELP)

    al = asub.add_parser("list", help="every live arc with its done/total")
    al.add_argument("--all", action="store_true", help="archived arcs too")
    al.add_argument("--json", action="store_true")

    ar = asub.add_parser("rename", help="give an arc a new name; its id stays")
    ar.add_argument("id")
    ar.add_argument("new_name")

    says = "take an arc off the summary; its open entries then list outside arcs"
    aa = asub.add_parser("archive", help=says, description=says)
    aa.add_argument("id")

    asd = asub.add_parser("seed", help="add one task per --name, or per entry of a "
                                       "configured catalog")
    asd.add_argument("id", nargs="?", default=None,
                     help="the arc to seed; omit with --dry-run to probe a catalog")
    asd.add_argument("--scope", default=None, metavar="SCOPE",
                     type=_catalog_choice(seed_scopes, "scope", store_dir),
                     help="one of: " + ", ".join(sorted(seed_scopes)))
    asd.add_argument("--name", dest="names", action="append", default=None,
                     help="explicit target(s); repeatable. Omit to sweep the scope's "
                          "catalog, which is a command configured under [catalogs] in "
                          "symbion.toml -- `item` has none and always needs --name")
    asd.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="print the resolved names and count; create nothing")
    asd.add_argument("--kind", default="task", help="the kind to mint (default task); "
                     "it needs the status bit and neither parked nor verdict")
    asd.add_argument("--author", default=None, help=_AUTHOR_HELP)

    atd = asub.add_parser(
        "todo",
        help="an arc's open items, one per line: kind, target, body, id")
    atd.add_argument("id")
    atd.add_argument("--json", action="store_true")

    rc = asub.add_parser(
        "reconcile",
        help="check each open entry's target against the live catalog; flag stale/renamed")
    rc.add_argument("id")
    rc.add_argument("--apply", action="store_true",
                     help="re-target renamed tasks onto the live name")
    rc.add_argument("--resolve-stale", dest="resolve_stale", action="store_true",
                     help="also close tasks with no live target and no rename evidence")
    rc.add_argument("--author", default=None, help=_AUTHOR_HELP)
    rc.add_argument("--json", action="store_true",
                    help="the report entries as a JSON array; --apply counts go to stderr")

    co = sub.add_parser("completion", help="print the shell code for TAB completion: "
                                           "eval \"$(symbion completion zsh)\" in ~/.zshrc")
    co.add_argument("shell", choices=("bash", "zsh", "fish"))

    return p


# ---- list: the header that keeps a page from reading as the whole ----
def _hidden(every, base, args, filt) -> list[str]:
    """What this view leaves out, each with the flag that shows it: the other
    statuses when one is filtered, and the superseded rows unless --all. A
    0 must never read as "nothing exists" when it means "nothing matches"."""
    out = []
    if args.status:
        for s in sorted(store.STATUSES - {args.status}):
            n = len(store.query(base, status=s, **filt))
            if n:
                out.append(f"+{n} {s} (--status {s})")
    if not args.all:
        n = (len(store.query(every, status=args.status, **filt))
             - len(store.query(base, status=args.status, **filt)))
        if n:
            out.append(f"+{n} superseded (--all)")
    else:
        # Echoed even at 0: a widening flag whose output is byte-identical
        # to the default reads as dropped.
        n = (len(store.query(every, status=args.status, **filt))
             - len(store.query(store.heads(every), status=args.status, **filt)))
        out.append(f"--all: {n} superseded included")
    return out


def _name_named_project_rows(ctx, row) -> None:
    """A 0.1.0 store can hold `project:NAME` rows, which the name rule now
    refuses, so a follow-up could not join them (2026-09-30). On that
    refusal, name the rows and the command that moves them to `item:NAME`."""
    for o in (row.get("target"), *(row.get("refs") or ())):
        name = isinstance(o, dict) and o.get("type") == "project" and o.get("name")
        n = isinstance(name, str) and len(store.heads_for(ctx.store_dir, "project", name))
        if n:
            q = shlex.quote(name)
            cmd = " ".join(filter(None, ["symbion", _named_store(ctx), "rename", q, q,
                                         "--type project --to-type item"]))
            print(f"note: this store holds {summ._count(n, 'entry')} on project:{name}, "
                  f"filed before this rule; `{cmd}` moves them to item:{name}",
                  file=sys.stderr)


def _named_store(ctx) -> str | None:
    """`--dir <store>` when the store is not the one the cwd's tree names,
    else None. A hub's session reads its satellite's store beside its own,
    and the two summaries were identical: nothing in either said which
    project it was about (2026-09-28). Spelled as the flag that reaches it,
    since a reader of the second summary must also write to it.
    `../NAME` when the store sits beside the cwd, as `.symbion` records it,
    else absolute: from a subdirectory, `../NAME` named nothing (2026-10-13)."""
    if ctx.tree_default is not None and ctx.store_dir == ctx.tree_default:
        return None
    rel = (f"../{ctx.store_dir.name}" if ctx.store_dir.parent == Path.cwd().resolve().parent
           else ctx.store_dir.as_posix())
    return f"--dir {shlex.quote(rel)}"


def _list_header(total: int, shown: int, hidden: list[str],
                 scanned: int = 0, filters: list[str] = ()) -> str:
    """With a filter, `N of M match <flags>`: a bare `0 rows` read as 0-of-0
    (2026-09-22). The flags are spelled as typed and
    shell-quoted, so the line pastes back."""
    if filters:
        head = f"{total} of {scanned} match " + " ".join(filters)
    elif scanned == 0:
        head = f"0 entries; {summ.FIRST_CONTACT}"       # the empty state names its verb
    else:
        head = summ._count(total, "entry")
    if shown < total:
        head += f", showing {shown} newest (--limit N, 0 for all)"
    return "; ".join([head, *hidden])


_NEIGHBOUR_CAP = 3
_AUTHOR_HELP = ("who writes it (default: $SYMBION_AUTHOR, else the agent whose shell "
                "runs this, else git user.name)")
_ERROR_CAP = 10


def _name_stale_bodies(store_dir, old: str, prefix: bool = False) -> None:
    """After a rename, the rows whose body still names the old name. An
    --amend moved a check and a resolve's ref to the new sha, and the resolve
    still read "Fixed in <old>." until a hand supersede (2026-10-02). A commit
    is matched by its 7-character prefix, the form a body cites (`prefix`).
    A name is matched as a word, as `list --grep` would: a substring named
    every row with an `a` in it after a rename of `a` (2026-10-05). And in
    its case: an item named in plain words named the rows that used those
    words capitalized, about something else, and none about the item
    (2026-10-22)."""
    word = lambda c: c.isalnum() or c == "_"            # noqa: E731, what \b sees
    pat = ((r"\b" if word(old[0]) else "") + re.escape(old)
           + (r"\b" if word(old[-1]) and not prefix else ""))
    if not prefix:
        pat = f"(?-i:{pat})"                           # so the printed grep agrees
    grep = re.compile(pat, re.I | re.M)                # list --grep's flags
    hits = store.newest_first(n for n in store.heads(store.load(store_dir))
                              if grep.search(n.body or ""))
    if hits:
        ids = ", ".join(n.id for n in hits[:_NEIGHBOUR_CAP])
        more = f", +{len(hits) - _NEIGHBOUR_CAP} more" if len(hits) > _NEIGHBOUR_CAP else ""
        print(f"note: {old} is still in the body of {summ._count(len(hits), 'entry')}: {ids}{more}; "
              f"supersede each to update it (list --grep {shlex.quote(pat)})", file=sys.stderr)


def _name_open_neighbours(store_dir, written) -> None:
    """After an add, name the rows already open on each new row's target.
    Revisions landed as new rows beside the head they revised (an open
    "cause unknown" beside the row that found it), and the stale head
    kept printing at every session start. `project` is every row's fallback
    target, so a line there would print on every add and say nothing.

    In an agent session a target's rows are listed at its first add there,
    and a later add prints only the count: on a busy file every add listed
    the same unrelated rows, and a note that repeats is a note the reader
    learns to skip (2026-10-15). A row someone else adds since reaches the
    writer as news (`_say_news`)."""
    new = {n.id for n in written}
    heads = store.heads(store.load(store_dir))
    seen = set()
    for n in written:
        t = n.target
        # On `project`, only the rows whose first line this one repeats: a
        # revision there re-uses its title (measured 2026-09-24: a second
        # open box, same bold first line, 27 minutes after its head).
        title = _title(n.body) if t.type == "project" else None
        if (t.type == "project" and not title) or (t.type, t.name, title) in seen:
            continue
        seen.add((t.type, t.name, title))
        open_ = store.newest_first(h for h in store.query(heads, target_type=t.type,
                                                          target_name=t.name, status="open")
                                   if h.id not in new and not h.spec.parked
                                   and (title is None or _title(h.body) == title))
        if open_:
            where = shlex.quote(f"{t.type}:{t.name or ''}")
            which = " with this title" if title else ""
            if not _first_listing(store_dir, (t.type, t.name, title)):
                print(f"note: {len(open_)} open on {t.type}:{t.name or ''}{which}, listed at "
                      f"this session's first add there (context --target {where})",
                      file=sys.stderr)
                continue
            # Each with its lead: by id alone, a duplicate showed only after
            # a `show` (dogfood, 2026-10-07).
            rows = "".join(f"\n  {h.id}  {summ.clip(h.body or '', 80)}".rstrip()
                           for h in open_[:_NEIGHBOUR_CAP])
            more = (f"\n  +{len(open_) - _NEIGHBOUR_CAP} more"
                    if len(open_) > _NEIGHBOUR_CAP else "")
            print(f"note: {len(open_)} open on {t.type}:{t.name or ''}{which}; if this entry "
                  f"settles or revises one, resolve or supersede that one "
                  f"(context --target {where}):{rows}{more}", file=sys.stderr)


def _name_unmerged_fixes(cfg, store_dir, written) -> None:
    """After an add, name a row on its target resolved at a commit HEAD
    cannot reach. A fix on an unmerged branch read as done, and an agent on
    main that searched `--status open` filed the bug again (demo run,
    2026-10-10). It holds the other way too: a branch cut before a fix on
    main. Narrow on purpose: every resolved row would be noise."""
    new = {n.id for n in written}
    heads = store.heads(store.load(store_dir))
    done = [h for t in dict.fromkeys(n.target for n in written if n.target.type != "project")
            for h in store.query(heads, target_type=t.type, target_name=t.name,
                                 status="resolved") if h.id not in new]
    away = gitref.unmerged(cfg, [r.name for h in done for r in h.refs
                                 if r.type == "commit" and r.name])
    for h in done:
        sha = next((r.name for r in h.refs if r.type == "commit" and r.name in away), None)
        if sha:
            print(f"note: {h.id} on {h.target.type}:{h.target.name or ''} was resolved at "
                  f"{sha[:7]}, which {away[sha]} holds and this branch lacks: if this entry is "
                  f"the same fault, its fix is there\n  {summ.clip(h.body or '', 80)}".rstrip(),
                  file=sys.stderr)


_BOLD_LEAD = re.compile(r"\s*(\*\*|__)(.+?)\1")


def _title(body: str) -> str | None:
    """A body's title: its leading bold span, else its first sentence;
    flattened, lowercase. The measured revision had its bold title alone on
    a line while its head carried detail after the title on the same line,
    so the first line was the wrong unit. None under 20 characters, where
    `Done.` would match every other `Done.`.
    ponytail: exact match; a similarity ratio if a reworded title is measured
    slipping past."""
    m = _BOLD_LEAD.match(body or "")
    line = summ.flatten(m[2] if m else (body or "").split("\n", 1)[0])
    if not m:
        line = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    line = line.lower()
    return line if len(line) >= 20 else None


def _name_catalog_misses(cfg, fields, written) -> None:
    """After a batch, one line naming every target or ref its catalog does
    not list. Each miss prints its own note in line order among the rows
    that hit, and in a 23-row batch one typo scrolled past and minted a
    second head (2026-09-23). A type with a resolver mints new names on
    purpose, so it is left out, and so is a name typed as a path: its own
    note said where it went, and this one said `stored as typed`."""
    names = [t for f, n in zip(fields, written)
             for t, o in zip((n.target, *n.refs), (f["target"], *(f.get("refs") or ())))
             if t.type in cfg.catalogs and t.type not in cfg.resolvers and t.name
             and not (o["type"] == "file" and gitref.is_path(o.get("name")))]
    known = {typ: set(catalog.names(cfg, typ)) for typ in {t.type for t in names}}
    every = list(dict.fromkeys(f"{t.type}:{t.name}" for t in names))
    miss = list(dict.fromkeys(f"{t.type}:{t.name}" for t in names if t.name not in known[t.type]))
    if miss:
        more = f", +{len(miss) - 5} more" if len(miss) > 5 else ""
        print(f"note: {len(miss)} of {len(every)} names not in their catalog, stored as typed: "
              f"{', '.join(miss[:5])}{more}", file=sys.stderr)


# A cited row: an id or its start (a date, a dash, more digits, hex or a
# placeholder's x), or the 10-character tail SKILL.md says to cite, a
# placeholder's x again (`588877-xxx`, 2026-10-16). Not inside a longer
# token: a full id's own tail is not checked twice, and a stamp
# (`20990101-000000Z`) or a size (`50000000-byte`) is not an id.
_CITED = re.compile(r"(?<![\w-])(?:20\d{6}-[\dxa-f-]*[\dxa-f]|\d{6}-[\dxa-f]{3})(?![\w-])")
# An id or tail after one dash, `-a1b` or `-123456-a1b`: no flag has the shape.
_DASHED_ID = re.compile(r"-(?:\d+-)*[0-9a-f]{3}")


def _name_unknown_ids(store_dir, texts, where="the body") -> None:
    """Rows cited before they existed, as placeholders (`20990101-123xxx`, a
    full id of zeros), were found only by reading the row back. A cited row
    no id here starts with (or, for a tail, ends with) gets a note; never a
    refusal, since a row may cite another store's."""
    ids = [n.id for n in store.load(store_dir)]
    miss = [tok for tok in dict.fromkeys(t for text in texts for t in _CITED.findall(text or ""))
            # a date-time file stamp is not a cite (dogfood, 2026-10-09)
            if not re.fullmatch(r"20\d{6}-\d+", tok) and not _held(ids, tok)]
    if miss:
        # A row a sibling store holds is a cite of another project's: no note.
        there = [ids for _, ids in _sibling_ids(store_dir)]
        miss = [tok for tok in miss if not any(_held(other, tok) for other in there)]
    if miss:      # one line: a retro citing another store's rows printed four (2026-10-11)
        print(f"note: no entry in this store matches {', '.join(miss)} (cited in {where})",
              file=sys.stderr)


def _name_new_items(store_dir, written, before=()) -> None:
    """After a write, name each `--ref item:NAME` no other row names. A ref
    meant for an existing followup named nothing and was stored with no
    word, found only by reading the command back (dogfood, 2026-10-10):
    `item` has no catalog to miss. A ref may start an item, so a note, never
    a refusal. A new target is named only beside a near name, as a catalog
    miss is: a new item is how a task starts, and a target one letter off a
    stored item forked its thread at exit 0 (2026-10-13, 2026-10-15).
    `before` is the refs the written row inherited, already named once."""
    new = {n.id for n in written}
    every = store.load(store_dir)
    prior = {o.name for n in every if n.id not in new for o in (n.target, *n.refs)
             if o.type == "item" and o.name} | {r.name for r in before if r.type == "item"}
    targets = {n.target.name for n in written if n.target.type == "item"}
    others = [n for n in store.heads(every) if n.id not in new]
    for name in dict.fromkeys(r.name for n in written for r in n.refs
                              if r.type == "item" and r.name not in prior | targets):
        near = _containing(others, name)
        print(f"note: no other entry names item:{name}, so this ref starts a new item"
              + catalog.did_you_mean(name, prior)
              + (f"; these contain it: {near}" if near else ""), file=sys.stderr)
    for name in sorted(targets - prior - {None}):
        if guess := catalog.did_you_mean(name, prior):
            print(f"note: no other entry names item:{name}{guess}", file=sys.stderr)


def _containing(notes, name: str, typ: str | None = None) -> str:
    """`item:zed two (2 rows), file:zed.py (1 row)`: the targets whose name
    holds `name`, any case, most rows first, three at most; '' if none."""
    want = name.lower()
    near = Counter(f"{n.target.type}:{n.target.name}" for n in notes
                   if n.target.name and typ in (None, n.target.type)
                   and want in n.target.name.lower())
    shown = ", ".join(f"{t} ({summ._count(k, 'entry')})" for t, k in near.most_common(3))
    return shown + (f", +{len(near) - 3} more" if len(near) > 3 else "")


def _name_near_tags(store_dir, written) -> None:
    """After a write, name the existing tags a new one nearly is: case, `_`
    or `-`, a trailing s. `list --tag flaky-test` missed every row tagged
    `flaky-tests` (2026-09-26: such pairs in two adopter stores), and
    `tags` lists the vocabulary but nobody runs it before writing. A tag is
    new when no other head carries it."""
    new = {n.id for n in written}
    have = Counter(t for h in store.heads(store.load(store_dir)) if h.id not in new
                   for t in h.tags)
    for tag in dict.fromkeys(t for n in written for t in n.tags):
        if tag in have:
            continue
        near = sorted((t for t in have if summ.tag_key(t) == summ.tag_key(tag)),
                      key=lambda t: (-have[t], t))
        if near:
            names = ", ".join(f"{t!r} ({have[t]})" for t in near)
            print(f"note: new tag {tag!r}; this store has {names}: if they mean the "
                  f"same, reuse it", file=sys.stderr)


def _name_unchecked(written) -> None:
    """A verdict row with no `--checked` still reads `current`, `behind N`,
    `external` or `pending`, as if a run backed it: a quarter of the verdict
    heads in two adopter stores had neither field (2026-09-26). Say so at
    write; the stamp still dates the commit, so it is a note, not a refusal."""
    miss = [n for n in written if n.spec.verdict and not n.checked]
    if not miss:
        return
    what = (f"this {miss[0].kind} entry" if len(miss) == 1 else
            f"{len(miss)} entries ({', '.join(sorted({n.kind for n in miss}))})")
    print(f"note: no --checked on {what}: it has a state but names no run; --checked "
          f"names what ran, or on a prediction, what will run", file=sys.stderr)


def _name_dirty(written, external: bool = True) -> None:
    """A verdict row stamped on a dirty tree reads `unverifiable (dirty
    tree)` for good, even once the tree is committed, and the write said
    nothing (2026-10-14). Name the paths while the run can still be redone:
    an untracked scratch file counts too."""
    miss = [n for n in written if (n.provenance or {}).get("dirty")]
    if not miss:
        return
    prov = miss[0].provenance
    paths = prov.get("dirty_paths") or []
    count = prov.get("dirty_count", len(paths))
    what = (f"this {miss[0].kind} entry reads" if len(miss) == 1 else
            f"these {len(miss)} entries read")
    tree = f"the {prov['repo']} tree" if prov.get("repo") else "the tree"
    has = (f"{summ._count(count, 'uncommitted path')} "
           f"({', '.join(paths[:3])}{', …' if count > 3 else ''})"
           if count else "uncommitted changes")    # a [provenance] command may name none
    print(f"note: {tree} has {has}, so {what} "
          f"`unverifiable (dirty tree)` for good. To stamp a commit: commit, re-run, "
          f"then `undo` and write again" + (". A run outside the repo takes --external"
                                            if external else ""), file=sys.stderr)


def _name_undated(written) -> None:
    """A parked row prints at session start only from its due date on, or when
    written by someone other than the reader: most of one store's open ideas
    were the agent's own and undated, and no session of its saw them again
    (2026-10-12). Filed as someone else, it reaches the reader's session."""
    miss = [n for n in written if n.spec.parked and n.spec.status and not n.due
            and n.author == api.author_default()]
    if not miss:
        return
    what = (f"this {miss[0].kind} entry" if len(miss) == 1 else
            f"{len(miss)} entries ({', '.join(sorted({n.kind for n in miss}))})")
    print(f"note: no --due on {what}: it is parked, so no session start of yours "
          f"prints it again; --due DATE sets a revisit", file=sys.stderr)


def _name_past_due(written) -> None:
    """A due date already past at the write: a trial agent registered a
    prediction with the date the user gave, after it, and the skill said
    nothing of it (2026-10-26). The row reads overdue at once."""
    for n in written:
        state = store.due_state(n.due) if n.due and store.read_status(n) == "open" else None
        if state and state[0]:
            print(f"note: --due {n.due} has passed: {n.id[-10:]} reads overdue "
                  f"at every session start from now; give a later date if that "
                  f"window is still open", file=sys.stderr)

def _name_dropped(old, note, *, refs: bool, tags: bool) -> None:
    """`supersede --ref` and `--tag` replace, while `resolve --ref` and every
    `--add-*` add: a session that meant to add a fixing commit dropped the
    row's only ref, and nothing said so (2026-10-01). Name what a replace
    dropped, measured against the row the write built on, read before it."""
    if old is None:
        return
    for flag, on, was, now, label in (("ref", refs, old.refs, note.refs, summ.ref_label),
                                      ("tag", tags, old.tags, note.tags, repr)):
        gone = [label(v) for v in was if v not in now]
        if on and gone:
            # A ref is dropped only past a leading '' now: no advice for that.
            print(f"note: --{flag} replaced the {flag}s, dropping {', '.join(gone)}"
                  + (f"; --add-{flag} adds one and keeps the rest" if flag == "tag" else ""),
                  file=sys.stderr)


def _say_body_dropped(store_dir, by_id, tip, note) -> None:
    """`supersede --body` replaces the whole text: meant to correct one
    sentence, it replaced a resolved row's report and the resolution added
    below it, at exit 0 (dogfood, 2026-10-15). Name the added-below sections
    the rewrite lost, counted as `show` marks them. `by_id` is the store
    before the write: the row keeps its id, so its old version moved."""
    after = {n.id: n for n in store.load(store_dir)}
    lost = summ._amendments(by_id, tip) - summ._amendments(after, note)
    if lost > 0:
        print(f"note: --body replaced the whole text, dropping "
              f"{summ._count(lost, 'added-below section')}; --append adds below and keeps them",
              file=sys.stderr)


def _echo_clipped_heads(store_dir, written) -> None:
    """Session start prints an open row outside active arcs as its first
    HEAD_CHARS, and those were usually context, not the claim or the ask
    (2026-09-26). Show the writer the cut while it is cheap to fix. A due or
    starred row prints at BODY_CHARS instead; the heads' cut is the tighter.
    Every one, uncapped: a batch is when the writer has the most heads to
    check, and a cap of 3 hid 4 of a bootstrap's 7 with no way to list them
    (2026-09-28)."""
    active = {a.id for a in store.load_arcs(store_dir) if not a.archived}
    cut = [n for n in written
           if n.spec.status and not n.spec.parked and store.read_status(n) == "open"
           and not n.spec.verdict            # a pre-registration: session start
           and n.arc_id not in active        # names its registration date itself
           and len(summ.clip(n.body, len(n.body or "") + 1)) > summ.HEAD_CHARS]
    if not cut:
        return
    # "check that", not an order: a row that already leads with its ask read
    # the old wording as a rebuke (cold bootstrap, 2026-09-27).
    if _first_clip_note():
        print(f"note: session start shows an open entry's first {summ.HEAD_CHARS} chars; "
              f"check that they carry its claim or ask:", file=sys.stderr)
    else:
        cut = [n for n in cut if _dated_opening(summ.clip(n.body, summ.HEAD_CHARS))]
        if not cut:
            return
        print(f"note: session start shows an open entry's first {summ.HEAD_CHARS} chars; a lead "
              f"that opens on a date often holds context, not the claim or ask:", file=sys.stderr)
    for n in cut:
        print(f"  {n.id}  {summ.clip(n.body, summ.HEAD_CHARS)}", file=sys.stderr)


def _mark_read(ctx, sid: str, at: str) -> None:
    """When this session's start summary read the store: `_say_news` takes
    no row from before it as news. The open-row lists start over
    (`_first_listing`): a compacted session has lost them."""
    store._replace_atomically(_session_cache() / f"{sid}.read",
                              f"{Path(ctx.store_dir).resolve()}\t{at}\n")
    (_session_cache() / f"{sid}.open").unlink(missing_ok=True)


def _session_mark(suffix: str) -> Path | None:
    """This agent session's `suffix` file in the session cache, or None
    outside a session."""
    key = store.session_key()
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        return None
    return _session_cache() / f"{key}.{suffix}"


def _session_read(ctx):
    """When this agent session's start summary read this store, or None."""
    mark = _session_mark("read")
    if mark is None:
        return None
    try:
        where, at = mark.read_text(encoding="utf-8").strip().split("\t")
    except (OSError, ValueError):
        return None
    return store.written_at(None, at) if where == str(Path(ctx.store_dir).resolve()) else None


def _first_listing(store_dir, target) -> bool:
    """False once this agent session's open-rows note has listed the rows on
    `target` (type, name, title) in this store; true, and marked, the first
    time. With no session key, every time."""
    mark = _session_mark("open")
    if mark is None:
        return True
    line = json.dumps([str(Path(store_dir).resolve()), *target])
    try:
        if line in mark.read_text(encoding="utf-8").splitlines():
            return False
    except OSError:
        pass
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        with open(mark, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:                  # a cache it cannot write costs the quiet, not the note
        pass
    return True


def _first_clip_note() -> bool:
    """False once this agent session has had the clip note. Each `add` is its
    own process, and one session's batches printed it 23 times, each echoing
    text just written (2026-10-11). SKILL.md teaches the rule, so a compacted
    session keeps it. With no session key, every time, as before.
    # ponytail: a subagent shares its session's key and so its mark; a
    # machine whose session-start hook never runs keeps every mark."""
    mark = _session_mark("clip")
    if mark is None:
        return True
    try:
        if mark.exists():
            return False
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    except OSError:                  # a cache it cannot write costs the quiet, not the note
        pass
    return True


def _dated_opening(lead: str) -> bool:
    """A short opening clause that holds a date: the lead opens on provenance
    ("IDEA (user, 2026-09-26):", "MEASURED 2026-09-07:"), not the claim. Over
    the clipped open leads of many stores (2026-10-12) this flagged about 2%,
    nearly all provenance; a lowercase start or a short opening clause alone
    flagged mostly good leads."""
    end = re.search(r"[.:;](?:\s|$)|\s[—-]{1,2}\s", lead)
    opening = lead[:end.start() + 1] if end else lead
    return len(opening) < 40 and bool(re.search(r"\d{4}-\d{2}-\d{2}", opening))


# ---- misses: what failed, the legal values, the command that lists them ----
def _no_arc(store_dir, arc_id: str) -> None:
    print(api.no_arc(store_dir, arc_id), file=sys.stderr)


def _name_archived_arcs(store_dir, written) -> None:
    """An open box filed in an archived arc lists outside arcs, and the arc
    reads one short under `arc list --all`: the write said nothing (dogfood,
    2026-10-28). check_arc lets it through: a closing note may go there."""
    gone = {a.id for a in store.load_arcs(store_dir) if a.archived}
    boxes = Counter(n.arc_id for n in written if n.arc_id in gone and n.spec.status
                    and not n.spec.parked and store.read_status(n) == "open")
    for arc, k in boxes.items():
        what = "this open entry lists" if k == 1 else f"these {k} open entries list"
        print(f"note: arc {arc} is archived: {what} outside arcs (list --arc {arc} --status open)",
              file=sys.stderr)


def _no_note(note_id: str, store_dir=None) -> None:
    # The summary prints arc ids beside entry ids, and an agent held one as
    # the other (dogfood, 2026-10-28).
    if store_dir and any(a.id == note_id for a in store.load_arcs(store_dir)):
        print(f"{note_id} is an arc, not an entry: arc todo {note_id} lists its open boxes, "
              f"list --arc {note_id} every entry in it", file=sys.stderr)
        return
    print(f"no entry found: {note_id} (an id, or its tail after a dash: 123456-a1b, a1b; "
          f"list --grep TEXT searches the entries)", file=sys.stderr)
    # A project's session looked up the rows it had filed in another
    # project's store, and each miss took a second call (2026-10-25).
    tok = store.id_tail(note_id)
    for p, ids in _sibling_ids(store_dir) if store_dir else ():
        if len(hits := _held(ids, tok)) == 1:
            where = shlex.quote(_from_here(p))
            print(f"  {where} has it: symbion --dir {where} show {hits[0]}", file=sys.stderr)


def _say_tip(store_dir, nid: str, *, resolving: bool):
    """A write to a superseded id lands on its chain's tip (the fast-forward
    that closes a race), and printed only the new id: a newcomer's
    `supersede <old> --body` replaced the newer row's text unawares
    (2026-09-26). Say which row the write lands on, and return it as it was
    before the write: the head keeps the id."""
    try:
        tip = store.chain_tip(store.load(store_dir), nid)
    except KeyError:
        return None                              # the verb's own no-note message
    if tip.id != nid:
        print(f"note: {nid} was superseded by {tip.id}; this applies to {tip.id}",
              file=sys.stderr)
    if resolving and store.read_status(tip) == "resolved":
        print(f"note: {tip.id} is already resolved", file=sys.stderr)
    return tip


def _others() -> str:
    """Whose rows a commit leaves: in an agent session it takes that
    session's, so the rest are other sessions', the same author's included
    (`it leaves 5 rows of other writers (claude 5)`, read by claude,
    2026-10-12); with no session, other writers'."""
    return "other sessions" if store.session_key() else "other writers"


def _say_pulled(ctx, p, quiet=False, cap=None) -> int:
    """`pull`'s lines, one for each thing it did; 1 when it merged nothing it
    should have. A merge git would refuse names the git commands that let it.
    What a killed pull set aside is said first: it went back whatever else did.
    `quiet` leaves out the line for a pull that found nothing new. `cap` shows
    that many split and dropped lines and counts the rest: the lines after
    them (the commits pulled, a changed symbion.toml, an older client, a
    stopped pull's commands) are few and are never cut. A changed
    symbion.toml is said before a pull that stopped says why: a pull a kill
    cut off may have merged it."""
    if p.recovered:
        print(p.recovered)
    shown = [*(merge.said_split(kept, new) for kept, new in p.splits), *p.dropped]
    if cap is not None and len(shown) > cap:
        shown = [*shown[:cap], f"+{len(shown) - cap} more not shown"]
    for line in [*shown, *p.notices(ctx)]:
        print(line)
    if p.error:
        print(f"pull: {p.error}", file=sys.stderr)
        return 1
    s = shlex.quote(str(ctx.store_dir))
    me = " ".join(filter(None, ["symbion", _named_store(ctx)]))
    if p.conflicts:
        files = " ".join(map(shlex.quote, p.conflicts))
        # Held here: changed, staged or untracked. Else both sides committed a change.
        held = gitref._git(ctx.store_dir, "--no-optional-locks", "status", "--porcelain", "-z",
                           "--untracked-files=all", "--", *p.conflicts).stdout.strip("\0")
        print("pull: merged nothing, and nothing here changed: "
              + ("git will not merge over the changes not committed to: " if held
                 else "both sides changed, and git cannot merge: ") + files, file=sys.stderr)
        if held:
            # `add` the untracked ones first: a commit names no untracked path,
            # and an `add` of a deletion `git rm` staged names nothing.
            loose = {e[3:] for e in held.split("\0") if e.startswith("?? ")}
            new = [f for f in p.conflicts if f in loose]
            say = ["commit them, or move them aside, then pull again:",
                   *([f"git -C {s} add -- {' '.join(map(shlex.quote, new))}"] if new else []),
                   f'git -C {s} commit -m "this side\'s change, before a pull" -- {files}',
                   f"{me} pull"]
        else:
            rows = store.pending_rows(ctx.store_dir)
            keep = store.with_bases(rows, api.own_rows(ctx))
            others = not all(map(keep, rows))
            say = ["to merge by hand, commit the pending entries"
                   + (" (the other sessions' with --all, once their session has ended)"
                      if others else "") + ", merge, fix each file git names, and commit:",
                   *([f"{me} commit"] if any(map(keep, rows)) else []),
                   *([f"{me} commit --all"] if others else []),
                   f"git -C {s} merge {shlex.quote(p.upstream)}", f"git -C {s} commit"]
        print(f"pull: {say[0]}", *(f"  {c}" for c in say[1:]), sep="\n", file=sys.stderr)
        return 1
    if p.merged:
        print(f"pulled {summ._count(p.merged, 'commit')} from {p.upstream}")
    elif not quiet:
        print(f"up to date with {p.upstream or 'origin'}")
    return 0


def _untrusted_note(ctx) -> str | None:
    """The line a read prints while a pulled symbion.toml waits on `trust`."""
    if not catalog.untrusted(ctx.store_dir):
        return None
    return "note: " + _untrusted(ctx, catalog.UNTRUSTED)


def _untrusted(ctx, e) -> str:
    """A write's refusal or a read's note, naming the store's own `trust`:
    under `--dir`, a bare `symbion trust` acts on the default store."""
    if str(e) != catalog.UNTRUSTED or ctx is None:
        return str(e)
    me = " ".join(filter(None, ["symbion", _named_store(ctx)]))
    return catalog.UNTRUSTED.replace("`symbion trust`", f"`{me} trust`")


def _trust(ctx) -> int:
    """`trust`: every change to symbion.toml since the first pull that shut
    the gate, then a yes read from a terminal. stdin from a pipe refuses,
    so an agent's shell cannot accept: it tells the user. Not a wall, as an
    agent runs as the user does; it stops one that obeys a pulled row."""
    s, old = ctx.store_dir, catalog.untrusted(ctx.store_dir)
    if not old:
        print("nothing to trust: no pull has changed symbion.toml since it was last trusted")
        return 0
    for args in (["log", "--no-show-signature", "--format=%h %an %s"], ["diff"]):
        print(gitref._git(s, *args, f"{old}..HEAD", "--", config.CONFIG_FILE).stdout, end="")
    sys.stdout.flush()                   # the change, above the words that name it
    if not sys.stdin.isatty():
        print("trust: the user accepts this at a terminal; an agent shows them the change "
              "and asks them to run it", file=sys.stderr)
        return 1
    try:
        said = input("Accept this symbion.toml? It names commands symbion runs. [y/N] ")
    except EOFError:                     # Ctrl-D is a no
        said = ""
    if said.strip().lower() not in ("y", "yes"):
        print("not trusted: writes and its commands still wait", file=sys.stderr)
        return 1
    catalog.untrusted_path(s).unlink()
    print("trusted: writes and symbion.toml's commands run again")
    return 0


SYNC_CAP = 5                        # split and dropped lines of session start's pull the summary carries
HOOK_FETCH_TIMEOUT = 4              # seconds session start waits on origin: HOOK_TIMEOUT stops the hook
HOOK_HISTORY_TIMEOUT = 3            # seconds the gotcha list counts its files' commits; past it, no count
HOOK_LOCK_WAIT = 2                  # seconds session start's pull waits on a store another command holds
# Seconds from session start's read to the end of its gotcha count. Each
# budget above fit alone, and together they ran 9.7 s against HOOK_TIMEOUT
# (audit, 2026-10-28). The pull's two (6 s) fit in it; the count takes what is left.
HOOK_DEADLINE = 8
PUSH_TIMEOUT = 30                   # seconds `commit` waits on origin's push


def _hook_pull(ctx) -> tuple[list[str], list[str]]:
    """Session start's pull in a shared store: the lines it says, none when
    it found nothing, and those of them the person reads too
    (Pulled.notices). The hook never fails a session: whatever the pull
    raises becomes a line, and the summary then reads the local store. A
    store another command holds past HOOK_LOCK_WAIT is not pulled: a wait
    with no bound outlasted the hook."""
    out, local = io.StringIO(), "; the summary below reads the local store"
    try:
        if not store.shared(ctx.store_dir) or store.read_only():
            return [], []
        # The hook's text has a budget, and `fit` cannot trim this block.
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            p = api.pull(ctx, timeout=HOOK_FETCH_TIMEOUT, until=time.monotonic() + HOOK_LOCK_WAIT)
            _say_pulled(ctx, p, quiet=True, cap=SYNC_CAP)
    except store.Busy:
        me = " ".join(filter(None, ["symbion", _named_store(ctx)]))
        return [f"pull: skipped: another symbion command holds the store; pull once it is "
                f"done: {me} pull"], []
    except Exception as e:
        return [f"pull: {str(e) or type(e).__name__}{local}"], []
    said = out.getvalue().splitlines()
    if p.error:                          # _say_pulled's last line
        said[-1] += local
    return said, p.notices(ctx)


def _pull_then_push(ctx) -> None:
    """A shared store's `commit`, once it has committed: api.sync's pull,
    then its push, each said. A pull that stopped holds the push, and a push
    git refused names the commands that retry. Neither is an exit code: the
    commit is made, and a sync that raises holds the push as a stopped pull
    does."""
    me = " ".join(filter(None, ["symbion", _named_store(ctx)]))
    try:
        s = api.sync(ctx, timeout=PUSH_TIMEOUT)
    except Exception as e:
        print(f"not pushed: {summ.flatten(str(e)) or type(e).__name__}; the push waits on "
              f"{me} pull, then {me} push")
        return
    p = s.pulled
    if _say_pulled(ctx, p, quiet=True):
        print(f"not pushed: the pull stopped on {' '.join(p.conflicts)}; the push waits on the "
              f"merge, then {me} push" if p.conflicts else
              f"not pushed: the pull failed; the push waits on {me} pull, then {me} push")
        return
    if s.timed_out:
        print(f"push took over {PUSH_TIMEOUT:g}s and was stopped; the push waits: {me} push")
        return
    r = s.pushed
    if r.returncode == 0:
        print(f"pushed {summ._count(s.carried, 'commit')} to origin")
        return
    said = [ln.strip() for ln in r.stderr.split("\n") if ln.strip()]
    why = next((ln for ln in said if ln.startswith("!")),
               said[-1] if said else f"git exited {r.returncode}")
    # Origin moving since this pull is the likeliest refusal, and only a pull mends it.
    print(f"push failed: {why}; retry: {me} pull, then {me} push")


def _commit_hint(ctx) -> str:
    """The command that commits the pending rows. `commit` takes only the
    caller's, so beside rows that are all someone else's it committed none
    of them (2026-10-12): name `--all`, and when it is safe."""
    cmd = " ".join(filter(None, ["symbion", _named_store(ctx), "commit"]))
    rows = store.pending_rows(ctx.store_dir)
    keep = store.with_bases(rows, api.own_rows(ctx))
    if rows and not any(keep(r) for r in rows):
        return f"{cmd} --all once their session has ended"
    return cmd


def _say_pending(ctx) -> None:
    """`add` wrote two rows and said nothing of their being uncommitted: the
    session found them by `git status` in the store at its end (dogfood,
    2026-10-11). End a write as `commit` ends, with the command, and say
    whose rows it takes and whose it leaves. An arc write changes arcs.jsonl
    alone, which every commit takes: `arc archive` printed the id and no
    note (dogfood, 2026-10-28)."""
    rows = store.pending_rows(ctx.store_dir)
    keep = store.with_bases(rows, api.own_rows(ctx))
    mine, left = [r for r in rows if keep(r)], [r for r in rows if not keep(r)]
    cmd = " ".join(filter(None, ["symbion", _named_store(ctx), "commit"]))
    if mine:
        more = (f"; it leaves {summ._count(len(store.entries(left)), 'entry')} of {_others()} "
                f"({store.rows_by_author(left)})" if left else "")
        print(f"note: {summ._count(len(store.entries(mine)), 'entry')} not committed "
              f"({store.rows_by_author(mine)}): {cmd}{more}", file=sys.stderr)
        return
    arcs = gitref.registry_modified(ctx.store_dir)
    if rows:
        print(f"note: {summ._count(len(store.entries(rows)), 'entry')} not committed "
              f"({store.rows_by_author(rows)}), none of them yours: {cmd} --all takes them"
              + (f"; arcs.jsonl changed too: {cmd} takes it" if arcs else ""),
              file=sys.stderr)
    elif arcs:
        print(f"note: arcs.jsonl not committed: {cmd}", file=sys.stderr)


def _say_news(ctx, wrote) -> None:
    """Each row someone else wrote or amended since the writer's previous
    write, as the summary prints it. A user's reply through the GUI
    mid-session reached the agent only by `commit`'s per-author count, four
    times (2026-10-10, 2026-10-11): session start had run before it. Quiet at
    a terminal, where the reader is the person, and on a first write, which
    session start covered. The floor is the later of the writer's previous
    write and this session's start summary: a session's first write repeated
    what that summary had printed (2026-10-13). A row this write touched is
    not news. `wrote` holds each id this command wrote.

    "Previous" is by place in the file, above this write's first new line.
    It was by time, before the command began, and a stamp holds whole
    seconds: a write in the second the next command began did not count, and
    commands run in parallel all began before any of them wrote, so each
    repeated the same news (2026-10-15)."""
    if sys.stdout.isatty():
        return
    reader = api.author_default()
    side = store.read_side(ctx.store_dir)
    # The other side's `claude` is someone else here: its write is no news
    # (the head is filtered below), and it must not move the writer's cutoff.
    loaded = summ.seen_from(store.load(ctx.store_dir), side)
    pos = {n.id: i for i, n in enumerate(loaded)}
    at = min((pos[i] for i in wrote if i in pos), default=len(loaded))
    key = lambda n: (store.written_at(n), pos[n.id])     # noqa: E731
    # A version this session wrote is not news, under any author: a capture
    # in the user's words came back as someone else's (2026-10-14).
    # ponytail: pending rows only, as the sessions file keeps them; one
    # written before a commit, and after the writer's last own write, reads as
    # news once. A session key on each row would close it.
    sk = store.session_key()
    ours = {i for i, k in store.sessions(ctx.store_dir).items() if k == sk} if sk else set()
    mine = [key(n) for n in loaded[:at] if n.author == reader or n.id in ours]
    if not mine:
        return
    read = _session_read(ctx)          # -1: a row in that second may postdate it
    since, by_id = max(mine + ([(read, -1)] if read else [])), {n.id: n for n in loaded}
    news = store.newest_first(
        h for h in store.heads(loaded) if h.id not in wrote and summ.on_side(by_id, h, side)
        and any(summ.wrote_it(reader, v.author) and key(v) > since and pos[v.id] < at
                for v in summ._chain(by_id, h)))
    if not news:
        return
    print("note: written by someone else since your previous write:", file=sys.stderr)
    for h in news[:_NEWS_CAP]:
        r = summ.row_digest(by_id, h, summ.HEAD_CHARS, reader)
        closer = summ._closer(by_id, h).author if store.read_status(h) == "resolved" else None
        if closer and r.get("last") == closer:
            del r["last"]
        print("  " + summ._labelled(summ.plain, r, first=closer and f"closed by {closer}"),
              file=sys.stderr)
    if len(news) > _NEWS_CAP:
        print(f"  +{len(news) - _NEWS_CAP} more: symbion list --since "
              f"{since[0].isoformat(timespec='seconds')}", file=sys.stderr)


_NEWS_CAP = 5
UPSTREAM_FETCH_TIMEOUT = 5          # seconds a write may wait on origin
UPSTREAM_CHECK_SECONDS = 600        # a fetch younger than this is not repeated


def _say_upstream(ctx) -> None:
    """After a write in a shared store, say when origin holds commits this
    clone lacks. It fetches at most once in UPSTREAM_CHECK_SECONDS, outside the
    lock, and never merges: a session's view of the store changes only when
    the session runs `pull`. A write that was made does not fail on a check,
    so any failure here is silence."""
    s = ctx.store_dir
    try:
        if not store.shared(s):
            return
        age = gitref.fetched_age(s)
        if age is None or age > UPSTREAM_CHECK_SECONDS:
            gitref.fetch(s, UPSTREAM_FETCH_TIMEOUT)      # a failure leaves the last fetch's count
            gitref.stamp_fetch(s)                        # and the next write waits out the interval
        if n := gitref.behind(s):
            me = " ".join(filter(None, ["symbion", _named_store(ctx)]))
            print(f"origin has {summ._count(n, 'new store commit')}: {me} pull", file=sys.stderr)
    except Exception:
        pass


def _say_dropped(tip, note) -> None:
    """A legacy row's field its kind cannot hold leaves the head on the next
    write (store.unholdable), and only the old row keeps it: a migrated
    store's rows lost that text from the head unseen."""
    gone = store.unholdable(tip) if tip is not None and note.kind == tip.kind else []
    if gone:
        why = " and ".join([*(f"has no {b} bit" for b, keys in (("verdict", {"checked", "result"}),
                                                                ("status", {"status", "due"}))
                              if keys & set(gone)),
                            *(["is not a question"] if "options" in gone else [])])
        print(f"note: kind {note.kind!r} {why}, so {note.id} drops "
              f"{', '.join(gone)}; its earlier version {note.supersedes} keeps them "
              f"(show {note.supersedes} --all --json). "
              f"To keep the text on the head, add it to the body", file=sys.stderr)


def _name_sweep_drops(store_dir, before: set) -> None:
    """`_say_dropped` for a sweep (rename, reconcile --apply), which printed
    only its counts: every row the sweep wrote since `before` (the ids then)
    whose old row held a field its kind cannot hold. A written row keeps its
    id, and the version it replaced took a new one."""
    notes = store.load(store_dir)
    by_id = {n.id: n for n in notes}
    old = [by_id[n.supersedes] for n in notes
           if n.supersedes not in before and n.supersedes in by_id
           and n.kind == by_id[n.supersedes].kind and store.unholdable(by_id[n.supersedes])]
    if old:
        fields = ", ".join(sorted({k for o in old for k in store.unholdable(o)}))
        more = f" (+{len(old) - 1} more)" if len(old) > 1 else ""
        print(f"note: {summ._count(len(old), 'entry')} dropped {fields}, a field its kind "
              f"cannot hold; the old entry keeps it: show {old[0].id} --all --json{more}",
              file=sys.stderr)


def _held(ids, tok: str) -> list[str]:
    """The ids a cited `tok` names: a full id or its start, or a tail after
    a dash."""
    return [i for i in ids if (i.startswith(tok) if tok[8:9] == "-" else i.endswith("-" + tok))]


def _siblings(store_dir) -> list[Path]:
    """The other stores beside this one, `../*-notes`."""
    # ponytail: a miss parses every sibling, 0.15 s to 0.5 s in all
    # (2026-10-27); skip a store whose text lacks the id if a write feels it.
    here = Path(store_dir).resolve()
    return [p for p in sorted(here.parent.glob("*-notes")) if p != here and store.exists(p)]


def _sibling_ids(store_dir):
    """Each sibling store and its ids. One that does not load (a toml a
    stopped pull left in conflict) is skipped: it raised after a write had
    landed, and the exit 1 invited a retry that wrote the row twice."""
    for p in _siblings(store_dir):
        try:
            yield p, [n.id for n in store.load(p)]
        except (ValueError, OSError):        # TOMLDecodeError is a ValueError
            continue


def _from_here(path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:          # Windows: no relative path to another drive
        return str(path)


def _expand_id(store_dir, raw: str) -> str | None:
    """The full id for `raw`: itself when exact, else the one id ending in
    `-<raw>` (a leading `…`, `...` or `-` dropped, as ids are cited in prose).
    A miss returns `raw`, for the verb's own no-note message. A tail more
    than one id ends in prints them all and returns None: never pick one.
    A 3-hex tail does not stay unique (measured 2026-09-24: 11 shared tails
    in a 383-row store)."""
    notes = store.load(store_dir) if store.exists(store_dir) else []
    tail = store.id_tail(raw)
    if any(n.id == tail for n in notes):
        return tail
    hits = [n for n in notes if n.id.endswith("-" + tail)]
    if len(hits) == 1:
        return hits[0].id
    if not hits:
        return raw
    print(f"ambiguous id {raw!r}: {len(hits)} entries end in -{tail}; give more of it",
          file=sys.stderr)
    for n in hits:
        print(f"  {n.id}  [{n.kind}] {n.target.type}:{summ.clip(n.target.name, summ.NAME_CHARS)}"
              f"  {summ.clip(n.body, summ.HEAD_CHARS)}", file=sys.stderr)
    return None


def _orphan_note(ctx) -> str:
    """A second line for `no store at <repo>-notes` when a store beside it has
    no project: a repo renamed with no `.symbion` lands here, and the line
    above sends it to the init that starts a second store."""
    root = ctx.cfg.project_root
    if root is None or ctx.store_dir != (Path(root).parent / f"{Path(root).name}-notes").resolve():
        return ""
    found = [f"../{s.name}" for s in config.orphan_stores(Path(root))]
    if not found:
        return ""
    if len(found) == 1:
        where, them, point = f"{found[0]}, which no project names", "it", found[0]
    else:
        more = f", +{len(found) - 3} more" if len(found) > 3 else ""
        where = f"one of {', '.join(found[:3])}{more}, which no project names"
        them, point = "one", "../<store>"
    return (f"\nnote: if this repo was renamed, its store may be {where}: "
            f"`echo {point} > .symbion` points this repo at {them}, instead of init")


def _pointer_note(ctx) -> str:
    """A second line for `no store at` when the repo's `.symbion` names that
    path: a person whose clones sit in another layout reads a committed
    pointer to nowhere, and the line above sent their agent to the init that
    starts a store nobody else reads (a shared-store trial, 2026-10-27). A tracked
    pointer is every clone's, so editing it is no way out there."""
    root = ctx.cfg.project_root
    if root is None or config._pointer(Path(root), ctx.cfg.work_root) != ctx.store_dir:
        return ""
    pointer = Path(root) / config.POINTER_FILE
    if _git_says(Path(root), pointer) == "git: tracked":
        return (f"\nnote: {pointer} names it. Clone or move the store there: git tracks "
                ".symbion, so every clone reads that path; init starts a new, empty store")
    return (f"\nnote: {pointer} names it. Clone or move the store there, "
            "or write your store's path in .symbion; init starts a new, empty store")


# ---- dispatch ----
_READS = {"list", "tags", "summary", "schema", "context"}
_ARC_READS = {"list", "todo", "reconcile"}


def _dispatch(args, ctx) -> int:
    store_dir, cfg, target_types = ctx.store_dir, ctx.cfg, ctx.target_types
    _resolve_body(args)
    if args.cmd == "init":
        return _init(store_dir, cfg, ctx.store_from_env, args.yes, args.repoint, args.agent,
                     args.merge_hook)
    if args.cmd == "completion":
        import argcomplete
        print(argcomplete.shellcode(["symbion"], shell=args.shell), end="")
        return 0
    if args.cmd == "serve" and (args.restart or args.stop):
        return _serve_all(restart=args.restart)

    # An absent store is never a first session: `init` creates it. It is a
    # `.symbion` pointer to nowhere, a renamed repo, a wrong --dir or
    # SYMBION_DIR, and every read's empty state (`0 rows; no notes yet:
    # symbion add ...`) sent the reader to the write that forks a second
    # store. So it is an error, on stderr, for a write too (2026-09-28: a
    # write used to create it) -- except summary's, which is the hook's
    # line: stdout, exit 0. A read's --json keeps its always-valid shape,
    # and summary --json its `store: null` gate.
    if not store.exists(store_dir):
        msg = f"symbion: no store at {store_dir}; run `symbion init`" + _orphan_note(ctx) + _pointer_note(ctx)
        read = args.cmd in _READS or args.cmd == "arc" and args.acmd in _ARC_READS
        if read and getattr(args, "json", False):
            # The shape stays, for the parser; a bare `[]` at exit 0 read as
            # an empty store under a wrong --dir (2026-09-27). summary's
            # `store: null` is its gate, so it stays quiet.
            if args.cmd != "summary":
                print(msg, file=sys.stderr)
        elif args.cmd == "summary":
            print(_hook_json(msg, msg) if args.hook else msg)
            return 0
        else:
            print(msg, file=sys.stderr)
            return 1

    # Once, before any verb reads it: a cited tail (`a1b`, `…123456-a1b`) stands for
    # the one id that ends in it.
    if args.cmd == "list" and args.note_id:
        args.note_id = [_expand_id(store_dir, raw) for raw in args.note_id]
        if None in args.note_id:
            return 1
    attr = {"resolve": "id", "supersede": "id", "undo": "id"}.get(args.cmd)
    tip = None                                   # the row a resolve or supersede builds on
    if attr and getattr(args, attr):
        full = _expand_id(store_dir, getattr(args, attr))
        if full is None:
            return 1
        setattr(args, attr, full)
        if args.cmd in ("resolve", "supersede"):
            tip = _say_tip(store_dir, full, resolving=args.cmd == "resolve")

    if args.cmd == "undo":
        try:
            u = api.undo(ctx, args.id, author=_resolved_author(args))
        except store.UndoRefused as e:
            print(f"undo: {e}", file=sys.stderr)
            return 1
        print(f"undone: {store.undo_words(u)}; the store reads as it did before it")
        return 0

    if args.cmd == "add":
        if args.kind_pos and args.kind:
            print("add: KIND or --kind, not both", file=sys.stderr)
            return 2
        args.kind = args.kind or args.kind_pos
        if not _take_target(args, "add"):
            return 2
        flag_row = _row_from_flags(args)
        if args.from_json:
            if set(flag_row) - {"author"}:      # --author is the rows' default
                print("add: --from-json takes no other note flags", file=sys.stderr)
                return 2
            rows, errors = _rows_from_json(args.from_json)
        else:
            if not (args.kind and args.type):
                print(f"add: a kind and a target are required: `add KIND --target TYPE:NAME`, "
                      f"`add --kind K --type T [--name N]`, or --from-json PATH; "
                      f"kinds: {', '.join(ctx.kinds)}; types: {', '.join(sorted(target_types))}; "
                      f"`symbion schema` says when to use each kind", file=sys.stderr)
                return 2
            rows, errors = [(None, flag_row)], []
        author = _resolved_author(args)
        fields = []
        for lineno, row in rows:
            try:
                fields.append(api.fields_from_row(ctx, row, author))
            except ValueError as e:
                errors.append((lineno, f"line {lineno}: {e}" if lineno else str(e), row))
        if errors:
            # Every failing line, in line order: the key check ran over the
            # whole batch first, so line 3 was named before line 2 (2026-10-09).
            errors.sort(key=lambda e: e[0] or 0)
            where = "--from-json " if args.from_json else ""
            for _, e, *row in errors[:_ERROR_CAP]:
                print(f"add: {where}{e}", file=sys.stderr)
                if row:
                    _name_named_project_rows(ctx, row[0])
            if len(errors) > _ERROR_CAP:
                print(f"add: +{len(errors) - _ERROR_CAP} more failing lines; nothing was written",
                      file=sys.stderr)
            return 1
        try:
            written = api.add_fields(ctx, fields)
        except store.Twin as e:
            if not args.from_json:          # a retry: the id it wrote first
                print(e.pairs[0][1])
                print(f"note: an identical entry is already stored: {e.pairs[0][1]}; "
                      f"nothing written", file=sys.stderr)
                return 0
            for i, nid in e.pairs[:_ERROR_CAP]:
                print(f"add: --from-json line {rows[i][0]}: identical to stored entry {nid}; "
                      f"nothing was written", file=sys.stderr)
            return 1
        for note in written:
            print(note.id)
            args.wrote.add(note.id)
        _name_unknown_ids(store_dir, [n.body for n in written])
        _name_open_neighbours(store_dir, written)
        _name_unmerged_fixes(ctx.cfg, store_dir, written)
        _name_near_tags(store_dir, written)
        _name_new_items(store_dir, written)
        _name_unchecked(written)
        _name_dirty(written)
        _name_undated(written)
        _name_past_due(written)
        _name_archived_arcs(store_dir, written)
        if len(written) > 1:
            _name_catalog_misses(ctx.cfg, fields, written)
        _echo_clipped_heads(store_dir, written)
        _echo_gotcha_notes(written, ctx.input_types)
        _name_long_text([(n.id, len((n.body or "").strip())) for n in written])
        return 0

    if args.cmd == "list":
        if args.arc_id and not any(a.id == args.arc_id
                                        for a in store.load_arcs(store_dir)):
            _no_arc(store_dir, args.arc_id)
            return 1
        if args.fixed_strings and args.grep is None:
            print("list: -F needs --grep PATTERN", file=sys.stderr)
            return 2
        if args.side is not None and not store.shared(store_dir):
            print("list: --side needs shared = true in symbion.toml", file=sys.stderr)
            return 2
        if not _take_target(args, "list"):
            return 2
        pattern = re.escape(args.grep) if args.fixed_strings else args.grep
        try:
            grep = re.compile(pattern, re.I | re.M) if args.grep is not None else None
        except re.error as e:
            print(f"list: --grep {shlex.quote(args.grep)} is not a regex: {e}", file=sys.stderr)
            return 2
        # Authors as the reader's side has them (`claude (B)`), for the filters and
        # the text; --json takes the stored rows back by id.
        stored = store.load(store_dir)
        side = store.read_side(store_dir)
        every = summ.seen_from(stored, side)
        if args.note_id and not args.all:
            # A cited id names a thread, not a version: one cited in chat went
            # stale within hours, and its --json `status` read `open` on a
            # resolved thread (dogfood, 2026-10-11). resolve, supersede and
            # --grep already follow it forward; --all reads that version.
            tips = {}
            for i in args.note_id:
                try:
                    tips[i] = store.chain_tip(every, i).id
                except KeyError:
                    continue                           # the miss below names it
                if tips[i] != i:
                    print(f"note: {i} was superseded; this is its head, {tips[i]} "
                          f"(--all shows {i} itself)", file=sys.stderr)
            args.note_id = list(dict.fromkeys(tips.get(i, i) for i in args.note_id))
        base = every if (args.all or args.note_id) else store.heads(every)
        if args.note_id:
            base = [n for n in base if n.id in args.note_id]
        scope = every                  # what the hints on superseded rows count against
        if args.side is not None:
            roots = {n.id: n for n in every}
            ours = {n.id for n in every if summ.side_of(roots, n) == catalog.nfc(args.side)}
            base = [n for n in base if n.id in ours]
            scope = [n for n in every if n.id in ours]
        if args.name and args.type is None:
            t, colon, n = args.name.partition(":")
            if (colon and t in ctx.input_types
                    and not any(x.target.name == args.name for x in every)):
                # The miss hint below prints TYPE:NAME, and pasting it back
                # read a silent 0: no target NAME carries the prefix (an
                # adopter hit it twice, 2026-09-28). The hint's own form is an
                # input now, as `add --target` and `context --target` take it.
                # A target literally named `item:x` still wins, and --type
                # given means the name was meant literally.
                print(f"note: read {args.name!r} as --type {t} "
                      f"--name {shlex.quote(n)}", file=sys.stderr)
                args.type, args.name = t, n
        # the stored form, so an abbreviation typed on read matches what a write
        # stored; a name the rows hold verbatim is itself
        args.type, name = gitref.read_target(cfg, args.type, args.name, every)
        # `--type file` alone in a store with [repos]: every repo's rows.
        # Rows still on `file` too: what is left to convert.
        ttype = (*cfg.repos, "file") if args.type == "file" and cfg.repos else args.type
        if args.since and args.since[1] is None:
            # A wrap writes its handoff from this: what the session did, as
            # the store holds it (wrap.md, step 3).
            if not (w := gitref.last_wrap(store_dir, cfg, side)):
                print("list: --since wrap: no wrap in this store yet; give a time "
                      "instead (2h, 2026-10-16)", file=sys.stderr)
                return 2
            args.since = (args.since[0], store.written_at(None, w["at"]))
        filt = dict(target_type=ttype, target_name=name,
                    kind=args.kind, tag=args.tag, arc_id=args.arc_id, author=args.author,
                    grep=grep, overdue=args.overdue or None,
                    since=args.since[1] if args.since else None,
                    due_by=args.due_by[1] if args.due_by else None)
        notes = store.query(base, status=args.status, **filt)
        if grep is not None:
            # An id is not text the pattern reads, so a cited tail read `0 of
            # N match` while `show` found its row (2026-10-07). As in the
            # GUI's search: a pasted id or tail joins the hits, and a
            # superseded one brings its current row. Each branch of a `|` is
            # its own tail: `tailA|tailB` returned only a row citing one
            # (2026-10-11). -F reads `|` as a character.
            from .gui import filters
            branches = [args.grep] if args.fixed_strings else args.grep.split("|")
            want = {i for n in every if any(filters.id_hit(n.id, b) for b in branches)
                    for i in (n.id, store.chain_tip(every, n.id).id)}
            got = {n.id for n in notes}
            notes += [n for n in store.query(base, status=args.status, **(filt | {"grep": None}))
                      if n.id in want and n.id not in got]
        if args.note_id and (missing := [i for i in args.note_id
                                         if i not in {n.id for n in notes}]):
            # An empty listing reads the same as "this row exists and matched
            # nothing else". You named an exact row; say it is not there.
            for i in missing:
                _no_note(i, store_dir)
            return 1
        notes = store.newest_first(notes)
        if args.note_id:      # in the order given: a script pairs its ids with the rows
            notes.sort(key=lambda n: args.note_id.index(n.id))
        total = len(notes)
        matched = {n.id for n in notes}
        # A literal pasted from a row (`$HOME`) never matches as a regex, and
        # its 0 read as absence. A plain word's 0 is a plain miss: no hint.
        ops = sorted(set(args.grep or "") & set(r"\.^$*+?{}[]()|"))
        if not total and args.name:
            # --name matches a whole name, while add takes a unique substring:
            # `--name` given the start of a longer item name read 0 beside a
            # dozen rows on it (an adopter, 2026-09-26).
            # A target with exactly that name was emptied by another filter,
            # which the footer names: `--status open` on a resolved target
            # read `no target is named 'zed'; these contain it: item:zed`.
            near = _containing(base, args.name, args.type)
            if near and not any(n.target.name == filt["target_name"]
                                and args.type in (None, n.target.type) for n in base):
                print(f"note: no target is named {args.name!r}; these contain it: {near} "
                      f"(--name matches a whole name)", file=sys.stderr)
        if not total and ops and not args.fixed_strings:
            # Only when -F would find something: a deliberate regex that
            # matches nothing is a plain miss, and the hint on it was noise.
            lit = re.compile(re.escape(args.grep), re.I | re.M)
            n_lit = len(store.query(base, status=args.status,
                                    **(filt | {"grep": lit})))
            if n_lit:
                print(f"note: --grep is a regex, and {shlex.quote(args.grep)} holds operators "
                      f"({' '.join(ops)}); as a literal (-F) it matches {n_lit}",
                      file=sys.stderr)
        if not total and args.fixed_strings and "|" in args.grep:
            # The other direction: -F takes every character literally, `|`
            # included, so a plain alternation reads 0 and looks like "nothing
            # filed yet" (an adopter, 2026-09-28). Only `|`: a literal pasted
            # from a row routinely holds `.`, `$` and `(`, and a hint on those
            # would fire on the reads -F exists for.
            try:
                n_re = len(store.query(base, status=args.status,
                                       **(filt | {"grep": re.compile(args.grep, re.I | re.M)})))
            except re.error:
                n_re = 0
            if n_re:
                print(f"note: -F took {shlex.quote(args.grep)} literally, '|' included; "
                      f"as a regex it matches {n_re}", file=sys.stderr)
        if not total and not args.fixed_strings and "\\|" in (args.grep or ""):
            # GNU grep's habit: in a Python regex `\|` is a literal pipe, so
            # `a\|b` read 0 where `a|b` matched (2026-10-26, twice).
            alt = args.grep.replace("\\|", "|")
            try:
                n_alt = len(store.query(base, status=args.status,
                                        **(filt | {"grep": re.compile(alt, re.I | re.M)})))
            except re.error:
                n_alt = 0
            if n_alt:
                print(f"note: --grep is a Python regex, where '\\|' is a literal pipe; "
                      f"as {shlex.quote(alt)} it matches {n_alt}", file=sys.stderr)
        # Text is bounded by default; --json only on an explicit --limit, so
        # the agent's array is never a silent page. 0 lifts the cap.
        cap = args.limit if args.limit is not None else \
            (None if args.json or args.note_id else summ.LIST_CAP)
        if cap:
            notes = notes[:cap]
        superseded = {n.supersedes for n in every if n.supersedes}
        heads_of = {n.id: store.chain_tip(every, n.id) for n in notes if n.id in superseded}
        given = []
        if not args.note_id:
            given = [f"--{flag} {shlex.quote(str(v))}" for flag, v in (
                ("type", args.type), ("name", args.name), ("kind", args.kind),
                ("status", args.status), ("tag", args.tag), ("arc", args.arc_id),
                ("author", args.author), ("side", args.side), ("grep", args.grep)) if v]
            if args.fixed_strings:
                given.append("-F")
            if args.overdue:
                given.append("--overdue")
            if args.since:
                given.append(f"--since {shlex.quote(args.since[0])}")
            if args.due_by:
                given.append(f"--due-by {shlex.quote(args.due_by[0])}")
        # One HEAD for the listing, not a read per check row.
        git_head = gitref.head_sha(cfg) if any(n.spec.verdict for n in notes) else None
        since = gitref.commits_since(cfg, notes)
        by_id = {n.id: n for n in every}
        if args.json:
            rows = []
            raw = {n.id: n for n in stored}
            for n in notes:
                st = api.verdict_state(cfg, n, git_head) if n.spec.verdict else None
                d = summ.json_row(raw[n.id], raw, since[n.id], st)
                if st:
                    if st[0] == "rewritten" and (s := gitref.successor(cfg, n.provenance,
                                                                       git_head)):
                        d |= {"successor": s.sha, "successor_state": s.state,
                              "same_tree": s.same_tree}
                if n.id in heads_of:
                    d["head"] = heads_of[n.id].id
                rows.append(d)
            print(json.dumps(rows))
            if len(rows) < total:
                print(f"showing {len(rows)} of {total}", file=sys.stderr)
            # A filtered array is a search: say, where stdout stays
            # parseable, what it hides -- the other statuses, and a row that
            # matches only in an older version (a resolve that replaced the
            # claim). An old version beside a head that matches too is noise.
            hidden = [h for h in (_hidden(scope, base, args, filt) if given else ())
                      if h.startswith("+") and not h.endswith("(--all)")]
            if given and not args.all:
                older = {store.chain_tip(scope, n.id).id
                         for n in store.query(scope, status=args.status, **filt)
                         if n.id in superseded} - matched
                if older:
                    hidden.append(f"{summ._count(len(older), 'entry')} "
                                  f"{'matches' if len(older) == 1 else 'match'} "
                                  f"only in an older version (--all)")
            if hidden:
                print(f"note: not in this array: {'; '.join(hidden)}", file=sys.stderr)
        else:
            paint = _painter(ctx.kinds)
            if not args.note_id:
                header = _list_header(total, len(notes), _hidden(scope, base, args, filt),
                                      scanned=len(base), filters=given)
                print(paint(header, "meta"))
                # `list | grep` deleted the header of a 25-of-38 page, and a miss
                # read as absence (dogfood, 2026-10-11). A person sees it as is,
                # and so does an agent whose shell gives both streams one file.
                if len(notes) < total and not sys.stdout.isatty() and not _one_stream():
                    print(f"note: {header}", file=sys.stderr)
            # One batched subjects() call for the whole listing rather than
            # one git-log per row -- gitref.subjects's whole reason to exist.
            shas = {n.target.name for n in notes
                   if n.target.type == "commit" and n.target.name}
            subjects = gitref.subjects(cfg, shas) if shas else {}
            gui = _gui(store_dir)
            # The reader as summary has it: at a terminal the person, who
            # has no one else's line to be shown.
            reader = None if sys.stdout.isatty() else api.author_default()
            # A superseded row's head carries its added lines, and a full body
            # shows them: `added` walks the chain, k versions cost k² clips.
            bare = args.full or bool(args.note_id)
            for n in notes:
                state = api.verdict_state(cfg, n, git_head) if n.spec.verdict else None
                subject = subjects.get(n.target.name) if n.target.type == "commit" else None
                succ = (gitref.successor(cfg, n.provenance, git_head)
                        if state and state[0] == "rewritten" else None)
                _print_note(n, state=state, subject=subject, successor=succ,
                            full=args.full or bool(args.note_id), head=heads_of.get(n.id),
                            gui=gui, since=since[n.id],
                            added=None if bare or n.id in heads_of
                            else summ.added(by_id, n, reader, summ.LIST_BODY_CHARS),
                            seams=summ.seams(by_id, n) if args.full or args.note_id else (),
                            links=summ.links(every, n) if args.note_id else (),
                            written=summ.written(by_id, n) if args.full or args.note_id else None,
                            side=_other_side(by_id, n, side))
            if len(notes) < total:
                # Where the eye stops: a 10-of-33 page read as the whole answer
                # (an adopter, 2026-09-26), the header notwithstanding.
                print("  " + paint(f"+{total - len(notes)} more not shown (--limit 0 for all)",
                                   "meta"))
        return 0

    if args.cmd == "resolve":
        # resolve is supersede with a fixed status; the body and tag merge are
        # the same plumbing supersede's combined --add-tag path uses.
        fields = {"status": "resolved", **_body_fields(args, append=True)}
        if args.result is not None:
            fields["result"] = args.result
        measures = _measures_from_flags(args.measures)
        if args.add_tags:             # onto the tip; a missing id raises below
            fields["tags"] = sorted(set(tip.tags if tip else ()) | set(args.add_tags))
        if tip is not None and store.read_status(tip) == "resolved" \
                and set(fields) == {"status"} and not measures and not args.refs:
            # It printed `already resolved`, then `edited in place` at exit 0,
            # which contradicted, and there was nothing to add (2026-10-09 review).
            print(tip.id)
            print("note: nothing to add, so nothing was written", file=sys.stderr)
            return 0
        try:
            note = api.supersede(ctx, args.id, author=_resolved_author(args),
                                 add_refs=_refs_from_flags(args.refs),
                                 add_measurements=measures, **fields)
        except KeyError:
            _no_note(args.id, store_dir)
            return 1
        print(note.id)
        args.wrote.add(note.id)
        _say_dropped(tip, note)
        _name_unknown_ids(store_dir, [args.body])
        _name_near_tags(store_dir, [note])
        _name_new_items(store_dir, [note], tip.refs if tip else ())
        if tip is not None and tip.spec.status and tip.spec.verdict \
                and store.read_status(tip) == "open":
            _name_dirty([note], external=False)   # a pre-registration's resolve is stamped anew
        if args.body:
            _name_long_text([(note.id, len(args.body.strip()))])
        return 0

    if args.cmd == "supersede":
        if args.external and tip is not None and not (tip.provenance or {}).get("external"):
            print(f"supersede: --external: {tip.id} is stamped with a commit, and a "
                  f"supersede keeps the stamp; a reading outside this repo is a new entry: "
                  f"add --external", file=sys.stderr)
            return 1
        fields = _body_fields(args)
        measures = _measures_from_flags(args.measures)
        if (opts := _options_from_flags(args.options)) is not None:
            fields["options"] = opts or None            # "" clears, as --due "" does
        if args.status is not None:
            fields["status"] = args.status
        if args.kind is not None:
            fields["kind"] = args.kind
        if args.checked is not None:
            fields["checked"] = args.checked
        if args.result is not None:
            fields["result"] = args.result
        if args.arc_id is not None:
            # "" detaches. `is not None` rather than truthiness, or detaching
            # would be the one edit this flag cannot express.
            fields["arc_id"] = args.arc_id or None
        if args.due is not None:
            fields["due"] = args.due or None          # "" clears, as --arc-id "" detaches
        if args.target is not None:
            fields["target"] = args.target
        if args.tags is not None and (args.add_tags or args.rm_tags):
            print("supersede: --tag replaces the tags, so --add-tag and --rm-tag beside it "
                  "would be dropped; nothing was written. Use --tag alone, or the other two",
                  file=sys.stderr)
            return 1
        if args.tags is not None:                      # --tag: replace
            fields["tags"] = args.tags
        elif (args.add_tags or args.rm_tags) and args.refs is None and not args.add_refs \
                and args.body is None and args.status is None and args.kind is None \
                and args.arc_id is None and args.checked is None and args.target is None \
                and args.result is None and args.due is None and not measures \
                and opts is None:
            try:
                note = api.retag(ctx, args.id, add=args.add_tags, rm=args.rm_tags,
                                 author=_resolved_author(args))
            except KeyError:
                _no_note(args.id, store_dir)
                return 1
            print(note.id)
            args.wrote.add(note.id)
            _say_dropped(tip, note)
            _name_near_tags(store_dir, [note])
            _echo_gotcha_notes([note], ctx.input_types)
            return 0
        elif args.add_tags or args.rm_tags:
            # combined with another field: merge inline against the tip, then
            # fall through to the single supersede below, so --add-tag --body
            # stays ONE row rather than two. A missing id leaves tip None,
            # and the store.supersede below raises it for "no note found".
            base = set(tip.tags) if tip else set()
            fields["tags"] = sorted((base | set(args.add_tags)) - set(args.rm_tags))
        if args.refs is not None:                      # --ref: replace, like --tag
            try:
                fields["refs"] = api.check_refs(          # `--ref ''` alone: none
                    ctx.input_types, _refs_from_flags([r for r in args.refs if r]), ctx.kinds)
            except ValueError as e:
                print(f"supersede: {e}", file=sys.stderr)
                return 1
        # Read before the write, as tip was: the head keeps the id.
        replacing = args.body is not None and not (args.append or args.prepend)
        by_id = {n.id: n for n in store.load(store_dir)} if replacing and tip else None
        try:
            # api.supersede, not store.supersede: the api door is what refuses
            # a provenance in **fields, and what resolves a target and the
            # refs under the lock. check_refs above is for the error message only;
            # the raw query must reach the lock, or a name that became
            # ambiguous since would resolve by exact match.
            # --ref read as adding one dropped the rest, more than once
            # by 2026-10-15; a leading '' says the replace is meant.
            note = api.supersede(ctx, args.id, author=_resolved_author(args),
                                 add_refs=_refs_from_flags(args.add_refs),
                                 add_measurements=measures,
                                 keep_refs=args.refs is not None and "" not in args.refs,
                                 **fields)
        except KeyError:
            _no_note(args.id, store_dir)
            return 1
        except store.RefsDropped as e:
            print(f"supersede: --ref replaces the refs, and would drop "
                  f"{', '.join(summ.ref_label(r) for r in e.dropped)}; nothing was written. "
                  f"--add-ref adds one and keeps the rest; to replace them, lead with "
                  f"--ref ''", file=sys.stderr)
            return 1
        print(note.id)
        args.wrote.add(note.id)
        if args.arc_id:
            _name_archived_arcs(store_dir, [note])
        _name_unknown_ids(store_dir, [args.body])
        _name_near_tags(store_dir, [note])
        _name_new_items(store_dir, [note], tip.refs if tip else ())
        _say_dropped(tip, note)
        _name_dropped(tip, note, refs=args.refs is not None, tags=args.tags is not None)
        if by_id is not None:
            _say_body_dropped(store_dir, by_id, tip, note)
        # --append keeps the lead it adds after, so the note asked about text
        # the write did not touch (3 times, 2026-10-01). Unless there was none.
        if args.body is not None and not (args.append and tip is not None and tip.body):
            _echo_clipped_heads(store_dir, [note])
        _echo_gotcha_notes([note], ctx.input_types)
        if args.body:
            _name_long_text([(note.id, _text_added(tip.body or "", note.body or "")
                              if replacing and tip else len(args.body.strip()))])
        return 0

    if args.cmd == "commit" and args.dry_run:
        # A careful commit read `git diff` by hand first: `commit` takes every
        # writer's pending rows (dogfood, 2026-10-06).
        rows = store.pending_rows(store_dir)
        keep = (lambda r: True) if args.everyone else \
            store.with_bases(rows, api.own_rows(ctx))
        mine, left = [r for r in rows if keep(r)], [r for r in rows if not keep(r)]
        # Each file, as `commit` names them: status shows an untracked dir as one.
        changed = ["diff", "HEAD", "--name-only", "-z"] if gitref._git(
            store_dir, "rev-parse", "-q", "--verify", "HEAD").returncode == 0 \
            else ["ls-files", "-z", "--cached"]
        files = sorted({f for cmd in (changed, ["ls-files", "-z", "--others",
                                                "--exclude-standard"])
                        for f in gitref._git(store_dir, *cmd).stdout.split("\0")
                        if f and f != store.NOTES_FILE})
        def listed(rows):
            for r in rows:
                print(f"  {(r.get('id') or '?')[-10:]}  {r.get('author') or '?'}  "
                      f"{r.get('kind') or '?'}  {summ.clip(r.get('body') or '', 60)}".rstrip())
        said = ([f"entries {store.rows_by_author(mine)}"] if mine else []) + \
            ([f"{summ._count(len(files), 'file')}"] if files else [])
        print("would commit: " + "; ".join(said) if said else "nothing to commit")
        listed(store.entries(mine))
        for f in files:
            print(f"  {f}")
        if left:
            print(f"would leave, {_others()}': entries {store.rows_by_author(left)}")
            listed(store.entries(left))
        _say_ignored_archive(store_dir, would=True)
        return 0

    if args.cmd == "commit":
        if args.message == "-":
            # A heredoc on stdin: inside `-m "$(cat <<'EOF' …)"`, bash 3.2
            # broke on one apostrophe (2026-10-13).
            args.message = sys.stdin.read()
            if not args.message.strip():
                print("error: -m - read nothing but whitespace on stdin, so nothing was "
                      "committed", file=sys.stderr)
                return 2
        if args.wrap and args.message is None:
            print("error: --wrap needs -m: the message is the handoff the next session "
                  "reads", file=sys.stderr)
            return 2
        ok = api.commit(ctx, args.message or "update entries", everyone=args.everyone,
                        wrap=args.wrap)
        print(gitref.committed(store_dir) if ok else "nothing to commit")
        # A row left here may be a live session's, or one a session left
        # when it ended: the newest's age tells them apart.
        left = store.pending_rows(store_dir)
        if left:
            newest = max(r.get("created_at") or "" for r in left)
            cmd = " ".join(filter(None, ["symbion", _named_store(ctx), "commit", "--all"]))
            print(f"left pending, {_others()}': entries {store.rows_by_author(left)}"
                  + (f", the newest written {summ.when_added(newest)}" if newest else "")
                  + f": {cmd} takes them")
        _say_ignored_archive(store_dir)
        # The second half of the footgun: committed is not off this disk.
        # Measured 2026-09-22, the first session with an origin on the
        # store -- "committed" read as done and the commit sat unpushed.
        n = gitref.unpushed(store_dir)
        if n is None:
            # Silence here read the same as a backed-up store (2026-09-23),
            # unless the store says one disk is the plan.
            if not ctx.cfg.local_only:
                print("no remote: this store exists on one disk")
        elif ok and store.shared(store_dir):
            _pull_then_push(ctx)
        elif n:
            push = " ".join(filter(None, ["symbion", _named_store(ctx), "push"]))
            print(f"{n} commit{'' if n == 1 else 's'} not on origin: {push}")
        return 0 if ok else 1

    if args.cmd == "push":
        if gitref.unpushed(store_dir) is None:
            print("no remote: this store exists on one disk", file=sys.stderr)
            return 1
        rc = api.push(ctx).returncode
        notes, registry = gitref.uncommitted(store_dir)
        bits = []
        if notes:
            bits.append(summ._count(notes, "entry"))
        if registry:
            bits.append("arc changes")
        if bits:
            print(f"not pushed: {' and '.join(bits)} not yet in the store's git "
                  f"({_commit_hint(ctx)})", file=sys.stderr)
        return rc

    if args.cmd == "pull":
        return _say_pulled(ctx, api.pull(ctx))

    if args.cmd == "trust":
        return _trust(ctx)

    if args.cmd == "rename":
        # A type the store no longer declares still names its rows: rename
        # is how they leave it (the repos design, "Converting a store").
        held = {o.type for n in store.load(store_dir) for o in (n.target, *n.refs)}
        try:
            _catalog_choice(target_types | held, "type", store_dir, ctx.kinds)(args.type)
        except argparse.ArgumentTypeError as e:
            print(f"rename: argument --type: {e}", file=sys.stderr)
            return 2
        if (args.to_type or args.type) not in target_types:
            # A held type the store dropped: its entries can only leave it.
            # Renaming within it said `unknown target type` (the repos review).
            repos = getattr(ctx.cfg, "repos", None)
            print(f"rename: this store declares no {args.type!r} type: give --to-type "
                  + (f"REPO ({', '.join(repos)})" if repos else "TYPE")
                  + " to move its entries out of it", file=sys.stderr)
            return 2
        if args.new is None:
            to = args.to_type or args.type
            if args.old is not None or to == args.type:
                print("rename: give OLD and NEW, or neither with --to-type TYPE to move "
                      f"every entry of {args.type} to TYPE, names as stored", file=sys.stderr)
                return 2
            before = {n.id for n in store.load(store_dir)}
            moved, refs = api.retype(ctx, args.type, to, author=_resolved_author(args))
            _name_sweep_drops(store_dir, before)
            print(f"re-targeted {summ._count(moved, 'entry')}, re-pointed "
                  f"{summ._count(refs, 'ref')}: every {args.type}:NAME -> {to}:NAME")
            return 0 if moved or refs else 1
        to = args.to_type or args.type
        before = {n.id for n in store.load(store_dir)}
        moved, refs, new = api.rename_target(ctx, args.type, args.old, args.new,
                                             author=_resolved_author(args), to_type=to)
        _name_sweep_drops(store_dir, before)
        print(f"re-targeted {summ._count(moved, 'entry')}, re-pointed "
              f"{summ._count(refs, 'ref')}: {args.type}:{args.old} -> {to}:{new}")
        if moved or refs:
            commit = args.type == "commit"
            _name_stale_bodies(store_dir, args.old[:7] if commit else args.old, prefix=commit)
            return 0
        # `old` is matched exactly, and a short sha read as "0 notes" beside
        # the row stored on the full one (2026-09-26). Name the stored names.
        names = sorted({o.name for n in store.heads(store.load(store_dir))
                        for o in (n.target, *n.refs)
                        if o.type == args.type and o.name})
        near = ([x for x in names if args.old and args.old.lower() in x.lower()]
                or difflib.get_close_matches(args.old, names, n=3, cutoff=0.65))
        if near:
            shown = ", ".join(f"{args.type}:{x}" for x in near[:api._HINT_CAP])
            more = f", +{len(near) - api._HINT_CAP} more" if len(near) > api._HINT_CAP else ""
            print(f"note: rename matches a stored name exactly; stored: {shown}{more}",
                  file=sys.stderr)
        return 1

    if args.cmd == "serve":
        try:
            from .gui import serve as gui_serve
        except ImportError as e:
            # Only the MISSING EXTRA gets the install instruction. A broken
            # import inside gui/ is also an ImportError, and answering it with
            # "pip install symbion[gui]" sends the reader to reinstall an
            # extra they already have while the real traceback is swallowed.
            if "nicegui" not in f"{getattr(e, 'name', '')} {e}":
                raise
            print("serve needs the gui extra: pip install 'symbion[gui]'",
                  file=sys.stderr)
            return 1
        return gui_serve.main(ctx, author=args.author or api.gui_author(),
                              port=args.port, show=not args.no_browser,
                              reload=args.reload, argv=args.argv,
                              host=args.host, allow=args.allow, read_only=args.read_only)

    if args.cmd == "tags":
        counts = store.tag_counts(store.load(store_dir))
        if not counts:
            print("no tags yet (add --tag NAME)")
        paint = _painter(ctx.kinds)
        for t, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"{paint(f'{cnt:5}', 'meta')}  {paint(t, 'text')}")
        return 0

    if args.cmd == "summary":
        if not store.exists(store_dir):   # text mode never gets here: see _READS
            print(json.dumps(summ.empty_summary()))
            return 0
        at_terminal = sys.stdout.isatty() and not (args.json or args.hook)
        read_at = store._now_iso() if args.hook else None
        deadline = time.monotonic() + HOOK_DEADLINE
        reader = None if at_terminal else api.author_default()
        sync, told = _hook_pull(ctx) if args.hook else ([], [])   # before the summary reads it
        if args.hook and (note := _untrusted_note(ctx)):          # each session until `trust`
            sync, told = [*sync, note], [*told, note]
        side = store.read_side(store_dir)      # after the pull: it can bring `shared`
        data = summ.summary(store_dir, cfg, full=args.full, reader=reader, side=side)
        data["sync"] = sync
        # A $SYMBION_AUTHOR left exported changes whose view this is: named,
        # unless it is the shell's agent's own name, as Codex's hook sets it.
        data["reader"] = reader if reader and reader != api.shell_author() else None
        # Both links lead to the same skill directory, so either serves.
        data["skill"] = next((f"~/{link.as_posix()}/SKILL.md" for link in (_SKILL_LINK, _CODEX_SKILL_LINK)
                              if (Path.home() / link / "SKILL.md").exists()), None)
        if os.environ.get("HERMES_AGENT") and (
                data["skill"] or (_hermes_home() / "skills/symbion/SKILL.md").exists()):
            # Hermes lists skills by name and loads one with skill_view: a
            # model read the path as where the skill loads from (a tester,
            # 2026-10-13).
            data["skill"] = "skill_view symbion"
        if at_terminal:
            data["skill"] = None      # the line is for an agent; the hook reads a pipe
        data["named_store"] = _named_store(ctx)
        data["commit_hint"] = _commit_hint(ctx) if data["uncommitted_notes"] else None
        if args.hook:
            sid = _hook_session()
            try:
                if sid:
                    _list_gotchas(ctx, sid, deadline)
                    _mark_read(ctx, sid, read_at)
            except OSError:             # a cache it cannot write costs the hint, not the summary
                pass
            text = summ.render_summary(summ.fit(data))
            note = _unknown_keys_note(ctx)
            print(_hook_json(f"{text}\n{note}" if note else text, summ.one_line(data, told)))
        else:
            print(json.dumps(data) if args.json else summ.render_summary(
                data, _painter(ctx.kinds, data.get("gui")), person=at_terminal))
        return 0

    if args.cmd == "schema":
        if args.toml:
            print(K.render_toml(ctx.kinds), end="")
            return 0
        data = summ.schema(store_dir, cfg, ctx.kinds)
        print(json.dumps(data) if args.json else summ.render_schema(data, _painter(ctx.kinds)))
        return 0

    if args.cmd == "context":
        if args.commit and gitref.peel(cfg, args.commit) is None:
            # A departed commit keeps its rows, so this is a note, not an error.
            where = "in any listed repo" if cfg.repos else "here"
            print(f"note: git does not know commit {args.commit!r} {where}; matched as typed",
                  file=sys.stderr)
        data = summ.context(store_dir, cfg, target=args.target, commit=args.commit,
                            branch=args.branch, since=args.since)
        if args.json:
            print(json.dumps(data))
        else:
            # A target with no notes printed nothing on stdout (2026-09-22).
            # The first line carries the count and its subject; the bare view
            # names the heads it leaves out, so its 0 is not an empty store.
            head = summ._count(len(data["notes"]), "entry")
            if args.target:
                head += f" for {data['target']}"
            elif args.commit:
                head += f" for commit {args.commit}"
            elif args.branch:
                head += f" for branch {args.branch}" + (f" since {args.since}" if args.since else "")
            side = store.read_side(store_dir)
            every = summ.seen_from(store.load(store_dir), side)
            by_id = {n.id: n for n in every}
            if (data.get("target") or "").startswith("arc:"):
                # A box is in an arc by its arc_id, not its target: `0 entries
                # for arc:X` read as an empty arc (dogfood, 2026-10-28).
                arc, shown = data["target"][4:], {d["id"] for d in data["notes"]}
                k = sum(n.arc_id == arc and n.id not in shown for n in store.heads(every))
                if k:
                    head += (f"; +{summ._count(k, 'entry')} filed in the arc (list --arc {arc}; "
                             f"arc todo {arc} lists its open boxes)")
            if not (args.target or args.commit or args.branch):
                other = len(store.heads(every)) - len(data["notes"])
                if other:
                    head += f"; +{summ._count(other, 'other entry')} (list)"
            print(_painter(ctx.kinds)(head, "meta"))
            gui = _gui(store_dir)
            for d in data["notes"]:
                row = by_id.get(d["id"])
                _print_note(store.note_from_dict(d, kinds=ctx.kinds), gui=gui,
                            since=d["commits_since"],
                            state=(d["state"], d["distance"]) if "state" in d else None,
                            written=summ.written(by_id, row) if row else None,
                            side=_other_side(by_id, row, side) if row else None)
        return 0

    if args.cmd == "arc":
        return _dispatch_arc(args, ctx)

    return 0


def _dispatch_arc(args, ctx) -> int:
    store_dir, cfg = ctx.store_dir, ctx.cfg
    if args.acmd == "create":
        # A second arc by one name read as the first in the summary (dogfood, 2026-10-28).
        twins = [a for a in store.load_arcs(store_dir) if a.name.casefold() == args.name.casefold()]
        act = api.create_arc(ctx, args.name, args.desc, args.scope,
                                  author=_resolved_author(args))
        print(act.id)
        _name_unknown_ids(store_dir, [args.desc], "--desc")
        notes = store.load(store_dir) if twins else ()
        for a in twins:
            done, total = store.arc_progress(notes, a.id)
            print(f"note: arc {a.id} is named {a.name!r} too ({done}/{total}"
                  f"{', archived' if a.archived else ''}); arc rename {act.id} NAME "
                  f"tells them apart", file=sys.stderr)
        return 0

    if args.acmd == "list":
        notes = store.load(store_dir)
        rows, hidden = [], 0
        for a in store.load_arcs(store_dir):
            if a.archived and not args.all:
                hidden += 1
                continue
            done, total = store.arc_progress(notes, a.id)
            # The arc whole: --json dropped its dates, scope and author (2026-10-28).
            rows.append(store.arc_to_dict(a) | {"done": done, "total": total})
        if args.json:
            print(json.dumps(rows))
        else:
            paint = _painter(ctx.kinds)
            if not rows:
                print(paint("0 arcs (arc create --name NAME --scope item|file|mixed|project)",
                            "meta"))
            w = max((len(r["id"]) for r in rows), default=0)
            for r in rows:
                desc = paint(f"  -- {r['description']}", "meta") if r["description"] else ""
                prog = paint(f"{r['done']}/{r['total']}",
                             "good" if r["done"] == r["total"] else "meta")
                when = f" {r['archived_at'][:10]}" if r.get("archived_at") else ""
                gone = paint(f"  (archived{when})", "meta") if r["archived"] else ""
                print(f"{paint(r['id'].ljust(w), 'text')} {prog}  "
                      f"{paint(r['name'], 'body')}{gone}{desc}")
            if hidden:
                print(paint(f"+{hidden} archived (arc list --all)", "meta"))
        return 0

    # The id prints either way; a no-op read as a change (dogfood, 2026-10-28).
    was = next((a for a in store.load_arcs(store_dir) if a.id == getattr(args, "id", None)), None)
    if args.acmd == "rename":
        try:
            api.rename_arc(ctx, args.id, args.new_name)
        except KeyError:
            _no_arc(store_dir, args.id)
            return 1
        print(args.id)                # every other write prints; silence read as "did nothing"
        if was.name == args.new_name:
            print(f"note: {args.id} is named {args.new_name!r} already; nothing changed",
                  file=sys.stderr)
        return 0

    if args.acmd == "archive":
        try:
            api.archive_arc(ctx, args.id)
        except KeyError:
            _no_arc(store_dir, args.id)
            return 1
        print(args.id)
        if was.archived:
            when = f" {store.shown(was.archived_at)}" if was.archived_at else ""
            print(f"note: {args.id} was archived{when}; nothing changed", file=sys.stderr)
            return 0
        done, total = store.arc_progress(store.load(store_dir), args.id)
        if total > done:
            print(f"note: {summ._count(total - done, 'open entry')} in {args.id}, now listed "
                  f"outside arcs (list --arc {args.id} --status open)", file=sys.stderr)
        return 0

    if args.acmd == "seed":
        # No try/except here: catalog.AmbiguousName subclasses ValueError, and
        # catching ValueError at this site would intercept it before it
        # reaches main()'s dedicated (catalog.CatalogError, catalog.AmbiguousName)
        # clause, printing "seed: {e}" instead of the old "error: {e}". Both
        # that and the bare SystemExit below must propagate to main() exactly
        # as they did through the old cli._seed_names, uncaught here.
        if args.scope is None:
            # The arc's own scope when seedable, else item for named steps.
            own = next((a.target_scope for a in store.load_arcs(store_dir)
                        if a.id == args.id), None) if args.id else None
            args.scope = (own if own in ctx.seed_scopes
                          else "item" if args.names else None)
            if args.scope is None:
                print(f"arc seed: name --scope (one of {', '.join(sorted(ctx.seed_scopes))}); "
                      f"this arc's own scope, {own}, has no catalog to sweep", file=sys.stderr)
                return 2
        if args.scope == "item" and not args.names:
            raise SystemExit("item scope requires at least one --name: it has no "
                             "catalog. Other scopes sweep a command configured "
                             "under [catalogs] in symbion.toml")
        if args.dry_run:
            # Before the id check on purpose: a catalog's first dry-run is how
            # you learn what it emits, and that should not need a write -- nor
            # an id, which is optional here for that reason (2026-09-23). The
            # preview resolves explicit names unlocked; a sweep is the catalog.
            names = api.seed_names(ctx, args.scope, args.names)
            print(f"would seed {len(names)}:")
            print("\n".join(names))
            return 0
        if args.id is None:
            raise SystemExit("arc seed needs an arc id (arc list); only --dry-run runs without one")
        if not any(a.id == args.id for a in store.load_arcs(store_dir)):
            _no_arc(store_dir, args.id)
            return 1
        # Raw names in: a sweep (none) is the catalog verbatim; explicit ones
        # resolve under the store lock (api.seed).
        made = api.seed(ctx, args.id, args.scope, args.names,
                        author=_resolved_author(args), kind=args.kind)
        for n in made:                    # ids on stdout, as add prints them
            print(n.id)
            args.wrote.add(n.id)
        print(f"seeded {len(made)} new {args.kind}(s)", file=sys.stderr)
        _name_archived_arcs(store_dir, made)
        return 0

    if args.acmd == "todo":
        if not any(a.id == args.id for a in store.load_arcs(store_dir)):
            _no_arc(store_dir, args.id)
            return 1
        notes = store.load(store_dir)
        items = store.arc_items(notes, args.id)
        open_items = [n for n in items if store.read_status(n) == "open"]
        resolved = sum(1 for n in items if store.read_status(n) == "resolved")
        # Insertion order: a hand-written checklist keeps its step order, and a
        # seeded one keeps its catalog's order, which was already sorted.
        rows = sorted(open_items, key=store.written_at)
        if args.json:
            # Same shape as `list --json`; a consumer written against one
            # must not KeyError on the other. `reconcile --json` stays flat
            # because its rows are a report, not notes.
            since = gitref.commits_since(cfg, rows)
            by_id = {n.id: n for n in notes}
            print(json.dumps([summ.json_row(n, by_id, since[n.id], st)
                              for n, st in summ.verdict_states(cfg, rows)]))
        else:
            # A finished arc printed nothing at exit 0, the same as one nobody
            # had started (2026-09-22). Count with denominator; the hidden set
            # named with the verb that shows it, as list's header does.
            head = f"{len(rows)} open of {summ._count(len(rows) + resolved, 'item')}"
            if resolved:
                head += f"; +{resolved} resolved (list --arc {args.id})"
            paint = _painter(ctx.kinds)
            print(paint(head, "meta"))
            # The session-start head line: many boxes share one target, so
            # target and id alone could not say which box a line was.
            for n in rows:
                print(summ.row_line(
                    paint, n.kind,
                    f"{n.target.type}:{summ.clip(n.target.name, summ.NAME_CHARS)}",
                    summ.clip(n.body, summ.HEAD_CHARS), n.id))
        return 0

    if args.acmd == "reconcile":
        if not any(a.id == args.id for a in store.load_arcs(store_dir)):
            _no_arc(store_dir, args.id)
            return 1
        notes = store.load(store_dir)
        ctx.consulted.add("catalog")
        renames = {t: catalog.rename_map(cfg, t) for t in cfg.renames}
        rows = store.reconcile_arc(
            notes, args.id, lambda t: set(catalog.names(cfg, t)),
            renames, set(cfg.catalogs))
        c = Counter(r["status"] for r in rows)
        # --json: stdout is the report alone, what was found before any
        # --apply; everything else goes to stderr so the array still parses.
        say = (lambda *a: print(*a, file=sys.stderr)) if args.json else print
        if args.json:
            print(json.dumps(rows))
        else:
            # What it could check, first: `9 open task(s): 9 uncheckable` on an
            # arc of items read as a clean pass while it checked nothing (an
            # adopter, 2026-09-26).
            checked = len(rows) - c["uncheckable"]
            seen = ", ".join(f"{c[k]} {k}" for k in ("live", "renamed", "stale") if c[k])
            head = summ._count(len(rows), "open task")
            if rows:
                head += f"; {checked} of {len(rows)} checkable" + (f": {seen}" if seen else "")
            if c["uncheckable"]:
                why = ", ".join(sorted({r["target_type"] for r in rows
                                        if r["status"] == "uncheckable"}))
                head += (f"; {c['uncheckable']} uncheckable" if checked else "") + \
                    f" ({why}: no catalog)"
            paint = _painter(ctx.kinds)
            print(paint(head, "meta"))
            for r in rows:
                if r["status"] not in ("renamed", "stale"):
                    continue
                tgt = paint(r["target_type"] + ":", "meta") + paint(r["target_name"], "text")
                if r["status"] == "renamed":
                    print(f"  {paint('RENAMED', 'warn')}  {tgt}  ->  "
                          f"{paint(r['suggestion'], 'good')}")
                elif r["status"] == "stale":
                    need = "; needs --result" if r["needs_result"] else ""
                    print(f"  {paint('STALE', 'bad')}    {tgt}"
                          f"  {paint(f'(no {cfg.stale_target_noun}{need})', 'meta')}")
        if args.apply or args.resolve_stale:
            before = {n.id for n in store.load(store_dir)}
            rt, rp, rs = store.apply_reconciliation(store_dir, rows,
                                                    resolve_stale=args.resolve_stale,
                                                    author=_resolved_author(args))
            _name_sweep_drops(store_dir, before)
            skipped = (sum(1 for r in rows if r["status"] == "stale" and r["needs_result"])
                       if args.resolve_stale else 0)
            say(f"applied: {rt} re-targeted and {rp} re-pointed (renamed, whole store), "
                f"{rs} resolved (stale), {skipped} skipped (needs --result)")
        elif c["renamed"] or c["stale"]:
            say("  (--apply to re-target renamed, --resolve-stale to also close stale)")
        return 0

    return 0


# ---- shell completion ----
def _complete() -> None:
    """Answer a shell's TAB press and exit, through argcomplete. It runs this
    store's own parser, so kinds, types and scopes complete as the store
    declares them; ids, arcs, tags and target names come from its rows. The
    shell runs `symbion` bare with the line in COMP_LINE, so a --dir on that
    line names the store. With no store to read, verbs and flags still
    complete: a TAB press has nowhere to print an error."""
    import argcomplete
    line = os.environ.get("COMP_LINE", "")
    try:
        words = shlex.split(line)
    except ValueError:                           # an unclosed quote mid-word
        words = line.split()
    try:
        dir_value, _ = _extract_dir(words[1:])
        ctx = api.resolve(dir_value and os.path.expanduser(dir_value))
    except (SystemExit, Exception):              # `--dir` with no value, no repo
        ctx = api.resolve(os.getcwd(), follow_owner=False)
    parser = _build_parser(ctx.target_types, ctx.arc_scopes,
                           ctx.seed_scopes, ctx.store_dir, ctx.kinds, ctx.input_types)

    @functools.cache
    def heads():
        return store.newest_first(store.heads(store.load(ctx.store_dir)))

    def rows(only_open):
        def complete(**_):
            return {n.id: f"[{n.kind}] {n.target.type}"
                          f"{':' + n.target.name if n.target.name else ''}  "
                          f"{summ.clip(n.body, summ.HEAD_CHARS)}"
                    for n in heads() if not only_open or store.read_status(n) == "open"}
        return complete

    def arcs(**_):
        return {a.id: a.name for a in store.load_arcs(ctx.store_dir) if not a.archived}

    def tags(**_):
        counts = store.tag_counts(heads())
        return {t: summ._count(counts[t], "entry") for t in sorted(counts, key=counts.get, reverse=True)}

    def targets(prefix, **_):
        typ, colon, _ = prefix.partition(":")
        if not colon:
            return [t if t == "project" else f"{t}:" for t in sorted(ctx.target_types)]
        names = {o.name for n in heads() for o in (n.target, *n.refs)
                 if o.type == typ and o.name}
        return [f"{typ}:{name}" for name in sorted(names)]

    def choices(values):
        return lambda **_: values

    def attach(p):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for sp in a.choices.values():
                    attach(sp)
            elif a.dest in ("id", "note_id"):
                a.completer = (arcs if p.prog.startswith("symbion arc ")
                               else rows(only_open=p.prog == "symbion resolve"))
            elif a.dest == "arc_id":
                a.completer = arcs
            elif a.dest in ("tag", "tags", "add_tags", "rm_tags"):
                a.completer = tags
            elif a.dest in ("target", "refs"):
                a.completer = targets
            elif a.dest in ("kind", "kind_pos"):
                seed = p.prog == "symbion arc seed"     # a status kind, neither parked nor verdict
                a.completer = choices({k: s.when for k, s in ctx.kinds.items()
                                       if not seed or s.status and not s.parked and not s.verdict})
            elif a.dest == "dir":
                a.completer = argcomplete.DirectoriesCompleter()
            elif a.dest in ("body_file", "from_json"):
                a.completer = argcomplete.FilesCompleter()
            elif getattr(a.type, "choices", None):
                a.completer = choices(a.type.choices)
    attach(parser)
    # Flags only after a `-`, so `add <TAB>` lists kinds, not kinds and every
    # flag; a free-text value (--body, --grep) completes nothing.
    argcomplete.autocomplete(parser, always_complete_options=False,
                             default_completer=argcomplete.SuppressCompleter())


def main(argv=None) -> int:
    """A reader that closes the pipe early (`list | head -1`) asked for less,
    not for a Python error. The write fails inside print, or at the
    interpreter's last flush after main returns, so that flush happens here."""
    # Windows gives a pipe the ANSI code page (cp1252): `list` exited 1 on a
    # row holding `→`, which cp1252 lacks, and a body piped to --body-file -
    # was stored garbled. Whoever reads symbion reads UTF-8.
    for s in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    try:
        store._now_iso()            # a bad SYMBION_NOW stops every command
    except store.BadClock as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        code = _main(argv)
        if sys.stdout is not None:               # None when fd 1 was closed
            sys.stdout.flush()
        return code
    except FileNotFoundError:
        # A shell sat in a directory that was then removed, or removed and
        # made again: git exits 128 there, and os.getcwd() raises this, also
        # inside the not-a-repo message. It printed as a traceback.
        try:
            os.getcwd()
        except FileNotFoundError:
            print("error: the current directory no longer exists: it was removed after the "
                  "shell entered it. If it was made again, `cd .` enters the new one",
                  file=sys.stderr)
            return 1
        raise
    except OSError as e:
        # Windows reports the closed pipe as EINVAL with no file named; a bad
        # path's EINVAL names one.
        if not (isinstance(e, BrokenPipeError)
                or (os.name == "nt" and e.errno == errno.EINVAL and e.filename is None)):
            raise
        # The interpreter flushes stdout once more at exit: devnull takes it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141                               # 128 + SIGPIPE, as a shell reports it


def _main(argv) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if "_ARGCOMPLETE" in os.environ:
        _complete()                              # argcomplete exits the process
    # git runs it from the store's root, with three temp files: no store to
    # resolve, no parser, no `--help` entry.
    if argv[:1] == ["merge-driver"]:
        if len(argv) != 5:
            print("usage: symbion merge-driver BASE OURS THEIRS PATH (git runs it, see "
                  "merge.symbion.driver in the clone's .git/config)", file=sys.stderr)
            return 2
        return merge.driver(Path(argv[1]), Path(argv[2]), Path(argv[3]), argv[4])
    ctx = None
    try:
        dir_value, rest = _extract_dir(argv)
        if rest[:1] in (["-V"], ["--version"]):
            print(_version())
            return 0
        # `init` installs into the cwd's project, so it never follows a
        # named store to the project that owns it. `completion` runs from a
        # shell's rc file, wherever the shell starts, and reads no store: the
        # cwd stands in as one, as for `-h` below. So does `serve --restart`
        # and `--stop`, which act on every store's serve.
        storeless = (rest[:1] == ["completion"]
                     or rest[:1] == ["serve"] and bool({"--restart", "--stop"} & set(rest)))
        if storeless:
            dir_value = os.getcwd()
        no_store = None
        try:
            ctx = api.resolve(dir_value, follow_owner=rest[:1] != ["init"] and not storeless)
        except subprocess.CalledProcessError as e:
            # Help needs no store, but argparse reaches `-h` only after the
            # store resolves, so outside a repo every `--help` exited 1 with
            # no usage -- including `init --help`, read from the parent
            # directory before creating the store (measured 2026-09-28).
            # The cwd stands in as a named store: it makes a Ctx with the
            # built-in types and kinds, and no repo to derive one from is
            # exactly when there is nothing store-specific to list.
            if not {"-h", "--help"} & set(rest):
                raise
            no_store = e
            ctx = api.resolve(os.getcwd(), follow_owner=False)
        # The hook merges stderr into its JSON: summary --hook puts it in the text.
        if (note := _unknown_keys_note(ctx)) and not (rest[:1] == ["summary"] and "--hook" in rest):
            print(note, file=sys.stderr)
        parser = _build_parser(ctx.target_types, ctx.arc_scopes,
                               ctx.seed_scopes, ctx.store_dir, ctx.kinds, ctx.input_types)
        # Agents typed `show <id>` from CLI habit and read the invalid-choice
        # error as "no such command" (2026-09-22..24).
        if rest[:1] == ["show"] and not {"-h", "--help"} & set(rest[1:]):
            # A tail cited as `-a1b`, as symbion prints it, read as a flag, and
            # `show -- 22b` as `list --id --`: the first printed nothing and
            # exited 0 (the user, 2026-10-09). Both are ids here.
            ids = [store.id_tail(a) if _DASHED_ID.fullmatch(a) else a
                   for a in rest[1:] if a != "--"]
            # Any other flag reaches show's parser, which refuses it: `show ID
            # --grep zzz` ran as a list filter and said `no note found` (2026-10-14).
            flags = [a for a in ids if a.startswith("-")]
            # `--full` as list takes it: show prints bodies whole, and it
            # exited 2 on a list habit (a trial, 2026-10-26).
            if len(flags) < len(ids) and set(flags) <= {"--json", "--all", "--full"}:
                # `show --json ID`, the flag first, read as `list --id --json ID`.
                rest = ["list", *flags, "--id", *(a for a in ids if a not in flags)]
        # grep's -i carried over: --grep is case-insensitive already, and
        # `--grep -i PATTERN` read -i as a flag and refused (seen
        # repeatedly, 2026-10-14).
        if "--grep" in rest:
            rest = [a for a in rest if a != "-i"]
        args = parser.parse_args(rest)
        args.argv = argv                         # serve --reload re-runs it
        args.wrote = set()                       # a write's rows, for _say_news
        if no_store is not None:
            raise no_store                       # `-h` was a value, not help
        # A command reads each repo's HEAD once (gitref.heads_once). Not serve:
        # its requests would keep this one scope for the process's life, so
        # each of its pages opens its own.
        scope = contextlib.nullcontext() if args.cmd == "serve" else gitref.heads_once()
        try:
            with scope:
                rc = _dispatch(args, ctx)
        finally:
            # Only where that repo decided something (api.Ctx.consulted): on
            # every read, then every write, it was noise, so a real mismatch
            # was skipped with it (2026-10-01, 2026-10-12). Last, so a refusal
            # it explains prints above it.
            if ctx.followed_owner and ctx.consulted:
                print(f"symbion: {ctx.store_dir} belongs to {ctx.cfg.work_root}; catalogs "
                      f"and check state resolve there, not in {os.getcwd()}", file=sys.stderr)
            elif ("stamp" in ctx.consulted and ctx.tree_default not in (None, ctx.store_dir)
                  and not getattr(ctx.cfg, "repos", None)):
                # Named, and no project claims it: a check written from
                # another repo took that repo's HEAD, silently (a trial,
                # 2026-10-26). Only the stamp: a scratch store probing this
                # tree's catalog is the point of one.
                print(f"symbion: no project claims {ctx.store_dir}, so this entry is "
                      f"stamped with the HEAD of {ctx.cfg.work_root}, where you stand; a "
                      f".symbion naming the store in its project's root claims it",
                      file=sys.stderr)
        # News needs the ids a write made; origin's count needs none, and an
        # arc write ended without it (dogfood, 2026-10-28).
        row_write = args.cmd in ("add", "resolve", "supersede") or \
            args.cmd == "arc" and args.acmd == "seed" and bool(args.wrote)
        write = row_write or args.cmd == "rename" or args.cmd == "arc" and (
            args.acmd in ("create", "rename", "archive")
            or args.acmd == "reconcile" and (args.apply or args.resolve_stale))
        if rc == 0 and write:
            _say_pending(ctx)
        if rc == 0 and row_write:
            _say_news(ctx, args.wrote)
        if rc == 0 and write:
            _say_upstream(ctx)
        # A refused write said it already; summary --hook says it in its text.
        if rc == 0 and args.cmd not in ("trust", "serve") and not getattr(args, "hook", False) \
                and (note := _untrusted_note(ctx)):
            print(note, file=sys.stderr)
        return rc
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        # config.project_root()'s `git worktree list --porcelain` and
        # config.work_root()'s `git rev-parse --show-toplevel`, both
        # check=True, exit nonzero when cwd isn't inside a git repo at all.
        print(f"error: not a git repository: {os.getcwd()}; the store is derived from "
              f"the project, so name it with --dir <store>, or write its path in a "
              f".symbion here, to read it from here",
              file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        # Every verb asks git first. A hook's PATH that left out the dir git
        # installs to (/usr/local/bin on OpenBSD) printed this as a traceback
        # into the session (2026-10-05). Windows names no file.
        if e.filename not in ("git", None) or shutil.which("git"):
            raise
        print(f"error: symbion runs git, and no `git` is on PATH "
              f"({os.environ.get('PATH', '')})", file=sys.stderr)
        return 1
    except tomllib.TOMLDecodeError as e:
        # store_dir isn't bound in this frame when api.resolve() raises
        # before returning (config.load runs in ITS frame, not this one) --
        # api.resolve attaches it to the exception for exactly this handler.
        if getattr(e, "store_dir", None) is None:     # not this store's toml
            print(f"error: could not parse a {config.CONFIG_FILE}: {e}", file=sys.stderr)
            return 1
        print(f"error: could not parse {e.store_dir / config.CONFIG_FILE}: {e}",
              file=sys.stderr)
        # A pull killed with git's merge stopped on this file leaves its
        # conflict here, and no command reads past it to put the rows back.
        # The merge may be a person's now: finishing it is theirs to choose.
        aside = store._pull_aside(e.store_dir)
        if aside and aside.exists() and gitref._git(e.store_dir, "rev-parse", "-q", "--verify",
                                                    "MERGE_HEAD").returncode == 0:
            here = f"git -C {shlex.quote(str(e.store_dir))}"
            print(f"symbion: the store is in the middle of a merge that stopped on this file: "
                  f"finish it ({here} commit, once the file is resolved) or abort it ({here} "
                  f"merge --abort); the next symbion --dir {shlex.quote(str(e.store_dir))} "
                  f"pull then puts back the pending entries a cut-off pull set aside",
                  file=sys.stderr)
        return 1
    except (catalog.CatalogError, catalog.AmbiguousName) as e:
        print(f"error: {_untrusted(ctx, e)}", file=sys.stderr)
        return 1
    except store.ReadOnly as e:
        print(f"error: {e} (SYMBION_READ_ONLY is set)", file=sys.stderr)
        return 1
    except RuntimeError as e:                    # CatalogError subclasses this;
        print(f"error: {e}", file=sys.stderr)    # its own clause must come first
        return 1
    except ValueError as e:                      # AmbiguousName subclasses this;
        print(f"error: {_untrusted(ctx, e)}", file=sys.stderr)    # its own clause must come first
        return 1


if __name__ == "__main__":
    sys.exit(main())
