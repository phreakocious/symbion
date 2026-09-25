"""The CLI. A console script deletes the reference's invocation footguns
outright: no `-m`, no repo-root cwd requirement, no `--dir` position trap
(the store resolves before subcommand dispatch), and `--json` removes the
need to parse `type:name  note-id` off the end of a text line.

This is the one module allowed to import every other symbion module.
"""
from __future__ import annotations

import argparse
import difflib
from importlib import resources
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
    """Kept as a thin alias: tests/test_cli.py patches cli.subprocess.run
    (the same module object api.py's subprocess calls through) and calls
    this name directly, and that file is a frozen regression net. The actual
    precedence rule lives in api.author_default()."""
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
_HOOK_SETTINGS = {"hooks": {"SessionStart": [{
    "matcher": "startup|resume|clear|compact",
    "hooks": [{"type": "command", "command": _HOOK_COMMAND,
               "timeout": 10, "statusMessage": "Reading symbion notes..."}]}]}}
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
    # The script's path, not the command: JSON escapes the command's quotes, and
    # a hand-written entry may spell it `~/...`; either way it runs this file.
    elif "skills/symbion/session_start.sh" not in settings.read_text(encoding="utf-8"):
        print(f"{settings} does not register the symbion SessionStart hook; add:\n"
              + json.dumps(_HOOK_SETTINGS, indent=2))


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
    the process exits and the next `add` starts a SECOND store beside the
    first -- measured 2026-09-11, and the same silent fork a renamed repo
    produces. Relative when the store is a sibling, so the pointer survives a
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
    if pointer.exists() and pointer.read_text().strip() == value:
        return
    pointer.write_text(value + "\n", encoding="utf-8")
    print(f"wrote {pointer} -> {value} ({_git_says(project_root, pointer)})")


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
# you add one -- a catalog command's output format can change between PATCH
# releases of the same tool, so docs and prior runs elsewhere tell you nothing:
# `pytest --collect-only -q` emits per-file counts ("tests/test_x.py: 39") on
# pytest 9.0.2, but full test node ids on pytest 9.1.1, for the same flags.
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
def _print_note(n, *, state=None, subject=None, full=True, head=None) -> None:
    """`state` is (check_state, distance) for a check note, computed by the
    caller (batched per-listing where it needs a subject lookup); `subject`
    is the commit's subject line for a commit-target note, degrading to the
    bare sha when the caller has none. `full=False` clips the body to one
    line. No created_at prefix: the id IS the timestamp, and the row used to
    print the same fact twice. `head` is the head of a superseded row's
    chain: without it an old row's `[open]` read as live (2026-09-23)."""
    tgt = n.target.name if n.target.name else "(project)"
    if n.target.type == "commit" and subject:
        tgt = n.target.name[:7] + " " + (subject[:60] + "…" if len(subject) > 60 else subject)
    status, head_status = store.read_status(n), head and store.read_status(head)
    extra = f" [{status}]" if status else ""
    if head is not None:
        extra += f" superseded -> {head.id}" + (f" [{head_status}]" if head_status else "")
    if n.spec.verdict:
        extra += f" checked={n.checked!r} result={n.result!r}"
        if state is not None:
            st, dist = state
            if st == "unverifiable":
                st += f" ({api.why_unverifiable(n.provenance)})"
            extra += f" state={st}" + (f" distance={dist}" if dist is not None else "")
    if n.due:
        # The words only while it can still fire: a resolved row is never late.
        state = store.due_state(n.due) if status == "open" else None
        extra += f" due={n.due}" + (f" ({summ.due_phrase(state)})" if state else "")
    if n.refs:
        extra += " refs=" + ",".join(f"{r.type}:{r.name}" for r in n.refs)
    if n.provenance:
        extra += f" prov={n.provenance}"
    if n.tags:
        extra += " " + " ".join(f"#{t}" for t in n.tags)
    print(f"{n.kind:8} {n.target.type}:{tgt}{extra}  {n.id}")
    if n.body and full:
        _print_body(n.body)
    elif n.body:
        print(f"    {summ.clip(n.body, summ.LIST_BODY_CHARS)}")


def _print_body(body: str) -> None:
    """A body is markdown by contract. On a terminal, with the [tty] extra
    installed, it renders; on a pipe, or without rich, it prints as written
    so an agent reads exactly what was stored."""
    if sys.stdout.isatty():
        try:
            from rich.console import Console
            from rich.markdown import Markdown
            from rich.padding import Padding
        except ImportError:
            pass
        else:
            Console().print(Padding(Markdown(body), (0, 0, 0, 4)))
            return
    print(f"    {body}")


# ---- add: rows in, one write out ----
_ROW_KEYS = {"kind", "target", "body", "status", "checked", "result",
             "arc_id", "due", "tags", "refs", "author"}


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


def _body_fields(args) -> dict:
    """--body/--body-file as supersede's keyword: `body` replaces it, and
    under --append, `append_body` goes after the chain tip's body, which
    store reads under the lock -- so an amendment cannot rewrite the text
    it amends."""
    if args.body is None:
        if args.append:
            raise SystemExit("--append needs --body or --body-file: the text to add")
        return {}
    return {"append_body" if args.append else "body": args.body}


def _row_from_flags(args) -> dict:
    """The flag path as a `list --json`-shaped row, so both paths build a
    note the same way. Only the flags actually given are present, which is
    also how `--from-json` tells whether any other note flag was passed."""
    refs = _refs_from_flags(args.refs)
    row = {"kind": args.kind, "target": {"type": args.type, "name": args.name},
           "body": args.body or None, "status": args.status, "checked": args.checked,
           "result": args.result, "arc_id": args.arc_id, "due": args.due,
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
        if not isinstance(row, dict):
            raise SystemExit(f"add: --from-json line {i}: expected a JSON object")
        bad = sorted(set(row) - _ROW_KEYS)
        if bad:
            raise SystemExit(f"add: --from-json line {i}: unexpected key(s) {bad}; "
                             f"id, created_at and provenance are minted, not read")
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
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        """argparse's "expected one argument" names the flag, not what it
        takes; a bare `--type` is how a human asks what the types are."""
        bare = re.fullmatch(r"argument (\S+): expected one argument", message)
        if bare:
            action = self._option_string_actions.get(bare[1].split("/")[0])
            legal = action and (action.choices or getattr(action.type, "choices", None))
            if legal:
                message += f" (choose from {', '.join(legal)})"
        super().error(message)


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


def _catalog_choice(legal, what, store_dir):
    return _choice(legal, what, f"catalog {what}s are declared in "
                                f"{store_dir / 'symbion.toml'} under [catalogs]")


def _target_choice(legal, store_dir):
    """TYPE:NAME with TYPE checked: an unknown type can hold no notes, so a
    typo would otherwise read as a target with none."""
    check = _catalog_choice(legal, "type", store_dir)

    def parse(v):
        check(v.partition(":")[0])
        return v
    return parse


def _kind_choice(kinds, store_dir):
    return _choice(kinds, "kind", f"kinds are declared in {store_dir / 'symbion.toml'} "
                                  f"under [kinds]")


def _build_parser(target_types, arc_scopes, seed_scopes, store_dir, kinds):
    p = _Parser(prog="symbion", description="a per-project notebook and ticket registry")
    p.add_argument("--dir", default=None, help="store dir (default: derived from the project root)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the store with a starter symbion.toml, and install SKILL.md, "
                                "the SessionStart hook and its registration; re-run to refresh them")

    a = sub.add_parser("add", help="add a note of any kind, or many from --from-json")
    # `add KIND --target TYPE:NAME` is the same row as `--kind/--type/--name`:
    # the shape every guessed add took, and how
    # `context --target` already spells a target.
    a.add_argument("kind_pos", nargs="?", default=None, metavar="KIND",
                   type=_kind_choice(kinds, store_dir), help="the same as --kind")
    a.add_argument("--kind", default=None, type=_kind_choice(kinds, store_dir),
                   help="one of: " + ", ".join(kinds) + "; required unless --from-json")
    a.add_argument("--target", default=None, metavar="TYPE:NAME",
                   type=_target_choice(target_types, store_dir),
                   help="the same as --type TYPE --name NAME; `project` needs no name")
    a.add_argument("--type", dest="type", default=None, metavar="TYPE",
                   type=_catalog_choice(target_types, "type", store_dir),
                   help="one of: " + ", ".join(sorted(target_types)) + "; required unless --from-json")
    a.add_argument("--from-json", dest="from_json", default=None, metavar="PATH",
                   help="read notes from PATH ('-' for stdin), one JSON object per line in the "
                        "`list --json` shape (kind, target{type,name}, body, status, checked, "
                        "result, arc_id, tags, refs, author); every row is validated before "
                        "any is written; takes no other note flags")
    a.add_argument("--name", default=None, help="target name (omit for project)")
    ab = a.add_mutually_exclusive_group()
    ab.add_argument("--body", default="")
    ab.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin), for prose with "
                         "apostrophes or paragraphs that --body cannot carry")
    a.add_argument("--status", default=None, choices=sorted(store.STATUSES))
    a.add_argument("--checked", default=None, help="check: what was checked")
    a.add_argument("--result", default=None, help="check: verdict")
    a.add_argument("--arc-id", "--arc", dest="arc_id", default=None, metavar="ID")
    a.add_argument("--due", default=None, metavar="DATE",
                   help="YYYY-MM-DD or an ISO datetime; status kinds only. Past due, an "
                        "open row prints first at session start and in list --overdue")
    a.add_argument("--tag", dest="tags", action="append", default=[], help="repeatable")
    a.add_argument("--ref", dest="refs", action="append", default=[], metavar="TYPE:NAME",
                   help="secondary target this note also implicates (repeatable), as TYPE:NAME")
    a.add_argument("--author", default=None)

    li = sub.add_parser("list", help="list notes (newest-first heads)")
    li.add_argument("--id", dest="note_id", default=None, metavar="ID",
                    help="one row by id; implies --all, so a superseded id "
                         "still resolves. Exits 1 if it matches nothing")
    li.add_argument("--type", dest="type", default=None, metavar="TYPE",
                    type=_catalog_choice(target_types, "type", store_dir),
                    help="one of: " + ", ".join(sorted(target_types)))
    li.add_argument("--name", default=None)
    li.add_argument("--kind", default=None, type=_kind_choice(kinds, store_dir))
    li.add_argument("--status", default=None, choices=sorted(store.STATUSES))
    li.add_argument("--tag", default=None, help="only notes carrying this tag")
    li.add_argument("--arc", "--arc-id", dest="arc_id", default=None, metavar="ID",
                    help="only notes in this arc, resolved included "
                         "(`arc todo` is the open list)")
    li.add_argument("--grep", default=None, metavar="PATTERN",
                    help="regex, case-insensitive, over body, target name, checked and result")
    li.add_argument("--overdue", action="store_true",
                    help="only open rows past their --due date")
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
                    help="the answer (question) or the fate (idea); omit to inherit")
    rb.add_argument("--body-file", dest="body_file", default=None, metavar="PATH",
                    help="read the body from PATH ('-' for stdin)")
    rs.add_argument("--append", action="store_true",
                    help="add the --body/--body-file text after the current body, which "
                         "stays byte-identical, instead of replacing it")
    rs.add_argument("--add-tag", dest="add_tags", action="append", default=[],
                    help="add a tag to the inherited set (repeatable): adopted, retired, …")
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
                    help="replace the inherited refs (repeatable); omit to inherit")
    sp.add_argument("--arc-id", "--arc", dest="arc_id", default=None, metavar="ID",
                    help="file this note under an arc ('' detaches it)")
    sp.add_argument("--due", default=None, metavar="DATE",
                    help="set the due date: YYYY-MM-DD or an ISO datetime ('' clears it)")
    sp.add_argument("--author", default=None)

    cm = sub.add_parser("commit", help="git add -A + commit the store")
    cm.add_argument("-m", "--message", default="update notes")

    rn = sub.add_parser("rename",
                        help="re-target all notes for a renamed object and re-point every --ref to it (old -> new name)")
    rn.add_argument("old")
    rn.add_argument("new")
    rn.add_argument("--type", required=True, metavar="TYPE",
                    type=_catalog_choice(target_types, "type", store_dir),
                    help="one of: " + ", ".join(sorted(target_types)))
    rn.add_argument("--author", default=None)

    sv = sub.add_parser("serve", help="local web UI (requires: pip install 'symbion[gui]')")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--author", default=None,
                    help="identity stamped on GUI writes (default: git user.name)")
    sv.add_argument("--no-browser", action="store_true")
    sv.add_argument("--reload", action="store_true", help="dev: reload on .py changes")

    sub.add_parser("tags", help="list tag vocabulary with counts (catches near-synonyms)")

    sm = sub.add_parser("summary", help="bounded session-start summary")
    sm.add_argument("--full", action="store_true", help="lift the display caps")
    sm.add_argument("--json", action="store_true")

    sc = sub.add_parser("schema", help="this store's vocabulary: every kind with its bits "
                                       "and row count, then the target types")
    sc.add_argument("--json", action="store_true")

    ctx = sub.add_parser("context", help="pull-based detail")
    grp = ctx.add_mutually_exclusive_group()
    grp.add_argument("--target", default=None, metavar="TYPE:NAME",
                     type=_target_choice(target_types, store_dir))
    grp.add_argument("--commit", default=None, metavar="SHA")
    grp.add_argument("--branch", default=None, metavar="REF")
    ctx.add_argument("--since", default=None, metavar="REF", help="base ref for --branch")
    ctx.add_argument("--json", action="store_true")

    act = sub.add_parser("arc", help="arc registry")
    asub = act.add_subparsers(dest="acmd", required=True)

    ac = asub.add_parser("create")
    ac.add_argument("--name", required=True)
    ac.add_argument("--desc", default="")
    ac.add_argument("--scope", required=True, metavar="SCOPE",
                    type=_catalog_choice(arc_scopes, "scope", store_dir),
                    help="one of: " + ", ".join(sorted(arc_scopes)))
    ac.add_argument("--author", default=None)

    al = asub.add_parser("list")
    al.add_argument("--json", action="store_true")

    ar = asub.add_parser("rename")
    ar.add_argument("id")
    ar.add_argument("new_name")

    aa = asub.add_parser("archive")
    aa.add_argument("id")

    asd = asub.add_parser("seed")
    asd.add_argument("id", nargs="?", default=None,
                     help="the arc to seed; omit with --dry-run to probe a catalog")
    asd.add_argument("--scope", required=True, metavar="SCOPE",
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
        help="open items for an arc, one per line: 'type:name  note-id'")
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
        if t.type == "project" or (t.type, t.name) in seen:
            continue
        seen.add((t.type, t.name))
        open_ = sorted((h for h in store.query(heads, target_type=t.type, target_name=t.name,
                                               status="open")
                        if h.id not in new and not h.spec.parked),
                       key=lambda h: (h.created_at, h.id), reverse=True)
        if open_:
            ids = ", ".join(h.id for h in open_[:_NEIGHBOUR_CAP])
            more = f", +{len(open_) - _NEIGHBOUR_CAP} more" if len(open_) > _NEIGHBOUR_CAP else ""
            where = shlex.quote(f"{t.type}:{t.name}")
            print(f"note: {len(open_)} open on {t.type}:{t.name}: {ids}{more}; if this row "
                  f"settles or revises one, resolve or supersede that one "
                  f"(context --target {where})", file=sys.stderr)


# ---- misses: what failed, the legal values, the command that lists them ----
_ARC_HINT_CAP = 8


def _no_arc(store_dir, arc_id: str) -> None:
    """A bare miss stopped at the miss (2026-09-22). A near miss gets its
    candidates, otherwise the legal values, and always the verb that lists
    them. One helper, so every site agrees."""
    ids = [a.id for a in store.load_arcs(store_dir)]
    near = difflib.get_close_matches(arc_id, ids, n=3, cutoff=0.65)
    if near:
        what = f"did you mean {', '.join(near)}?"
    elif ids:
        shown = ", ".join(ids[:_ARC_HINT_CAP])
        more = f", +{len(ids) - _ARC_HINT_CAP} more" if len(ids) > _ARC_HINT_CAP else ""
        what = f"arcs: {shown}{more}"
    else:
        print(f"no arc {arc_id!r}; no arcs yet (arc create)", file=sys.stderr)
        return
    print(f"no arc {arc_id!r}; {what} (arc list)", file=sys.stderr)


def _no_note(note_id: str) -> None:
    print(f"no note found: {note_id} (list --limit 0 --all lists every id)", file=sys.stderr)


# ---- dispatch ----
_READS = {"list", "tags", "summary", "schema", "context"}
_ARC_READS = {"list", "todo", "reconcile"}


def _dispatch(args, ctx) -> int:
    store_dir, cfg, target_types = ctx.store_dir, ctx.cfg, ctx.target_types
    _resolve_body(args)
    if args.cmd == "init":
        return _init(store_dir, cfg, ctx.store_from_env)

    # An absent store is never a first session: `init` creates it. It is a
    # `.symbion` pointer to nowhere, a renamed repo, a wrong --dir or
    # SYMBION_DIR, and every read's empty state (`0 rows; no notes yet:
    # symbion add ...`) sent the reader to the write that forks a second
    # store. So it is an error, on stderr -- except summary's, which is the
    # hook's line: stdout, exit 0. --json keeps each verb's always-valid
    # shape, and summary --json its `store: null` gate.
    if (not store.exists(store_dir) and not getattr(args, "json", False)
            and (args.cmd in _READS or args.cmd == "arc" and args.acmd in _ARC_READS)):
        msg = f"symbion: no store at {store_dir}; run `symbion init`"
        if args.cmd == "summary":
            print(msg)
            return 0
        print(msg, file=sys.stderr)
        return 1

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
                return 1
        written = api.add_fields(ctx, fields)
        for note in written:
            print(note.id)
        _name_open_neighbours(store_dir, written)
        return 0

    if args.cmd == "list":
        if args.arc_id and not any(a.id == args.arc_id
                                        for a in store.load_arcs(store_dir)):
            _no_arc(store_dir, args.arc_id)
            return 1
        try:
            grep = re.compile(args.grep, re.IGNORECASE) if args.grep is not None else None
        except re.error as e:
            print(f"list: --grep {args.grep!r} is not a regex: {e}", file=sys.stderr)
            return 2
        every = store.load(store_dir)
        base = every if (args.all or args.note_id) else store.heads(every)
        filt = dict(target_type=args.type, target_name=api.canon(cfg, args.type, args.name),
                    kind=args.kind, tag=args.tag, arc_id=args.arc_id, author=args.author,
                    grep=grep, overdue=args.overdue or None)
        notes = store.query(base, id=args.note_id, status=args.status, **filt)
        if args.note_id and not notes:
            # An empty listing reads the same as "this row exists and matched
            # nothing else". You named an exact row; say it is not there.
            _no_note(args.note_id)
            return 1
        # created_at is to the second, so the id (microseconds) breaks ties;
        # without it "newest first" was file order inside one second.
        notes = sorted(notes, key=lambda x: (x.created_at, x.id), reverse=True)
        total = len(notes)
        # Text is bounded by default; --json only on an explicit --limit, so
        # the agent's array is never a silent page. 0 lifts the cap.
        cap = args.limit if args.limit is not None else \
            (None if args.json or args.note_id else summ.LIST_CAP)
        if cap:
            notes = notes[:cap]
        superseded = {n.supersedes for n in every if n.supersedes}
        heads_of = {n.id: api._chain_tip(every, n.id) for n in notes if n.id in superseded}
        if args.json:
            rows = []
            for n in notes:
                d = store.read_dict(n)
                if n.spec.verdict:
                    state, distance = gitref.check_state(cfg, n.provenance)
                    d["state"] = state
                    d["distance"] = distance
                if n.id in heads_of:
                    d["head"] = heads_of[n.id].id
                rows.append(d)
            print(json.dumps(rows))
            if len(rows) < total:
                print(f"showing {len(rows)} of {total}", file=sys.stderr)
        else:
            if not args.note_id:
                given = [f"--{flag} {shlex.quote(str(v))}" for flag, v in (
                    ("type", args.type), ("name", args.name), ("kind", args.kind),
                    ("status", args.status), ("tag", args.tag), ("arc", args.arc_id),
                    ("author", args.author), ("grep", args.grep)) if v]
                if args.overdue:
                    given.append("--overdue")
                print(_list_header(total, len(notes), _hidden(every, base, args, filt),
                                   scanned=len(base), filters=given))
            # One batched subjects() call for the whole listing rather than
            # one git-log per row -- gitref.subjects's whole reason to exist.
            shas = {n.target.name for n in notes
                   if n.target.type == "commit" and n.target.name}
            subjects = gitref.subjects(cfg, shas) if shas else {}
            for n in notes:
                state = gitref.check_state(cfg, n.provenance) if n.spec.verdict else None
                subject = subjects.get(n.target.name) if n.target.type == "commit" else None
                _print_note(n, state=state, subject=subject,
                            full=args.full or bool(args.note_id), head=heads_of.get(n.id))
        return 0

    if args.cmd == "resolve":
        # resolve is supersede with a fixed status; the body and tag merge are
        # the same plumbing supersede's combined --add-tag path uses.
        fields = {"status": "resolved", **_body_fields(args)}
        if args.result is not None:
            fields["result"] = args.result
        if args.add_tags:
            try:
                cur = api._chain_tip(store.load(store_dir), args.id)
            except KeyError:
                cur = None            # store.supersede raises it again below
            fields["tags"] = sorted(set(cur.tags if cur else ()) | set(args.add_tags))
        try:
            note = api.supersede(ctx, args.id, author=_resolved_author(args), **fields)
        except KeyError:
            _no_note(args.id)
            return 1
        print(note.id)
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
                fields["refs"] = api.check_refs(target_types,
                                                _refs_from_flags(args.refs))
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
        return 0

    if args.cmd == "commit":
        ok = api.commit(ctx, args.message)
        print("committed" if ok else "nothing to commit")
        # The second half of the footgun: committed is not off this disk.
        # Measured 2026-09-22, the first session with an origin on the
        # store -- "committed" read as done and the commit sat unpushed.
        n = gitref.unpushed(store_dir)
        if n is None:
            # Silence here read the same as a backed-up store (2026-09-23).
            print("no remote: this store exists on one disk")
        elif n:
            print(f"{n} commit{'' if n == 1 else 's'} not on origin: git -C {store_dir} push")
        return 0 if ok else 1

    if args.cmd == "rename":
        moved, refs = store.rename_target(store_dir, args.type, args.old, args.new,
                                          author=_resolved_author(args))
        print(f"re-targeted {moved} note(s), re-pointed {refs} ref(s): "
              f"{args.type}:{args.old} -> {args.new}")
        return 0 if moved or refs else 1

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
                       reload=args.reload)
        return 0

    if args.cmd == "tags":
        for t, cnt in sorted(store.tag_counts(store.load(store_dir)).items(),
                             key=lambda x: (-x[1], x[0])):
            print(f"{cnt:5}  {t}")
        return 0

    if args.cmd == "summary":
        if not store.exists(store_dir):   # text mode never gets here: see _READS
            print(json.dumps(summ.empty_summary()))
            return 0
        data = summ.summary(store_dir, cfg, full=args.full)
        skill = _SKILL_LINK / "SKILL.md"
        data["skill"] = f"~/{skill}" if (Path.home() / skill).exists() else None
        data["leftovers"] = (_old_copies(Path(cfg.project_root))
                             if cfg.project_root is not None else [])
        print(json.dumps(data) if args.json else summ.render_summary(data))
        return 0

    if args.cmd == "schema":
        data = summ.schema(store_dir, cfg, ctx.kinds)
        print(json.dumps(data) if args.json else summ.render_schema(data))
        return 0

    if args.cmd == "context":
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
            print(head)
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
            if not rows:
                print("0 arcs (arc create --name NAME --scope item|file|mixed|project)")
            w = max((len(r["id"]) for r in rows), default=0)
            for r in rows:
                desc = f"  -- {r['description']}" if r["description"] else ""
                print(f"{r['id']:{w}} {r['done']}/{r['total']}  {r['name']}{desc}")
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
        return 0

    if args.acmd == "seed":
        # No try/except here: catalog.AmbiguousName subclasses ValueError, and
        # catching ValueError at this site would intercept it before it
        # reaches main()'s dedicated (catalog.CatalogError, catalog.AmbiguousName)
        # clause, printing "seed: {e}" instead of the old "error: {e}". Both
        # that and the bare SystemExit below must propagate to main() exactly
        # as they did through the old cli._seed_names, uncaught here.
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
        print(f"seeded {len(made)} new {args.kind}(s)")
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
        rows = sorted(open_items, key=lambda x: x.created_at)
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
            print(head)
            for n in rows:
                print(f"{n.target.type}:{n.target.name}  {n.id}")
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
        line = ", ".join(f"{c[k]} {k}" for k in
                         ("live", "renamed", "stale", "uncheckable") if c[k])
        # --json: stdout is the report alone, what was found before any
        # --apply; everything else goes to stderr so the array still parses.
        say = (lambda *a: print(*a, file=sys.stderr)) if args.json else print
        if args.json:
            print(json.dumps(rows))
        else:
            print(f"{len(rows)} open task(s): {line or '(none)'}")
            for r in rows:
                if r["status"] == "renamed":
                    print(f"  RENAMED  {r['target_type']}:{r['target_name']}  ->  {r['suggestion']}")
                elif r["status"] == "stale":
                    need = "; needs --result" if r["needs_result"] else ""
                    print(f"  STALE    {r['target_type']}:{r['target_name']}"
                          f"  (no {cfg.stale_target_noun}{need})")
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        dir_value, rest = _extract_dir(argv)
        # `init` installs into the cwd's project, so it never follows a
        # named store to the project that owns it.
        ctx = api.resolve(dir_value, follow_owner=rest[:1] != ["init"])
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
