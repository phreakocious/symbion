"""The CLI. A console script deletes the reference's invocation footguns
outright: no `-m`, no repo-root cwd requirement, no `--dir` position trap
(the store resolves before subcommand dispatch), and `--json` removes the
need to parse a text line, whose layout is for a reader and may change.

This is the one module allowed to import every other symbion module.
"""
from __future__ import annotations

import argparse
import difflib
import functools
from importlib import metadata, resources
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

from . import api, catalog, config, gitref, kinds as K, store
from . import summary as summ


# ---- author resolution ----
def _author_default() -> str:
    """A thin alias the precedence test in tests/test_cli.py calls, while it
    patches `cli.subprocess.run` (the same module object api.py's git call
    goes through). The rule lives in api.author_default()."""
    return api.author_default()


def _resolved_author(args) -> str:
    return args.author if args.author is not None else api.author_default()


# ---- init ----
# The agent surface is linked ONCE per user, into the package:
# ~/.claude/skills/symbion -> <symbion>/data/skill. Per-project copies drifted
# within the hour (2026-09-04) and re-staled every adopter on every SKILL.md
# edit; a link into the install cannot drift.
_SKILL_LINK = Path(".claude/skills/symbion")          # under $HOME
_HOOK_COMMAND = 'bash "$HOME/.claude/skills/symbion/session_start.sh"'
_HOOK_ENTRY = {"matcher": "startup|resume|clear|compact",
               "hooks": [{"type": "command", "command": _HOOK_COMMAND,
                          "timeout": 10, "statusMessage": "Reading symbion notes..."}]}
_HOOK_SETTINGS = {"hooks": {"SessionStart": [_HOOK_ENTRY]}}
# The script's path, not the command: JSON escapes the command's quotes, and a
# hand-written entry may spell it `~/...`; either way it runs this file.
_HOOK_SCRIPT = "skills/symbion/session_start.sh"
# What an older init wrote into each project, and the registration that ran it.
_OLD_HOOK = "$CLAUDE_PROJECT_DIR/hooks/session_start.sh"


def _skill_dir() -> Path:
    return Path(str(resources.files("symbion") / "data" / "skill"))


def _link_user_skill() -> None:
    """Link the skill, and register the hook in the user's settings only when
    that file is absent: merging someone's JSON is not this tool's business,
    so an existing file without the hook gets the block printed instead. A
    skill dir that is not this link is someone's; it is named, never replaced."""
    link, target = Path.home() / _SKILL_LINK, _skill_dir()
    if link.is_symlink() and link.resolve() == target.resolve():
        print(f"kept {link} -> {target}")
    elif link.exists() or link.is_symlink():
        print(f"note: {link} exists and is not a link to {target}; left alone")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
        print(f"linked {link} -> {target}")
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(_HOOK_SETTINGS, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {settings}")
        return
    registered = _hook_registered(settings)
    # The entry, not a whole settings object: pasted over a file that has
    # SessionStart hooks already, the object replaced them (adopter report).
    add = ('append this entry to the "SessionStart" list under "hooks" '
           f'(create either if missing):\n{json.dumps(_HOOK_ENTRY, indent=2)}')
    if registered:
        # Said out loud, as the skill link is: a re-run printed nothing here,
        # so "hook checked and present" read the same as "hook not checked"
        # (adopter report, 2026-09-28).
        print(f"kept hook in {settings}")
    elif registered is None:
        print(f"note: {settings} cannot be read as a JSON object, so whether it "
              f"registers the symbion SessionStart hook is unknown; to register it, {add}")
    else:
        print(f"{settings} does not register the symbion SessionStart hook; {add}")


def _hook_registered(settings: Path) -> bool | None:
    """Does `hooks.SessionStart` run our script? None means the file could not
    be read as the JSON object it must be, so neither answer is available.

    The test used to be a substring match over the whole file, which the
    command under another event key -- or the path quoted in a note -- also
    passes, and an unreadable file failed it, reading as "not registered"
    (adopter report, 2026-09-28). A file with no hooks at all IS the negative
    case; a shape this cannot walk is not."""
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks")
    if hooks is None:
        return False
    if not isinstance(hooks, dict):
        return None
    entries = hooks.get("SessionStart", [])
    if not isinstance(entries, list):
        return None
    return any(_HOOK_SCRIPT in str(h.get("command", ""))
               for e in entries if isinstance(e, dict)
               for h in (e.get("hooks") or []) if isinstance(h, dict))


def _old_copies(root: Path) -> list[str]:
    """What an init from before the user-level link left in this project: a
    second, staler skill beside the linked one, and a second hook. A
    project's own hooks/session_start.sh is not ours unless it runs symbion."""
    out = []
    if (root / _SKILL_LINK).exists():
        out.append(str(_SKILL_LINK))
    hook = root / "hooks" / "session_start.sh"
    if hook.exists() and "symbion summary" in hook.read_text(encoding="utf-8", errors="replace"):
        out.append("hooks/session_start.sh")
    settings = root / ".claude" / "settings.json"
    if settings.exists() and _OLD_HOOK in settings.read_text(encoding="utf-8"):
        out.append(".claude/settings.json SessionStart entry")
    return out


def _old_copies_line(old: list[str]) -> str:
    return (f"{len(old)} per-project symbion {'copy' if len(old) == 1 else 'copies'} from an "
            f"older init: {', '.join(old)} -- the user-level skill replaces "
            f"{'it' if len(old) == 1 else 'them'}; remove {'it' if len(old) == 1 else 'them'}")


def _git_says(root: Path, path: Path) -> str:
    """Whether the next `git add -A` takes `path`. In a public repo that did
    not ignore `.claude/`, init wrote three files there and said nothing
    (2026-09-23). A tracked path answers "not ignored", which is the
    question: will it be committed."""
    rc = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", str(path)],
                        capture_output=True).returncode
    return {0: "git: ignored", 1: "git: not ignored"}.get(rc, "git: check-ignore failed")


def _ignored_paths(store_dir, prefix: str) -> list[str]:
    """Paths under `prefix` that `git add -A` skips. `--ignored=matching`
    names each ignored FILE; the default collapses them to the directory."""
    r = subprocess.run(["git", "-C", str(store_dir), "status", "--porcelain",
                        "--ignored=matching"], capture_output=True, text=True)
    return sorted(ln[3:] for ln in r.stdout.splitlines()
                  if ln.startswith("!! ") and ln[3:].startswith(prefix + "/"))


def _init(store_dir, cfg, store_from_env: bool = False) -> int:
    if cfg.project_root is None:
        raise SystemExit(f"init needs a git repository: {os.getcwd()} is in none, and "
                         f"the store is derived from the project")
    if Path(store_dir).resolve() == Path(cfg.project_root):
        raise SystemExit(f"init installs into a project, and {store_dir} is the store; "
                         f"run it from the project (--dir naming the store if it is "
                         f"not ../<project>-notes)")
    store.ensure_store(store_dir)
    up = gitref.set_upstream(store_dir)
    if up:
        print(f"set upstream {up}")     # a bare `git push` of the store now works
    p = Path(store_dir) / config.CONFIG_FILE
    if p.exists():
        print(f"kept {p}")          # re-run: never the config
    else:
        _write_starter_toml(p, cfg)
        print(f"wrote {p}")
    _link_user_skill()
    old = _old_copies(Path(cfg.project_root))
    if old:
        print("note: " + _old_copies_line(old))
    _record_pointer(Path(store_dir), Path(cfg.project_root), store_from_env)
    return 0


def _record_pointer(store_dir: Path, project_root: Path,
                    store_from_env: bool = False) -> None:
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
    pointer naming somewhere else is reported. Removing one is the owner's
    call, and silence would leave two stores with nothing saying which the
    next command uses."""
    pointer = project_root / config.POINTER_FILE
    default = (project_root.parent / f"{project_root.name}-notes").resolve()
    store_dir = store_dir.resolve()
    if store_dir == default:
        # Compared by where it RESOLVES (config's own reader), not by existing:
        # a pointer reading `../<name>-notes` names the default and is fine.
        named = config._pointer(project_root)
        if named is not None and named != store_dir:
            print(f"note: {pointer} names {pointer.read_text().strip()!r}, not this store; "
                  f"delete it if that is stale")
        return
    if store_from_env:
        print(f"note: store came from SYMBION_DIR; not writing {pointer}. "
              f"Use `symbion --dir {store_dir} init` to make it durable")
        return
    if store_dir.parent == project_root.parent:
        value = f"../{store_dir.name}"
    else:
        value = str(store_dir)
    old = pointer.read_text().strip() if pointer.exists() else None
    if old == value:
        return
    pointer.write_text(value + "\n", encoding="utf-8")
    was = f" (was {old!r}: that store is no longer read from here)" if old else ""
    print(f"wrote {pointer} -> {value}{was} ({_git_says(project_root, pointer)})")


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
default_branch = "{cfg.default_branch}"

# A catalog type is a command emitting ONE NAME PER LINE and nothing else. It
# runs in the root of the current worktree.
# Verify with `symbion arc seed --scope <type> --dry-run` (no arc needed) every time
# you add one -- a catalog command's output depends on the tool's version and
# on the project's own config, so docs and prior runs elsewhere tell you nothing:
# `pytest --collect-only -q` emits full test node ids, but per-file counts
# ("tests/test_x.py: 39") where the project's pytest config adds `-q` to addopts.
[catalogs]
# `--others --exclude-standard` lists files not yet committed, so a note on a
# file made this session resolves instead of warning `stored as typed`.
# file = "git ls-files --cached --others --exclude-standard '*.py'"   # pair with `file = "git"` under [renames] below
# test = "git ls-files --cached --others --exclude-standard 'tests/test_*.py'"
# A STORE-DERIVED catalog is empty until its first row exists, and that row
# cannot be written while the catalog is empty: it NEEDS its [resolvers]
# entry below, which decides what a first sighting stores. Uncomment both.
# reading = "set -o pipefail; symbion list --all --json --type reading | jq -r '.[].target.name' | sort -u"

# A resolver replaces the built-in match rule (exact, else unique substring,
# else the input) for ONE catalog type. stdin: the query on line 1, then one
# catalog name per line. Exit 0: the first non-blank stdout line is the name
# to store, in or out of the list. Exit 2: stdout lists the matches, one per
# line; symbion refuses as ambiguous. Anything else is an error and stores
# nothing. It runs while the store lock is held, so it must not write to the
# store.
# A NUMERIC catalog without a resolver fragments: 3.1416 is not a
# substring of 3.14159, so it silently becomes a second target.
# `set -o pipefail` is load-bearing on a catalog that pipes: /bin/sh reports
# the LAST command's status, so without it a failed producer reads as an
# empty catalog and the resolver mints a fresh name.
[resolvers]
# reading = "python3 tools/resolve_reading.py"   # required by the store-derived catalog above

[renames]
# file = "git"      # built-in NUL-safe rename detection with transitive chains;
#                   # without it `arc reconcile` cannot follow a renamed file

# Kinds: a label on three bits. status = carries open/resolved and counts
# in arcs and at session start; parked = has a status but is hidden from
# every open view (requires status); verdict = carries --checked/--result,
# is stamped with provenance at write, and has a state derived from HEAD.
# status + verdict is a pre-registration: it cannot be resolved without
# --result. This table REPLACES the defaults when present. Renaming a label
# that already has rows also needs, once, by hand:
#   macOS: sed -i '' -e 's/"kind": "old"/"kind": "new"/g' notes.jsonl
#   GNU:   sed -i    -e 's/"kind": "old"/"kind": "new"/g' notes.jsonl
# `symbion schema` prints this table with a row count per label.
{K.render_toml(K.DEFAULT_KINDS)}''', encoding="utf-8")


# ---- text rendering ----
def _painter():
    """term.painter() at a terminal, else summary.plain. The import sits
    here for the reason _print_note's does: a pipe never loads rich."""
    if not sys.stdout.isatty():
        return summ.plain
    from . import term
    return term.painter()


def _print_note(n, *, state=None, subject=None, full=True, head=None) -> None:
    """`state` is api.verdict_state's pair for a verdict row, computed by the
    caller (batched per-listing where it needs a subject lookup); `subject`
    is the commit's subject line for a commit-target note, degrading to the
    bare sha when the caller has none. `full=False` clips the body to one
    line. No created_at prefix: the id IS the timestamp, and the row used to
    print the same fact twice. `head` is the head of a superseded row's
    chain: without it an old row's `[open]` read as live (2026-09-23)."""
    # A superseded row's own status is history: printed first, `[open]`
    # read as live beside a resolved head (2026-09-28). The head's decides.
    status = store.read_status(head if head is not None else n)
    # The due words only while it can still fire: a resolved row is never late.
    due = store.due_state(n.due) if n.due and status == "open" else None
    if sys.stdout.isatty():
        # Imported here, not at the top: a pipe (every agent read, the
        # session-start hook) never pays the ~50 ms of loading rich.
        from . import term
        term.print_note(n, status=status, state=state, due=due, subject=subject,
                        head=head, full=full)
        return
    tgt = n.target.name or ""          # `project:`, the form --target takes
    if n.target.type == "commit" and subject:
        tgt = n.target.name[:7] + " " + (subject[:60] + "…" if len(subject) > 60 else subject)
    extra = f" superseded -> {head.id}" if head is not None else ""
    extra += f" [{status}]" if status else ""
    if n.spec.verdict:
        if state is not None:
            st, dist = state
            if st == "unverifiable":
                st += f" ({api.why_unverifiable(n.provenance)})"
            elif st == "external":
                st += f" ({summ.age_phrase(n.provenance['at'])})"
            extra += f" state={st}" + (f" distance={dist}" if dist is not None else "")
    if n.due:
        extra += f" due={store.shown(n.due)}" + (f" ({summ.due_phrase(due)})" if due else "")
    if n.refs:
        extra += " refs=" + ",".join(summ.ref_label(r) for r in n.refs)
    if n.tags:
        extra += " " + " ".join(f"#{t}" for t in n.tags)
    print(f"{n.kind:8} {n.target.type}:{tgt}{extra}  {n.id}")
    if full:
        # A row in full says when, by whom, and what it revises: the id is a
        # timestamp no human reads as one (newcomer walk-through, 2026-09-26).
        print(f"    written {store.shown(n.created_at)} by {n.author}"
              + (f"; revises {n.supersedes}" if n.supersedes else ""))
    if n.spec.verdict:
        # Labelled lines, not reprs on the head line; the provenance dict
        # was printed raw there too (the v1 CLI's default).
        text = summ.flatten if full else (lambda v: summ.clip(v, summ.LIST_BODY_CHARS))
        sha = (n.provenance or {}).get("sha")
        at = f" at {sha[:7]}" if sha else ""
        if state and state[0] == "pending":
            print(f"    to check, registered{at}: {text(n.checked)}")
        elif n.checked is not None or at:
            print(f"    checked{at}: {text(n.checked)}")
        if n.result is not None:
            print(f"    result: {text(n.result)}")
    if n.body and full:
        # A body is markdown by contract: printed as written here, so an
        # agent reads exactly what was stored. symbion.term renders it.
        print(f"    {n.body}")
    elif n.body:
        print(f"    {summ.clip(n.body, summ.LIST_BODY_CHARS)}")


# ---- add: rows in, one write out ----
_ROW_KEY_ORDER = ("kind", "target", "body", "status", "checked", "result",
                  "external", "arc_id", "due", "tags", "refs", "author")
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
    if args.body_file == "-":
        args.body = sys.stdin.read()
        return
    try:
        args.body = Path(args.body_file).read_text()
    except OSError as e:
        # SystemExit(str) so main() prints it unprefixed and exits 1, the same
        # shape as every other bad-input refusal here. Uncaught, an OSError
        # reaches no handler in main() and the user gets a traceback.
        raise SystemExit(f"--body-file: {e}")


def _body_fields(args, append=False) -> dict:
    """--body/--body-file as supersede's keyword: `body` replaces it, and
    under --append, `append_body` goes after the chain tip's body, which
    store reads under the lock -- so an amendment cannot rewrite the text
    it amends. resolve always appends: replacing lost the claim the row was
    about in 111 of 149 adopter resolves (2026-09-26 audit)."""
    append = append or args.append
    if args.body is None:
        if args.append:
            raise SystemExit("--append needs --body or --body-file: the text to add")
        return {}
    return {"append_body" if append else "body": args.body}


def _row_from_flags(args) -> dict:
    """The flag path as a `list --json`-shaped row, so both paths build a
    note the same way. Only the flags actually given are present, which is
    also how `--from-json` tells whether any other note flag was passed."""
    refs = _refs_from_flags(args.refs)
    row = {"kind": args.kind, "target": {"type": args.type, "name": args.name},
           "body": args.body or None, "status": args.status, "checked": args.checked,
           "result": args.result, "external": args.external,
           "arc_id": args.arc_id, "due": args.due,
           "tags": args.tags or None, "refs": refs or None, "author": args.author}
    row = {k: v for k, v in row.items() if v is not None}
    if row["target"] == {"type": None, "name": None}:
        del row["target"]
    return row


def _rows_from_json(path):
    """[(lineno, row)] from PATH or stdin: one JSON object per line, blank
    lines skipped. Keys outside the input shape are refused by name -- an
    `id`, `created_at` or `provenance` in the input would otherwise be
    silently discarded, and a typo'd key silently dropped."""
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    rows = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"add: --from-json line {i}: {e}")
        if isinstance(row, list):
            raise SystemExit(f"add: --from-json line {i} is a JSON array; it reads one "
                             f"JSON object per line")
        if not isinstance(row, dict):
            raise SystemExit(f"add: --from-json line {i}: expected a JSON object")
        bad = sorted(set(row) - _ROW_KEYS)
        if bad:
            raise SystemExit(f"add: --from-json line {i}: unexpected key(s) {bad}; a row "
                             f"takes only {', '.join(_ROW_KEY_ORDER)}. symbion mints id, "
                             f"created_at and provenance itself")
        rows.append((i, row))
    return rows


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
    return value, rest


# ---- argparse ----
def _version(here: Path = Path(__file__).parent) -> str:
    """pip's `NAME VERSION from PATH`. The metadata version never moves in
    a checkout, so a checkout adds the commit it runs. It asks whether THIS
    file is tracked, not whether a repo encloses it: a venv inside a project
    sits in that project's work tree, and its HEAD is not symbion's."""
    line = f"symbion {metadata.version('symbion')} from {here}"
    if gitref._git(here, "ls-files", "--error-unmatch", "cli.py").returncode == 0:
        line += f" (git {gitref._git(here, 'describe', '--always', '--dirty').stdout.strip()})"
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
            self.print_help(sys.stderr)
            self.exit(2)
        bad = re.match(r"argument cmd: invalid choice: '([^']*)'", message)
        if bad and bad[1] in _VERB_HINTS:        # the usage line lists the verbs
            message = f"no verb {bad[1]!r}: {_VERB_HINTS[bad[1]]}"
        stray = re.match(r"argument KIND: invalid kind '([^']*)'", message)
        if stray and self._argv[:1] != [stray[1]]:
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
        super().error(message)

    def parse_known_args(self, args=None, namespace=None):
        """A verb's leftover arguments reached the top-level parser, which
        reported them under a usage that lists no flag of that verb. A leaf
        parser owns everything after its name, so it reports them itself."""
        self._argv = list(args or [])            # error() reads where KIND sat
        ns, extras = super().parse_known_args(args, namespace)
        if extras and self._subparsers is None:
            hints = [f"{x}: use {_FLAG_HINTS[x]}" for x in extras if x in _FLAG_HINTS]
            bare = [x for x in extras if not x.startswith("-")]
            if bare and "--body" in self._option_string_actions:
                # A quoted string arrives as one token with its spaces; several
                # space-free words are an unquoted value that split.
                if not any(" " in x for x in bare):
                    hints.append('a value with spaces needs quotes: --target "item:a b"')
                hints.append('text goes in --body "…"')
            self.error(f"unrecognized arguments: {' '.join(extras)}"
                       + "".join(f"; {h}" for h in hints))
        return ns, extras


# A first-day user's guesses (newcomer walk-through, 2026-09-26), each
# answered with the command that does it.
_APPEND_ONLY = ("rows are append-only: `resolve ID` closes a status row, "
                "`supersede ID --add-tag retired` retires a plain one")
_VERB_HINTS = {
    **dict.fromkeys(("delete", "rm", "remove", "undo", "drop"), _APPEND_ONLY),
    **dict.fromkeys(("edit", "update", "amend", "correct"),
                    "`supersede ID --body …` records a correction (--append adds to it)"),
    **dict.fromkeys(("close", "done", "finish", "complete"), "`resolve ID`"),
    **dict.fromkeys(("search", "find", "grep"), "`list --grep TEXT`"),
    **dict.fromkeys(("history", "log"), "`show ID` prints a row and names its chain's head"),
    **dict.fromkeys(("new", "create", "note"), "`add KIND --target TYPE:NAME --body …`"),
    **dict.fromkeys(("status", "todo", "open"), "`summary`, or `list --status open`"),
    "ls": "`list`",
    "due": "`list --overdue` (past due), or `summary` (due within 7 days)",
    "recent": "`list --since 2h`",
}
_FLAG_HINTS = {"--open": "--status open", "--closed": "--status resolved",
               "--resolved": "--status resolved", "--search": "--grep",
               "--message": "--body", "--text": "--body", "--title": "--body"}


def _choice(legal, what, where):
    """argparse `type` for a value set the config extends. `choices=` can only
    say what is legal; a user who typed a name that is not declared needs to
    hear where one is declared."""
    legal = sorted(legal)

    def parse(v):
        if v in legal:
            return v
        raise argparse.ArgumentTypeError(
            f"invalid {what} {v!r} (choose from {', '.join(legal)}); {where}")
    parse.choices = legal       # for _Parser.error on a bare flag
    return parse


def _catalog_choice(legal, what, store_dir, kinds=(), as_kind=None):
    """`kinds` and `as_kind` for a --type: a kind typed there (`--type bug`)
    is named as one, with the flag that takes it."""
    parse = _choice(legal, what, f"catalog {what}s are declared in "
                                 f"{store_dir / 'symbion.toml'} under [catalogs]")
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


def _since(v):
    """(as typed, cutoff): the header echoes what was typed."""
    try:
        return v, store.since_cutoff(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _kind_choice(kinds, store_dir):
    return _choice(kinds, "kind", f"kinds are declared in {store_dir / 'symbion.toml'} "
                                  f"under [kinds]")


_EPILOG = """\
read:
  symbion summary                    what is open in this project
  symbion list --grep WORD           search every row
  symbion show ID                    one row; a unique tail of its id works
write:
  symbion add bug --target item:NAME --body "..."
  symbion resolve ID --body "what closed it"
  symbion commit                     a row is not in the store's git until this
  symbion push                       nor off this disk until this

`symbion schema` lists this store's kinds and target types;
`symbion VERB -h` gives each verb's flags."""


def _build_parser(target_types, arc_scopes, seed_scopes, store_dir, kinds):
    p = _Parser(prog="symbion", description="a per-project notebook and ticket registry",
                epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=None,
                   help="store dir (default: $SYMBION_DIR, else a .symbion file in the repo, "
                        "else ../<repo>-notes)")
    # Listed here for -h; main() answers it before the store resolves.
    p.add_argument("-V", "--version", action="store_true",
                   help="print the version and the path it runs from")
    sub = p.add_subparsers(dest="cmd", required=True)

    init_says = ("create this project's store with a starter symbion.toml, and set up "
                 "Claude Code outside it: link the symbion skill into ~/.claude/skills "
                 "and register a session-start hook in ~/.claude/settings.json if that file "
                 "is absent, else print the entry to add; re-run to refresh them")
    sub.add_parser("init", help=init_says, description=init_says)

    a = sub.add_parser("add", help="add a note of any kind, or many from --from-json")
    # `add KIND --target TYPE:NAME` is the same row as `--kind/--type/--name`:
    # the shape every guessed add took, and how
    # `context --target` already spells a target.
    a.add_argument("kind_pos", nargs="?", default=None, metavar="KIND",
                   type=_kind_choice(kinds, store_dir), help="the same as --kind")
    a.add_argument("--kind", default=None, type=_kind_choice(kinds, store_dir),
                   help="one of: " + ", ".join(kinds) + "; required unless --from-json")
    a.add_argument("--target", default=None, metavar="TYPE:NAME",
                   type=_target_choice(target_types, store_dir, kinds),
                   help="the same as --type TYPE --name NAME; `project` needs no name")
    a.add_argument("--type", dest="type", default=None, metavar="TYPE",
                   type=_catalog_choice(target_types, "type", store_dir, kinds,
                                        "`add {} --target TYPE:NAME`"),
                   help="one of: " + ", ".join(sorted(target_types)) + "; required unless --from-json")
    a.add_argument("--from-json", dest="from_json", default=None, metavar="PATH",
                   help="read notes from PATH ('-' for stdin): one JSON object per line, "
                        "not the array `list --json` prints, with only these keys: "
                        + ", ".join("target{type,name}" if k == "target" else k
                                    for k in _ROW_KEY_ORDER)
                        + ". Every row is validated before any is written; takes no "
                          "other note flags")
    a.add_argument("--name", default=None, help="target name (omit for project)")
    ab = a.add_mutually_exclusive_group()
    ab.add_argument("--body", default="")
    ab.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin): safest for prose "
                         "with backticks, $ or several paragraphs, which a shell "
                         "rewrites inside a quoted --body")
    a.add_argument("--status", default=None, choices=sorted(store.STATUSES))
    a.add_argument("--checked", default=None, help="check: what was checked")
    a.add_argument("--result", default=None, help="check: verdict")
    a.add_argument("--external", action="store_true", default=None,
                   help="verdict kinds: what was checked is outside this repo (DNS, a "
                        "host, a live db), so the row is stamped with when it ran, not "
                        "HEAD, and lists its age instead of behind N or a dirty tree")
    a.add_argument("--arc-id", "--arc", dest="arc_id", default=None, metavar="ID")
    a.add_argument("--due", default=None, metavar="DATE",
                   help="YYYY-MM-DD or an ISO datetime, UTC unless TZ is set; status kinds "
                        "only. Past due, an "
                        "open row prints first at session start and in list --overdue")
    a.add_argument("--tag", dest="tags", action="append", default=[], help="repeatable")
    a.add_argument("--ref", dest="refs", action="append", default=[], metavar="TYPE:NAME",
                   help="secondary target this note also implicates (repeatable), as TYPE:NAME")
    a.add_argument("--author", default=None)

    li = sub.add_parser("list", help="list notes, newest first; filter by kind, target, tag, "
                                     "arc or --grep text")
    li.add_argument("--id", dest="note_id", default=None, metavar="ID",
                    help="one row by id; implies --all, so a superseded id "
                         "still resolves. Exits 1 if it matches nothing")
    li.add_argument("--type", dest="type", default=None, metavar="TYPE",
                    type=_catalog_choice(target_types, "type", store_dir, kinds,
                                         "`list --kind {}`"),
                    help="one of: " + ", ".join(sorted(target_types)))
    li.add_argument("--name", default=None)
    li.add_argument("--kind", default=None, type=_kind_choice(kinds, store_dir))
    li.add_argument("--status", default=None, choices=sorted(store.STATUSES))
    # The guess agents made most (2026-09-21..29). Out of -h, which teaches --status.
    for flag, status in (("--open", "open"), ("--resolved", "resolved"), ("--closed", "resolved")):
        li.add_argument(flag, dest="status", action="store_const", const=status,
                        help=argparse.SUPPRESS)
    li.add_argument("--tag", default=None, help="only notes carrying this tag")
    li.add_argument("--arc", "--arc-id", dest="arc_id", default=None, metavar="ID",
                    help="only notes in this arc, resolved included "
                         "(`arc todo` is the open list)")
    li.add_argument("--grep", default=None, metavar="PATTERN",
                    help="regex, case-insensitive, over body, target name, checked, "
                         "result and refs; ^ and $ anchor a line")
    li.add_argument("-F", "--fixed-strings", action="store_true",
                    help="take --grep as a literal string, not a regex")
    li.add_argument("--overdue", action="store_true",
                    help="only open rows past their --due date")
    li.add_argument("--since", default=None, metavar="WHEN", type=_since,
                    help="only rows written since WHEN: 30m, 2h, 3d, 1w, a date or an "
                         "ISO datetime, UTC unless TZ is set (a revision counts as "
                         "written when it was)")
    li.add_argument("--all", action="store_true", help="include superseded rows")
    li.add_argument("--author")
    li.add_argument("--limit", type=int, default=None, metavar="N",
                    help=f"rows to show, newest first (text default {summ.LIST_CAP}; 0 for "
                         f"all). --json is whole unless this is given")
    li.add_argument("--full", action="store_true",
                    help="print bodies as stored instead of one clipped line (--id implies it)")
    li.add_argument("--json", action="store_true")

    # Parsed only for -h: main() rewrites `show ID` to `list --id ID` first.
    # Listed so -h and the invalid-choice error name the verb, and with its
    # arguments so `show -h` documents them instead of erroring as `list --id -h`.
    sh = sub.add_parser("show", help="one row by id; `show ID [--json]` runs `list --id ID`",
                        description="one row by id, superseded or not: `list --id ID`")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")

    rs = sub.add_parser("resolve", help="mark an open row resolved",
                        description="mark an open row resolved")
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
    rs.add_argument("--add-tag", dest="add_tags", action="append", default=[],
                    help="add a tag to the inherited set (repeatable): adopted, retired, …")
    rs.add_argument("--ref", dest="refs", action="append", default=[], metavar="TYPE:NAME",
                    help="add a ref, e.g. the commit that closed it (repeatable); the "
                         "inherited refs stay")
    rs.add_argument("--result", default=None, help="the verdict; required on a "
                    "status+verdict kind (a pre-registration)")
    rs.add_argument("--author", default=None)

    sp = sub.add_parser("supersede", help="record a correction to a note")
    sp.add_argument("id")
    sb = sp.add_mutually_exclusive_group()
    sb.add_argument("--body", default=None)
    sb.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin)")
    sp.add_argument("--append", action="store_true",
                    help="add the --body/--body-file text after the current body, which "
                         "stays byte-identical, instead of replacing it")
    sp.add_argument("--status", default=None, choices=sorted(store.STATUSES))
    sp.add_argument("--checked", default=None,
                    help="check: correct what was checked")
    sp.add_argument("--result", default=None,
                    help="check: correct the verdict")
    sp.add_argument("--tag", dest="tags", action="append", default=None,
                    help="replace the inherited tags (repeatable); omit to inherit")
    sp.add_argument("--add-tag", dest="add_tags", action="append", default=[],
                    help="add a tag to the inherited set (repeatable)")
    sp.add_argument("--rm-tag", dest="rm_tags", action="append", default=[],
                    help="remove a tag from the inherited set (repeatable)")
    sp.add_argument("--ref", dest="refs", action="append", default=None, metavar="TYPE:NAME",
                    help="replace the inherited refs (repeatable; '' clears them); "
                         "omit to inherit")
    sp.add_argument("--arc-id", "--arc", dest="arc_id", default=None, metavar="ID",
                    help="file this note under an arc ('' detaches it)")
    sp.add_argument("--due", default=None, metavar="DATE",
                    help="set the due date: YYYY-MM-DD or an ISO datetime ('' clears it)")
    sp.add_argument("--author", default=None)

    cm = sub.add_parser("commit", help="git add -A + commit the store")
    cm.add_argument("-m", "--message", default="update notes")
    sub.add_parser("push", help="git push the store to its remote; commit first")

    rn = sub.add_parser("rename",
                        help="re-target all notes for a renamed object and re-point every --ref to it (old -> new name)")
    rn.add_argument("old")
    rn.add_argument("new")
    rn.add_argument("--type", required=True, metavar="TYPE",
                    type=_catalog_choice(target_types, "type", store_dir, kinds),
                    help="one of: " + ", ".join(sorted(target_types)))
    rn.add_argument("--to-type", metavar="TYPE", default=None,
                    type=_catalog_choice(target_types, "type", store_dir, kinds),
                    help="move the object to this type too (default: --type); "
                         "project:NAME rows from 0.1.0 move with --type project --to-type item")
    rn.add_argument("--author", default=None)

    sv = sub.add_parser("serve", help="local web UI (requires: pip install 'symbion[gui]')")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--author", default=None,
                    help="identity stamped on GUI writes (default: git user.name)")
    sv.add_argument("--no-browser", action="store_true")
    sv.add_argument("--reload", action="store_true", help="dev: reload on .py changes")

    sub.add_parser("tags", help="list tag vocabulary with counts (catches near-synonyms)")

    sm = sub.add_parser("summary", help="what is open: counts, due rows, arcs, open rows "
                                        "(the session-start text)")
    sm.add_argument("--full", action="store_true", help="lift the display caps")
    sm.add_argument("--json", action="store_true")

    sc = sub.add_parser("schema", help="this store's vocabulary: every kind with its bits "
                                       "and row count, then the target types")
    sgrp = sc.add_mutually_exclusive_group()
    sgrp.add_argument("--json", action="store_true")
    sgrp.add_argument("--toml", action="store_true",
                      help="the kinds as a [kinds] table to paste into symbion.toml; a table "
                           "there replaces the defaults, so keep the ones you use")

    ctx = sub.add_parser("context", help="every note on one object (--target), commit "
                                         "(--commit) or branch (--branch)")
    grp = ctx.add_mutually_exclusive_group()
    grp.add_argument("--target", default=None, metavar="TYPE:NAME",
                     type=_target_choice(target_types, store_dir, kinds))
    grp.add_argument("--commit", default=None, metavar="SHA")
    grp.add_argument("--branch", default=None, metavar="REF")
    ctx.add_argument("--since", default=None, metavar="REF", help="base ref for --branch")
    ctx.add_argument("--json", action="store_true")

    act = sub.add_parser(
        "arc", help="campaigns: a checklist of rows across objects",
        description="An arc is a checklist. `arc create --name NAME` prints its id; then "
                    "`add task --arc ID --target TYPE:NAME --body …` adds a step with a "
                    "body, or `arc seed ID --name STEP` adds bodiless ones. `resolve ID` "
                    "ticks a step; `arc todo ID` is what is left.")
    asub = act.add_subparsers(dest="acmd", required=True)

    ac = asub.add_parser("create", help="start an arc; prints its id")
    ac.add_argument("--name", required=True)
    ac.add_argument("--desc", default="")
    ac.add_argument("--scope", default="mixed", metavar="SCOPE",
                    type=_catalog_choice(arc_scopes, "scope", store_dir),
                    help="what the arc's rows are about (default mixed): item, project, "
                         "or a catalog type, which `arc seed` and the web UI can sweep; "
                         "one of: " + ", ".join(sorted(arc_scopes)))
    ac.add_argument("--author", default=None)

    al = asub.add_parser("list", help="every arc with its done/total")
    al.add_argument("--json", action="store_true")

    ar = asub.add_parser("rename", help="give an arc a new name; its id stays")
    ar.add_argument("id")
    ar.add_argument("new_name")

    aa = asub.add_parser("archive", help="take an arc off the summary; its open rows "
                                         "then list outside arcs")
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
    asd.add_argument("--author", default=None)

    atd = asub.add_parser(
        "todo",
        help="an arc's open items, one per line: kind, target, body, id")
    atd.add_argument("id")
    atd.add_argument("--json", action="store_true")

    rc = asub.add_parser(
        "reconcile",
        help="check each open task's target against the live catalog; flag stale/renamed")
    rc.add_argument("id")
    rc.add_argument("--apply", action="store_true",
                     help="re-target renamed tasks onto the live name")
    rc.add_argument("--resolve-stale", dest="resolve_stale", action="store_true",
                     help="also close tasks with no live target and no rename evidence")
    rc.add_argument("--author", default=None)
    rc.add_argument("--json", action="store_true",
                    help="the report rows as a JSON array; --apply counts go to stderr")

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
            print(f"note: this store holds {summ._count(n, 'row')} on project:{name}, "
                  f"filed before this rule; `{cmd}` moves them to item:{name}",
                  file=sys.stderr)


def _named_store(ctx) -> str | None:
    """`--dir <store>` when the store is not the one the cwd's tree names,
    else None. A hub's session reads its satellite's store beside its own,
    and the two summaries were identical: nothing in either said which
    project it was about (2026-09-28). Spelled as the flag that reaches it,
    since a reader of the second summary must also write to it.
    Relative when it sits beside the project, as `.symbion` records it."""
    if ctx.tree_default is not None and ctx.store_dir == ctx.tree_default:
        return None
    root = ctx.cfg.project_root
    rel = (f"../{ctx.store_dir.name}"
           if root is not None and ctx.store_dir.parent == Path(root).parent
           else str(ctx.store_dir))
    return f"--dir {shlex.quote(rel)}"


def _list_header(total: int, shown: int, hidden: list[str],
                 scanned: int = 0, filters: list[str] = ()) -> str:
    """With a filter, `N of M match <flags>`: a bare `0 rows` read as 0-of-0
    (2026-09-22). The flags are spelled as typed and
    shell-quoted, so the line pastes back."""
    if filters:
        head = f"{total} of {scanned} match " + " ".join(filters)
    elif scanned == 0:
        head = f"0 rows; {summ.FIRST_CONTACT}"       # the empty state names its verb
    else:
        head = summ._count(total, "row")
    if shown < total:
        head += f", showing {shown} newest (--limit N, 0 for all)"
    return "; ".join([head, *hidden])


_NEIGHBOUR_CAP = 3


def _name_open_neighbours(store_dir, written) -> None:
    """After an add, name the rows already open on each new row's target.
    Revisions landed as new rows beside the head they revised (an open
    "cause unknown" beside the row that found it), and the stale head
    kept printing at every session start. `project` is every row's fallback
    target, so a line there would print on every add and say nothing."""
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
            ids = ", ".join(h.id for h in open_[:_NEIGHBOUR_CAP])
            more = f", +{len(open_) - _NEIGHBOUR_CAP} more" if len(open_) > _NEIGHBOUR_CAP else ""
            where = shlex.quote(f"{t.type}:{t.name or ''}")
            which = " with this title" if title else ""
            print(f"note: {len(open_)} open on {t.type}:{t.name or ''}{which}: {ids}{more}; "
                  f"if this row settles or revises one, resolve or supersede that one "
                  f"(context --target {where})", file=sys.stderr)


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


def _name_catalog_misses(cfg, written) -> None:
    """After a batch, one line naming every target or ref its catalog does
    not list. Each miss prints its own note in line order among the rows
    that hit, and in a 23-row batch one typo scrolled past and minted a
    second head (2026-09-23). A type with a resolver mints new names on
    purpose, so it is left out."""
    names = [t for n in written for t in (n.target, *n.refs)
             if t.type in cfg.catalogs and t.type not in cfg.resolvers and t.name]
    known = {typ: set(catalog.names(cfg, typ)) for typ in {t.type for t in names}}
    every = list(dict.fromkeys(f"{t.type}:{t.name}" for t in names))
    miss = list(dict.fromkeys(f"{t.type}:{t.name}" for t in names if t.name not in known[t.type]))
    if miss:
        more = f", +{len(miss) - 5} more" if len(miss) > 5 else ""
        print(f"note: {len(miss)} of {len(every)} names not in their catalog, stored as typed: "
              f"{', '.join(miss[:5])}{more}", file=sys.stderr)


def _name_near_tags(store_dir, written) -> None:
    """After a write, name the existing tags a new one nearly is: case, `_`
    or `-`, a trailing s. `list --tag flaky-test` missed every row tagged
    `flaky-tests` (2026-09-26: 6 such pairs in 2 of 8 adopter stores), and
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
    print(f"note: session start shows an open row's first {summ.HEAD_CHARS} chars; "
          f"check that they carry its claim or ask:", file=sys.stderr)
    for n in cut:
        print(f"  {n.id}  {summ.clip(n.body, summ.HEAD_CHARS)}", file=sys.stderr)


# ---- misses: what failed, the legal values, the command that lists them ----
def _no_arc(store_dir, arc_id: str) -> None:
    print(api.no_arc(store_dir, arc_id), file=sys.stderr)


def _no_note(note_id: str) -> None:
    print(f"no note found: {note_id} (an id, or its tail after a dash: 123456-a1b, a1b; "
          f"list --grep TEXT searches the rows)", file=sys.stderr)


def _say_tip(store_dir, nid: str, *, resolving: bool) -> None:
    """A write to a superseded id lands on its chain's tip (the fast-forward
    that closes a race), and printed only the new id: a newcomer's
    `supersede <old> --body` replaced the newer row's text unawares
    (2026-09-26). Say which row the write lands on."""
    try:
        tip = api._chain_tip(store.load(store_dir), nid)
    except KeyError:
        return                                   # the verb's own no-note message
    if tip.id != nid:
        print(f"note: {nid} was superseded by {tip.id}; this applies to {tip.id}",
              file=sys.stderr)
    if resolving and store.read_status(tip) == "resolved":
        print(f"note: {tip.id} is already resolved", file=sys.stderr)


def _expand_id(store_dir, raw: str) -> str | None:
    """The full id for `raw`: itself when exact, else the one id ending in
    `-<raw>` (a leading `…` or `...` dropped, as ids are cited in prose).
    A miss returns `raw`, for the verb's own no-note message. A tail more
    than one id ends in prints them all and returns None: never pick one.
    A 3-hex tail does not stay unique (measured 2026-09-24: 11 shared tails
    in a 383-row store)."""
    notes = store.load(store_dir) if store.exists(store_dir) else []
    tail = raw.removeprefix("…").removeprefix("...")
    if any(n.id == tail for n in notes):
        return tail
    hits = [n for n in notes if n.id.endswith("-" + tail)]
    if len(hits) == 1:
        return hits[0].id
    if not hits:
        return raw
    print(f"ambiguous id {raw!r}: {len(hits)} rows end in -{tail}; give more of it",
          file=sys.stderr)
    for n in hits:
        print(f"  {n.id}  [{n.kind}] {n.target.type}:{summ.clip(n.target.name, summ.NAME_CHARS)}"
              f"  {summ.clip(n.body, summ.HEAD_CHARS)}", file=sys.stderr)
    return None


# ---- dispatch ----
_READS = {"list", "tags", "summary", "schema", "context"}
_ARC_READS = {"list", "todo", "reconcile"}


def _dispatch(args, ctx) -> int:
    store_dir, cfg, target_types = ctx.store_dir, ctx.cfg, ctx.target_types
    _resolve_body(args)
    if args.cmd == "init":
        return _init(store_dir, cfg, ctx.store_from_env)
    if args.cmd == "completion":
        import argcomplete
        print(argcomplete.shellcode(["symbion"], shell=args.shell), end="")
        return 0

    # An absent store is never a first session: `init` creates it. It is a
    # `.symbion` pointer to nowhere, a renamed repo, a wrong --dir or
    # SYMBION_DIR, and every read's empty state (`0 rows; no notes yet:
    # symbion add ...`) sent the reader to the write that forks a second
    # store. So it is an error, on stderr, for a write too (2026-09-28: a
    # write used to create it) -- except summary's, which is the hook's
    # line: stdout, exit 0. A read's --json keeps its always-valid shape,
    # and summary --json its `store: null` gate.
    if not store.exists(store_dir):
        msg = f"symbion: no store at {store_dir}; run `symbion init`"
        read = args.cmd in _READS or args.cmd == "arc" and args.acmd in _ARC_READS
        if read and getattr(args, "json", False):
            # The shape stays, for the parser; a bare `[]` at exit 0 read as
            # an empty store under a wrong --dir (2026-09-27). summary's
            # `store: null` is its gate, so it stays quiet.
            if args.cmd != "summary":
                print(msg, file=sys.stderr)
        elif args.cmd == "summary":
            print(msg)
            return 0
        else:
            print(msg, file=sys.stderr)
            return 1

    # Once, before any verb reads it: a cited tail (`a1b`, `…123456-a1b`) stands for
    # the one id that ends in it.
    attr = {"list": "note_id", "resolve": "id", "supersede": "id"}.get(args.cmd)
    if attr and getattr(args, attr):
        full = _expand_id(store_dir, getattr(args, attr))
        if full is None:
            return 1
        setattr(args, attr, full)
        if args.cmd != "list":
            _say_tip(store_dir, full, resolving=args.cmd == "resolve")

    if args.cmd == "add":
        if args.kind_pos and args.kind:
            print("add: KIND or --kind, not both", file=sys.stderr)
            return 2
        args.kind = args.kind or args.kind_pos
        if args.target is not None:
            if args.type or args.name:
                print("add: --target or --type/--name, not both", file=sys.stderr)
                return 2
            args.type, _, name = args.target.partition(":")
            args.name = name or None
        flag_row = _row_from_flags(args)
        if args.from_json:
            if set(flag_row) - {"author"}:      # --author is the rows' default
                print("add: --from-json takes no other note flags", file=sys.stderr)
                return 2
            rows = _rows_from_json(args.from_json)
        else:
            if not (args.kind and args.type):
                print(f"add: a kind and a target are required: `add KIND --target TYPE:NAME`, "
                      f"`add --kind K --type T [--name N]`, or --from-json PATH; "
                      f"kinds: {', '.join(ctx.kinds)}; types: {', '.join(sorted(target_types))}; "
                      f"`symbion schema` says when to use each kind", file=sys.stderr)
                return 2
            rows = [(None, flag_row)]
        author = _resolved_author(args)
        fields = []
        for lineno, row in rows:
            try:
                fields.append(api.fields_from_row(ctx, row, author))
            except ValueError as e:
                where = f"--from-json line {lineno}: " if lineno else ""
                print(f"add: {where}{e}", file=sys.stderr)
                _name_named_project_rows(ctx, row)
                return 1
        written = api.add_fields(ctx, fields)
        for note in written:
            print(note.id)
        _name_open_neighbours(store_dir, written)
        _name_near_tags(store_dir, written)
        if len(written) > 1:
            _name_catalog_misses(ctx.cfg, written)
        _echo_clipped_heads(store_dir, written)
        return 0

    if args.cmd == "list":
        if args.arc_id and not any(a.id == args.arc_id
                                        for a in store.load_arcs(store_dir)):
            _no_arc(store_dir, args.arc_id)
            return 1
        if args.fixed_strings and args.grep is None:
            print("list: -F needs --grep PATTERN", file=sys.stderr)
            return 2
        pattern = re.escape(args.grep) if args.fixed_strings else args.grep
        try:
            grep = re.compile(pattern, re.I | re.M) if args.grep is not None else None
        except re.error as e:
            print(f"list: --grep {args.grep!r} is not a regex: {e}", file=sys.stderr)
            return 2
        every = store.load(store_dir)
        base = every if (args.all or args.note_id) else store.heads(every)
        if args.name and args.type is None:
            t, colon, n = args.name.partition(":")
            if (colon and t in ctx.target_types
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
        filt = dict(target_type=args.type, target_name=api.canon(cfg, args.type, args.name),
                    kind=args.kind, tag=args.tag, arc_id=args.arc_id, author=args.author,
                    grep=grep, overdue=args.overdue or None,
                    since=args.since[1] if args.since else None)
        notes = store.query(base, id=args.note_id, status=args.status, **filt)
        if args.note_id and not notes:
            # An empty listing reads the same as "this row exists and matched
            # nothing else". You named an exact row; say it is not there.
            _no_note(args.note_id)
            return 1
        notes = store.newest_first(notes)
        total = len(notes)
        matched = {n.id for n in notes}
        # A literal pasted from a row (`$HOME`) never matches as a regex, and
        # its 0 read as absence. A plain word's 0 is a plain miss: no hint.
        ops = sorted(set(args.grep or "") & set(r"\.^$*+?{}[]()|"))
        if not total and args.name:
            # --name matches a whole name, while add takes a unique substring:
            # `--name` given the start of a longer item name read 0 beside a
            # dozen rows on it (an adopter, 2026-09-26).
            want = args.name.lower()
            near = Counter(f"{n.target.type}:{n.target.name}" for n in base
                           if n.target.name and want in n.target.name.lower()
                           and args.type in (None, n.target.type))
            if near:
                shown = ", ".join(f"{t} ({summ._count(k, 'row')})" for t, k in near.most_common(3))
                more = f", +{len(near) - 3} more" if len(near) > 3 else ""
                print(f"note: no target is named {args.name!r}; these contain it: {shown}{more} "
                      f"(--name matches a whole name)", file=sys.stderr)
        if not total and ops and not args.fixed_strings:
            # Only when -F would find something: a deliberate regex that
            # matches nothing is a plain miss, and the hint on it was noise.
            lit = re.compile(re.escape(args.grep), re.I | re.M)
            n_lit = len(store.query(base, id=args.note_id, status=args.status,
                                    **(filt | {"grep": lit})))
            if n_lit:
                print(f"note: --grep is a regex, and {args.grep!r} holds operators "
                      f"({' '.join(ops)}); as a literal (-F) it matches {n_lit}",
                      file=sys.stderr)
        if not total and args.fixed_strings and "|" in args.grep:
            # The other direction: -F takes every character literally, `|`
            # included, so a plain alternation reads 0 and looks like "nothing
            # filed yet" (an adopter, 2026-09-28). Only `|`: a literal pasted
            # from a row routinely holds `.`, `$` and `(`, and a hint on those
            # would fire on the reads -F exists for.
            try:
                n_re = len(store.query(base, id=args.note_id, status=args.status,
                                       **(filt | {"grep": re.compile(args.grep, re.I | re.M)})))
            except re.error:
                n_re = 0
            if n_re:
                print(f"note: -F took {args.grep!r} literally, '|' included; "
                      f"as a regex it matches {n_re}", file=sys.stderr)
        # Text is bounded by default; --json only on an explicit --limit, so
        # the agent's array is never a silent page. 0 lifts the cap.
        cap = args.limit if args.limit is not None else \
            (None if args.json or args.note_id else summ.LIST_CAP)
        if cap:
            notes = notes[:cap]
        superseded = {n.supersedes for n in every if n.supersedes}
        heads_of = {n.id: api._chain_tip(every, n.id) for n in notes if n.id in superseded}
        given = []
        if not args.note_id:
            given = [f"--{flag} {shlex.quote(str(v))}" for flag, v in (
                ("type", args.type), ("name", args.name), ("kind", args.kind),
                ("status", args.status), ("tag", args.tag), ("arc", args.arc_id),
                ("author", args.author), ("grep", args.grep)) if v]
            if args.fixed_strings:
                given.append("-F")
            if args.overdue:
                given.append("--overdue")
            if args.since:
                given.append(f"--since {shlex.quote(args.since[0])}")
        if args.json:
            rows = []
            for n in notes:
                d = store.read_dict(n)
                if n.spec.verdict:
                    state, distance = api.verdict_state(cfg, n)
                    d["state"] = state
                    d["distance"] = distance
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
            hidden = [h for h in (_hidden(every, base, args, filt) if given else ())
                      if h.startswith("+") and not h.endswith("(--all)")]
            if given and not args.all:
                older = {api._chain_tip(every, n.id).id
                         for n in store.query(every, status=args.status, **filt)
                         if n.id in superseded} - matched
                if older:
                    hidden.append(f"{summ._count(len(older), 'row')} "
                                  f"{'matches' if len(older) == 1 else 'match'} "
                                  f"only in an older version (--all)")
            if hidden:
                print(f"note: not in this array: {'; '.join(hidden)}", file=sys.stderr)
        else:
            paint = _painter()
            if not args.note_id:
                print(paint(_list_header(total, len(notes), _hidden(every, base, args, filt),
                                         scanned=len(base), filters=given), "meta"))
            # One batched subjects() call for the whole listing rather than
            # one git-log per row -- gitref.subjects's whole reason to exist.
            shas = {n.target.name for n in notes
                   if n.target.type == "commit" and n.target.name}
            subjects = gitref.subjects(cfg, shas) if shas else {}
            for n in notes:
                state = api.verdict_state(cfg, n) if n.spec.verdict else None
                subject = subjects.get(n.target.name) if n.target.type == "commit" else None
                _print_note(n, state=state, subject=subject,
                            full=args.full or bool(args.note_id), head=heads_of.get(n.id))
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
        if args.add_tags:
            try:
                cur = api._chain_tip(store.load(store_dir), args.id)
            except KeyError:
                cur = None            # store.supersede raises it again below
            fields["tags"] = sorted(set(cur.tags if cur else ()) | set(args.add_tags))
        try:
            note = api.supersede(ctx, args.id, author=_resolved_author(args),
                                 add_refs=_refs_from_flags(args.refs), **fields)
        except KeyError:
            _no_note(args.id)
            return 1
        print(note.id)
        _name_near_tags(store_dir, [note])
        return 0

    if args.cmd == "supersede":
        fields = _body_fields(args)
        if args.status is not None:
            fields["status"] = args.status
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
        if args.tags is not None:                      # --tag: replace
            fields["tags"] = args.tags
        elif (args.add_tags or args.rm_tags) and args.refs is None \
                and args.body is None and args.status is None \
                and args.arc_id is None and args.checked is None \
                and args.result is None and args.due is None:
            try:
                note = api.retag(ctx, args.id, add=args.add_tags, rm=args.rm_tags,
                                 author=_resolved_author(args))
            except KeyError:
                _no_note(args.id)
                return 1
            print(note.id)
            _name_near_tags(store_dir, [note])
            return 0
        elif args.add_tags or args.rm_tags:
            # combined with another field: merge inline against the tip, then
            # fall through to the single supersede below, so --add-tag --body
            # stays ONE row rather than two. A ValueError (cyclic chain)
            # propagates uncaught to main()'s handler, same as api.retag's
            # does on the tags-only path above -- both report a cycle
            # identically. A KeyError (missing id) is swallowed here so the
            # trailing store.supersede call below still raises it and hits
            # the existing "no note found" handler.
            try:
                cur = api._chain_tip(store.load(store_dir), args.id)
            except KeyError:
                cur = None
            base = set(cur.tags) if cur else set()
            fields["tags"] = sorted((base | set(args.add_tags)) - set(args.rm_tags))
        if args.refs is not None:                      # --ref: replace, like --tag
            try:
                fields["refs"] = api.check_refs(          # `--ref ''` alone: none
                    target_types, _refs_from_flags([r for r in args.refs if r]), ctx.kinds)
            except ValueError as e:
                print(f"supersede: {e}", file=sys.stderr)
                return 1
        try:
            # api.supersede, not store.supersede: the api door is what refuses
            # a target/provenance in **fields, and what resolves the refs
            # under the lock. check_refs above is for the error message only;
            # the raw query must reach the lock, or a name that became
            # ambiguous since would resolve by exact match.
            note = api.supersede(ctx, args.id, author=_resolved_author(args), **fields)
        except KeyError:
            _no_note(args.id)
            return 1
        print(note.id)
        _name_near_tags(store_dir, [note])
        if args.body is not None:
            _echo_clipped_heads(store_dir, [note])
        return 0

    if args.cmd == "commit":
        ok = api.commit(ctx, args.message)
        print("committed" if ok else "nothing to commit")
        # `git add -A` skips ignored paths silently. A tree copied verbatim
        # under archive/ can carry its own `.gitignore` of `*`, and then the
        # copy is on disk, `diff -r` passes, and the store's git holds none of
        # it (2026-09-28). archive/ exists to be committed, so anything
        # ignored there is the bug, not a choice.
        skipped = _ignored_paths(store_dir, "archive")
        if skipped:
            more = f", +{len(skipped) - 3} more" if len(skipped) > 3 else ""
            print(f"note: {len(skipped)} path(s) under archive/ are gitignored, so this "
                  f"commit does not hold them: {', '.join(skipped[:3])}{more} "
                  f"(git -C {store_dir} add -f archive)", file=sys.stderr)
        # The second half of the footgun: committed is not off this disk.
        # Measured 2026-09-22, the first session with an origin on the
        # store -- "committed" read as done and the commit sat unpushed.
        n = gitref.unpushed(store_dir)
        if n is None:
            # Silence here read the same as a backed-up store (2026-09-23).
            print("no remote: this store exists on one disk")
        elif n:
            push = " ".join(filter(None, ["symbion", _named_store(ctx), "push"]))
            print(f"{n} commit{'' if n == 1 else 's'} not on origin: {push}")
        return 0 if ok else 1

    if args.cmd == "push":
        if gitref.unpushed(store_dir) is None:
            print("no remote: this store exists on one disk", file=sys.stderr)
            return 1
        gitref.set_upstream(store_dir)             # a bare `git push` needs one
        # Not captured: git's progress, prompts and refusals are its own words.
        rc = subprocess.run(["git", "-C", str(store_dir), "push"]).returncode
        notes, registry = gitref.uncommitted(store_dir)
        bits = []
        if notes:
            bits.append(summ._count(notes, "note"))
        if registry:
            bits.append("arc changes")
        if bits:
            print(f"not pushed: {' and '.join(bits)} not yet in the store's git "
                  f"(symbion commit)", file=sys.stderr)
        return rc

    if args.cmd == "rename":
        to = args.to_type or args.type
        moved, refs, new = api.rename_target(ctx, args.type, args.old, args.new,
                                             author=_resolved_author(args), to_type=to)
        print(f"re-targeted {moved} note(s), re-pointed {refs} ref(s): "
              f"{args.type}:{args.old} -> {to}:{new}")
        if moved or refs:
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
        gui_serve.main(ctx, author=args.author or api.gui_author(),
                       port=args.port, show=not args.no_browser,
                       reload=args.reload, argv=args.argv)
        return 0

    if args.cmd == "tags":
        counts = store.tag_counts(store.load(store_dir))
        if not counts:
            print("no tags yet (add --tag NAME)")
        paint = _painter()
        for t, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"{paint(f'{cnt:5}', 'meta')}  {paint(t, 'text')}")
        return 0

    if args.cmd == "summary":
        if not store.exists(store_dir):   # text mode never gets here: see _READS
            print(json.dumps(summ.empty_summary()))
            return 0
        at_terminal = sys.stdout.isatty() and not args.json
        data = summ.summary(store_dir, cfg, full=args.full,
                            reader=None if at_terminal else api.author_default())
        skill = _SKILL_LINK / "SKILL.md"
        data["skill"] = f"~/{skill}" if (Path.home() / skill).exists() else None
        if at_terminal:
            data["skill"] = None      # the line is for an agent; the hook reads a pipe
        data["leftovers"] = (_old_copies(Path(cfg.project_root))
                             if cfg.project_root is not None else [])
        data["named_store"] = _named_store(ctx)
        print(json.dumps(data) if args.json else summ.render_summary(data, _painter()))
        return 0

    if args.cmd == "schema":
        if args.toml:
            print(K.render_toml(ctx.kinds), end="")
            return 0
        data = summ.schema(store_dir, cfg, ctx.kinds)
        print(json.dumps(data) if args.json else summ.render_schema(data, _painter()))
        return 0

    if args.cmd == "context":
        if args.commit and gitref.canonical_commit(cfg, args.commit) == args.commit \
                and gitref._git(cfg, "cat-file", "-e", f"{args.commit}^{{commit}}").returncode:
            # A departed commit keeps its rows, so this is a note, not an error.
            print(f"note: git does not know commit {args.commit!r} here; matched as typed",
                  file=sys.stderr)
        data = summ.context(store_dir, cfg, target=args.target, commit=args.commit,
                            branch=args.branch, since=args.since)
        if args.json:
            print(json.dumps(data))
        else:
            # A target with no notes printed nothing on stdout (2026-09-22).
            # The first line carries the count and its subject; the bare view
            # names the heads it leaves out, so its 0 is not an empty store.
            head = summ._count(len(data["notes"]), "note")
            if args.target:
                head += f" for {args.target}"
            elif args.commit:
                head += f" for commit {args.commit}"
            elif args.branch:
                head += f" for branch {args.branch}" + (f" since {args.since}" if args.since else "")
            else:
                other = len(store.heads(store.load(store_dir))) - len(data["notes"])
                if other:
                    head += f"; +{summ._count(other, 'other head')} (list)"
            print(_painter()(head, "meta"))
            for d in data["notes"]:
                _print_note(store.note_from_dict(d, kinds=ctx.kinds))
        return 0

    if args.cmd == "arc":
        return _dispatch_arc(args, ctx)

    return 0


def _dispatch_arc(args, ctx) -> int:
    store_dir, cfg = ctx.store_dir, ctx.cfg
    if args.acmd == "create":
        act = api.create_arc(ctx, args.name, args.desc, args.scope,
                                  author=_resolved_author(args))
        print(act.id)
        return 0

    if args.acmd == "list":
        notes = store.load(store_dir)
        rows = []
        for a in store.load_arcs(store_dir):
            if a.archived:
                continue
            done, total = store.arc_progress(notes, a.id)
            rows.append({"id": a.id, "name": a.name, "description": a.description,
                         "done": done, "total": total})
        if args.json:
            print(json.dumps(rows))
        else:
            paint = _painter()
            if not rows:
                print(paint("0 arcs (arc create --name NAME --scope item|file|mixed|project)",
                            "meta"))
            w = max((len(r["id"]) for r in rows), default=0)
            for r in rows:
                desc = paint(f"  -- {r['description']}", "meta") if r["description"] else ""
                prog = paint(f"{r['done']}/{r['total']}",
                             "good" if r["done"] == r["total"] else "meta")
                print(f"{paint(r['id'].ljust(w), 'text')} {prog}  "
                      f"{paint(r['name'], 'body')}{desc}")
        return 0

    if args.acmd == "rename":
        try:
            api.rename_arc(ctx, args.id, args.new_name)
        except KeyError:
            _no_arc(store_dir, args.id)
            return 1
        print(args.id)                # every other write prints; silence read as "did nothing"
        return 0

    if args.acmd == "archive":
        try:
            api.archive_arc(ctx, args.id)
        except KeyError:
            _no_arc(store_dir, args.id)
            return 1
        print(args.id)
        done, total = store.arc_progress(store.load(store_dir), args.id)
        if total > done:
            print(f"note: {summ._count(total - done, 'open row')} in {args.id}, now listed "
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
        print(f"seeded {len(made)} new {args.kind}(s)", file=sys.stderr)
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
            print(json.dumps([store.read_dict(n) for n in rows]))
        else:
            # A finished arc printed nothing at exit 0, the same as one nobody
            # had started (2026-09-22). Count with denominator; the hidden set
            # named with the verb that shows it, as list's header does.
            head = f"{len(rows)} open of {summ._count(len(rows) + resolved, 'item')}"
            if resolved:
                head += f"; +{resolved} resolved (list --arc {args.id})"
            paint = _painter()
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
        renames = {}
        for t in cfg.renames:
            renames.update(catalog.rename_map(cfg, t))
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
            paint = _painter()
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
            rt, rp, rs = store.apply_reconciliation(store_dir, rows, cfg,
                                                    resolve_stale=args.resolve_stale,
                                                    author=_resolved_author(args))
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
                           ctx.seed_scopes, ctx.store_dir, ctx.kinds)

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
        return {t: summ._count(counts[t], "row") for t in sorted(counts, key=counts.get, reverse=True)}

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
    try:
        code = _main(argv)
        if sys.stdout is not None:               # None when fd 1 was closed
            sys.stdout.flush()
        return code
    except BrokenPipeError:
        # The interpreter flushes stdout once more at exit: devnull takes it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141                               # 128 + SIGPIPE, as a shell reports it


def _main(argv) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if "_ARGCOMPLETE" in os.environ:
        _complete()                              # argcomplete exits the process
    try:
        dir_value, rest = _extract_dir(argv)
        if rest[:1] in (["-V"], ["--version"]):
            print(_version())
            return 0
        # `init` installs into the cwd's project, so it never follows a
        # named store to the project that owns it. `completion` runs from a
        # shell's rc file, wherever the shell starts, and reads no store: the
        # cwd stands in as one, as for `-h` below.
        if rest[:1] == ["completion"]:
            dir_value = os.getcwd()
        no_store = None
        try:
            ctx = api.resolve(dir_value, follow_owner=rest[:1] not in (["init"], ["completion"]))
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
        if ctx.followed_owner:
            print(f"symbion: {ctx.store_dir} belongs to {ctx.cfg.work_root}; catalogs "
                  f"and check state resolve there, not in {os.getcwd()}", file=sys.stderr)
        parser = _build_parser(ctx.target_types, ctx.arc_scopes,
                               ctx.seed_scopes, ctx.store_dir, ctx.kinds)
        # Agents typed `show <id>` from CLI habit and read the invalid-choice
        # error as "no such command" (2026-09-22..24).
        if rest[:1] == ["show"] and not {"-h", "--help"} & set(rest[1:]):
            rest = ["list", "--id", *rest[1:]]
        args = parser.parse_args(rest)
        args.argv = argv                         # serve --reload re-runs it
        if no_store is not None:
            raise no_store                       # `-h` was a value, not help
        return _dispatch(args, ctx)
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
              f"the project, so name it with --dir <store> to read it from here",
              file=sys.stderr)
        return 1
    except tomllib.TOMLDecodeError as e:
        # store_dir isn't bound in this frame when api.resolve() raises
        # before returning (config.load runs in ITS frame, not this one) --
        # api.resolve attaches it to the exception for exactly this handler.
        print(f"error: could not parse {e.store_dir / config.CONFIG_FILE}: {e}",
              file=sys.stderr)
        return 1
    except (catalog.CatalogError, catalog.AmbiguousName) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:                    # CatalogError subclasses this;
        print(f"error: {e}", file=sys.stderr)    # its own clause must come first
        return 1
    except ValueError as e:                      # AmbiguousName subclasses this;
        print(f"error: {e}", file=sys.stderr)    # its own clause must come first
        return 1


if __name__ == "__main__":
    sys.exit(main())
