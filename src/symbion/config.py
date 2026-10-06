"""Config load and path derivation. No project-specific values live in code."""
from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import NON_CANONICAL_TYPES

CONFIG_FILE = "symbion.toml"


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

    def __post_init__(self):
        if self.work_root is None:
            object.__setattr__(self, "work_root", self.project_root)


def project_root(cwd=None) -> Path:
    """The MAIN worktree, never the current one.

    A linked worktree must share the parent project's store: worktrees are
    parallel lines of work on one project, so one store per worktree is
    backwards. `git worktree list --porcelain` names the main worktree in its
    first record, and unlike dirname(--git-common-dir) it is also correct for
    submodules and --separate-git-dir layouts."""
    out = subprocess.run(["git", "worktree", "list", "--porcelain"],
                         cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True, check=True).stdout
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
                         capture_output=True, text=True, check=True).stdout
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
    it FORKS it. `summary` prints nothing and exits 0 (the README's table
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
            q = shlex.quote(str(p))
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
                  if store.exists(s) and store_owner(s) is None)


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
        command_timeout=d.get("command_timeout", 30),
        local_only=local_only,
        store=Path(store).resolve(),
    )
