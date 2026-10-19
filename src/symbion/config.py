"""Config load and path derivation. No project-specific values live in code."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .catalog import DOC_CATALOG, NON_CANONICAL_TYPES

CONFIG_FILE = "symbion.toml"
# A repo's catalog under [repos] unless [catalogs] names the repo: every file
# git lists, committed or not, less what it ignores.
FILE_CATALOG = "git ls-files --cached --others --exclude-standard"


@dataclass(frozen=True)
class Config:
    # None when the store was NAMED (--dir, SYMBION_DIR) from a cwd in no git
    # repository -- see api.resolve. Reads then degrade (a check reads
    # `unverifiable`, as for a departed sha); a configured command and a
    # provenance stamp refuse by name; `init` refuses outright.
    project_root: Path | None
    # The checkout git operations run against: HEAD, branches, dirtiness and
    # configured commands (catalogs, renames, the provenance override) are
    # all per-worktree, so they must resolve here, not against project_root
    # (which is deliberately pinned to the MAIN worktree so every linked
    # worktree shares one store -- see project_root()'s docstring). Defaults
    # to project_root when not given, which is also correct: a Config built
    # by hand (every test fixture that only sets project_root) or from a
    # plain, non-worktree repo has only one checkout, so the two coincide.
    work_root: Path | None = None
    git_name: str = "symbion-notes"
    git_email: str = "symbion-notes@local"
    stale_target_noun: str = "catalog entry"
    default_branch: str = "main"
    catalogs: dict = field(default_factory=dict)
    renames: dict = field(default_factory=dict)
    resolvers: dict = field(default_factory=dict)
    provenance_command: str | None = None
    command_timeout: int = 30
    # A store with no remote on purpose: `commit` stops saying so.
    local_only: bool = False
    # The store this Config was loaded from, so configured commands can hand
    # a nested symbion the SAME store: `--dir` is argv and cannot be inherited,
    # SYMBION_DIR is. None for a hand-built Config (test fixtures), which then
    # leaves the child to resolve the store the normal way.
    store: Path | None = None
    # [repos]: each listed repo's name -> its main checkout. Each name is also
    # a catalog type (load puts it in `catalogs`). The roots above stand in
    # `here`, the listed repo the cwd is in, and in none outside every listed
    # repo: what is about no row's object runs there, and is refused outside,
    # as outside a repo today (the repos design, "Where you stand").
    repos: dict = field(default_factory=dict)
    here: str | None = None
    # The cwd's main worktree as resolved, before [repos] cleared the roots:
    # `init` installs there, and the summary names it when [repos] lacks it.
    cwd_root: Path | None = None

    def __post_init__(self):
        if self.work_root is None:
            object.__setattr__(self, "work_root", self.project_root)

    def repo(self, name) -> "Config":
        """This config at repo `name`, with no [repos] of its own, so a
        catalog, a stamp or a git question runs there: in the cwd's worktree
        when the cwd stands in `name`, else in its main checkout. A name not
        listed, or a repo this machine lacks, gets no roots: git answers
        there as it does for a departed sha."""
        if name is not None and name == self.here:
            roots = (self.project_root, self.work_root)
        elif (p := self.repos.get(name)) is not None and p.is_dir():
            roots = (p, p)
        else:
            roots = (None, None)
        return replace(self, project_root=roots[0], work_root=roots[1], repos={}, here=None)

    def for_type(self, target_type) -> "Config":
        """`repo(target_type)` for a listed repo's type, else this config. A
        `doc` is a file in the store's own docs/, so its commands run at the
        store's root wherever the caller stands."""
        if target_type == "doc" and self.store is not None:
            return replace(self, project_root=self.store, work_root=self.store, repos={}, here=None)
        return self.repo(target_type) if target_type in self.repos else self

    def missing(self, name) -> str | None:
        """Why repo `name` cannot run here, or None."""
        p = self.repos.get(name)
        if p is None or p.is_dir():
            return None
        return f"repo {name!r} is listed under [repos] at {p}, which is not on this machine"


def project_root(cwd=None) -> Path:
    """The MAIN worktree, never the current one.

    A linked worktree must share the parent project's store: worktrees are
    parallel lines of work on one project, so one store per worktree is
    backwards. `git worktree list --porcelain` names the main worktree in its
    first record, and unlike dirname(--git-common-dir) it is also correct for
    submodules and --separate-git-dir layouts."""
    out = subprocess.run(["git", "worktree", "list", "--porcelain"],
                         cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):]).resolve()
    raise RuntimeError("no worktree record; not a git repository")


def work_root(cwd=None) -> Path:
    """The CURRENT worktree -- HEAD, branches and dirtiness are per-worktree
    by definition, so this is the root ref/state resolution must use, not
    project_root. `--show-toplevel` is exactly wrong for project_root (see
    its docstring) and exactly right here: this is the one place that call
    belongs, because here the current checkout IS what's wanted."""
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return Path(out.strip()).resolve()


POINTER_FILE = ".symbion"


class PointerError(ValueError):
    """A `.symbion` that is there but names no store symbion can read."""


def _pointer(root: Path, work: Path | None = None) -> Path | None:
    """`<root>/.symbion`: the store this project uses, when it is not the
    sibling the name implies.

    The knob cannot live in symbion.toml -- that file is INSIDE the store
    being looked for. It has to sit beside the project, and it has to be a
    file rather than an env var because the thing it fixes is durable:
    measured 2026-09-11, renaming a repo DIRECTORY does not lose its store,
    it FORKS it. `summary` prints nothing and exits 0 (the guide's table
    reads that as "no store yet") and the next `add` created a second store
    and printed an ordinary id. (Since 2026-09-28 that `add` refuses and names
    the path: only `init` creates a store.)

    First non-blank line, the same rule as a resolver's stdout, so a `# why:`
    line can follow it. Blank or whitespace-only falls through to the
    default: an empty file must not resolve to `root` itself, which would
    make the project its own store.

    A pointer that is there but cannot be read is refused, never read as
    absent: absent names the sibling, and `no store at <sibling>; run
    symbion init` sent a `ln -s ../store .symbion` toward a second store
    (2026-10-03).

    `work`, the current worktree: its pointer counts while the main checkout
    has none, as on a branch that adopts symbion. Its path still resolves
    against `root`: it is the project's, and `../x` from a worktree nested
    in the repo would name a directory inside it (2026-10-03)."""
    p = root / POINTER_FILE
    if work is not None and not (p.exists() or p.is_symlink()):
        p = work / POINTER_FILE
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        if isinstance(e, FileNotFoundError) and not p.is_symlink():
            return None
        how = f"{POINTER_FILE} is a file whose first line names the store"
        if p.is_symlink() and not p.is_file():       # a link to the store, there or not
            t = os.readlink(p)
            q = shlex.quote(p.as_posix())
            raise PointerError(f"{p} is a link to {t}; {how}. Replace it: "
                               f"rm {q} && echo {shlex.quote(t)} > {q}") from None
        raise PointerError(f"cannot read {p} ({e.strerror}); {how}") from None
    for line in text.splitlines():
        if line.strip():
            return (root / Path(line.strip()).expanduser()).resolve()
    return None


def store_dir(root: Path, work: Path | None = None) -> Path:
    """SYMBION_DIR, else `.symbion`, else the sibling `<name>-notes`. The env
    var stays outermost: a one-off invocation against another store must not
    be overridden by a file in the tree."""
    env = os.environ.get("SYMBION_DIR")
    if env:
        return Path(env).resolve()
    return tree_store(root, work)


def tree_store(root: Path, work: Path | None = None) -> Path:
    """The store a project names for itself: `.symbion`, else the sibling."""
    pointed = _pointer(root, work)
    if pointed is not None:
        return pointed
    return (root.parent / f"{root.name}-notes").resolve()


def store_owner(store) -> Path | None:
    """The project whose own rules name `store`: `<name>` beside a store
    called `<name>-notes`, when that is the top of a MAIN worktree and its
    `.symbion` does not send it elsewhere. The inverse of tree_store, so a
    store named with `--dir` from another repo can resolve in its project
    (measured 2026-09-23: symbion's catalog ran in another project).

    A store whose name does not match (reached through a `.symbion`) is owned
    by the one sibling repo whose pointer names it; measured 2026-09-24, it
    had no owner and `--dir` resolved check state in the cwd's repo: 100 of
    100 rows `unverifiable`, stderr empty. Two such repos: no owner.

    ponytail: only siblings are scanned; a pointer from a repo elsewhere
    stays unowned until the store records its project."""
    store = Path(store).resolve()
    if store.name.endswith("-notes"):
        root = _claims(store.parent / store.name[:-len("-notes")], store)
        if root is not None:
            return root
    if not store.parent.is_dir():
        return None
    pointers = [c for c in store.parent.iterdir()
                if c != store and (c / ".symbion").is_file()]
    owners = [r for r in (_claims(c, store) for c in pointers) if r is not None]
    return owners[0] if len(owners) == 1 else None


def orphan_stores(root: Path) -> list[Path]:
    """The stores beside `root` that no project claims (`store_owner`): where
    a repo renamed with no `.symbion` left its store. A store that a sibling's
    pointer names is claimed though its name matches no project, so another
    project's store is never offered to the renamed one."""
    from . import store                  # late: config is imported before store
    return sorted(s for s in root.parent.glob("*-notes")
                  if store.exists(s) and not lists_repos(s) and store_owner(s) is None)


def lists_repos(store) -> bool:
    """The store's symbion.toml has a [repos] table. Read raw, before any
    Config: a store its repos claim has no one owner to follow, and is no
    orphan."""
    try:
        d = tomllib.loads((Path(store) / CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool(d.get("repos"))


def _repos(d: dict, store: Path, p: Path) -> dict:
    """[repos] as name -> the resolved main checkout, each path relative to
    the store (`~` expands). A name is a target type, so it may not be a
    built-in's, or `file`, which a store with repos takes as input only.
    Resolved, so a symlink matches the root git reports; two names on one
    path would be two spellings of every row's object."""
    raw = d.get("repos") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"[repos] in {p} is a table of NAME = \"PATH\", one line per repo")
    out = {}
    for name, v in raw.items():
        if not isinstance(v, str):
            raise ValueError(f"[repos] in {p} has {name} = {v!r}: a path is a quoted string")
        if name in NON_CANONICAL_TYPES | {"file"}:
            raise ValueError(f"[repos] in {p} names {name!r}: a repo's name is its target "
                             f"type, and {name!r} is taken; name it after the repo")
        path = (store / Path(v).expanduser()).resolve()
        if twin := next((k for k, q in out.items() if q == path), None):
            raise ValueError(f"[repos] in {p} names {path} as {twin!r} and {name!r}: "
                             f"one repo is one type; keep one name")
        out[name] = path
    return out


def _claims(cand: Path, store: Path) -> Path | None:
    """`cand` when it is the top of a MAIN worktree whose own rules name
    `store`. A subdirectory of a repo resolves to the repo, a linked worktree
    to its main one: neither is `cand`, so neither claims the store."""
    if not cand.is_dir():
        return None
    try:
        root = project_root(cand)
        if root != cand.resolve() or tree_store(root) != store:
            return None
    except (subprocess.CalledProcessError, RuntimeError, PointerError):
        return None
    return root


def uncomment_hint(store, name: str) -> str | None:
    """The commented `name =` lines that would declare catalog type `name`,
    or None. The starter toml ships its catalogs commented out, and "catalog
    types are declared in ... under [catalogs]" left the user of a new store to
    find the line and the [renames] pair (2026-10-09)."""
    p = Path(store) / CONFIG_FILE
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    section, found = None, []
    for n, line in enumerate(lines, 1):
        head = re.match(r"\s*\[([^\]]+)\]", line)
        if head:
            section = head[1].strip()
        elif section in ("catalogs", "renames") and re.match(
                rf"\s*#\s*{re.escape(name)}\s*=", line):
            found.append(f"line {n} under [{section}]")
    return f"uncomment `{name} =` in {p}: {', '.join(found)}" if found else None


def load(store, project_root: Path, work_root: Path | None = None) -> Config:
    """`work_root` param is the current worktree the caller resolved (e.g.
    `config.work_root()`); omit it and it falls back to `project_root`,
    matching every direct `Config(project_root=...)` construction.

    Precedence for an explicit `project_root =` in symbion.toml: it also
    pins `work_root` to the same value, on the theory that hand-pinning the
    root means "operate on this tree" -- unless `work_root =` is ALSO named
    explicitly in the file, which always wins."""
    p = Path(store) / CONFIG_FILE
    d = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    prov = d.get("provenance") or {}
    explicit_root = d.get("project_root")
    if explicit_root:
        resolved_root = Path(explicit_root).resolve()
    elif project_root is not None:
        resolved_root = Path(project_root).resolve()
    else:
        resolved_root = None
    if "work_root" in d:
        resolved_work = Path(d["work_root"]).resolve()
    elif explicit_root:
        resolved_work = resolved_root
    elif work_root is not None:
        resolved_work = Path(work_root).resolve()
    else:
        resolved_work = resolved_root
    catalogs = dict(d.get("catalogs") or {})
    resolvers = dict(d.get("resolvers") or {})
    repos = _repos(d, Path(store), p)
    if repos and "file" in catalogs:
        raise ValueError(f"[catalogs] file in {p} sits beside [repos]: a store with repos has "
                         f"no file type. Move the command under the repo it lists: "
                         f"{next(iter(repos))} = {catalogs['file']!r}")
    # Each type reads its own [renames] entry, so this one would follow no
    # repo's renames, and each renamed file would read `stale`.
    if repos and "file" in (d.get("renames") or {}):
        raise ValueError(f"[renames] file in {p} sits beside [repos]: a store with repos has "
                         f"no file type, so write it under each repo whose renames it follows: "
                         f"{next(iter(repos))} = {d['renames']['file']!r}")
    for name in repos:
        catalogs.setdefault(name, FILE_CATALOG)
    catalogs.setdefault("doc", DOC_CATALOG)
    builtins_named = sorted(set(resolvers) & NON_CANONICAL_TYPES)
    if builtins_named:
        raise ValueError(
            f"[resolvers] in {p} names {builtins_named}, a built-in type that is never "
            f"resolved by a command")
    orphans = sorted(set(resolvers) - set(catalogs))
    if orphans:
        raise ValueError(
            f"[resolvers] in {p} names {orphans} with no [catalogs] entry: a resolver "
            f"replaces the match rule for a catalog type, and the catalog declares the type")
    local_only = d.get("local_only", False)
    if not isinstance(local_only, bool):
        raise ValueError(f"local_only in {p} is {local_only!r}: write true or false, unquoted")
    # These reached subprocess as they were, and a quoted number or a bare
    # one ended in a TypeError traceback there (2026-10-09 review).
    timeout = d.get("command_timeout", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"command_timeout in {p} is {timeout!r}: write seconds as a "
                         f"positive number, unquoted")
    for table, entries in (("catalogs", catalogs), ("resolvers", resolvers),
                           ("renames", d.get("renames") or {}),
                           ("provenance", {"command": prov.get("command", "")})):
        bad = [f"{k} = {v!r}" for k, v in entries.items() if not isinstance(v, str)]
        if bad:
            raise ValueError(f"[{table}] in {p} has {', '.join(bad)}: a command is a "
                             f"quoted string")
    cwd_root, here = resolved_root, None
    if repos:
        here = next((n for n, path in repos.items() if path == resolved_root), None)
        if here is None:
            # Outside every listed repo the roots stand in none, so what is
            # about no row's object is refused by name, as outside a repo
            # today. A repo's type still runs there: Config.repo gives it roots.
            resolved_root = resolved_work = None
    return Config(
        project_root=resolved_root,
        work_root=resolved_work,
        git_name=d.get("git_name", "symbion-notes"),
        git_email=d.get("git_email", "symbion-notes@local"),
        stale_target_noun=d.get("stale_target_noun", "catalog entry"),
        default_branch=d.get("default_branch", "main"),
        catalogs=catalogs,
        renames=dict(d.get("renames") or {}),
        resolvers=resolvers,
        provenance_command=prov.get("command"),
        command_timeout=timeout,
        local_only=local_only,
        store=Path(store).resolve(),
        repos=repos,
        here=here,
        cwd_root=cwd_root,
    )
