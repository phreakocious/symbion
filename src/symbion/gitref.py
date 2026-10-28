"""Git is symbion's one authoritative catalog and its provenance source."""
from __future__ import annotations

import json
import math
import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import catalog, config, store


def _git(cfg_or_path, *args, input: str | None = None, errors: str = "strict"):
    """HEAD, branches and dirtiness are per-worktree, so this must run
    against `work_root` (the CURRENT checkout), never `project_root` (which
    is deliberately the MAIN checkout -- see config.project_root). A bare
    path (as `uncommitted(store)` passes) has neither attribute and is used
    as-is.

    A list that grows with the store goes on `input`, never argv: a Windows
    command line holds at most 32,767 characters, and past it the call
    raises WinError 206 (a store's paths ran well past it, 2026-10-16).
    `errors="replace"` for text from any commit: an imported history holds
    bytes that are not UTF-8, and a strict decode raised (2026-10-26)."""
    root = _root(cfg_or_path)
    if root is None:
        # A store named from outside any repository (api.resolve): every git
        # question answers as it does for a departed sha -- a failed run --
        # so a check reads `unverifiable`, a subject falls back to its sha.
        return subprocess.CompletedProcess(["git", *args], 128, "", "not a git repository")
    if store.read_only():
        # A read-only serve shows a frozen snapshot, so each distinct question
        # has one answer a process. The public demo asked about 60 a render,
        # 59 distinct over 5 renders: 5 s a page on its box, 2 s answered once
        # (2026-10-22).
        key = (str(root), args, input, errors)
        if key not in _ANSWERS:
            _ANSWERS[key] = subprocess.run(["git", "-C", str(root), *args], input=input,
                                           capture_output=True, text=True, encoding="utf-8",
                                           errors=errors)
        return _ANSWERS[key]
    return subprocess.run(["git", "-C", str(root), *args], input=input,
                          capture_output=True, text=True, encoding="utf-8", errors=errors)


_ANSWERS: dict = {}


def _root(cfg_or_path):
    return getattr(cfg_or_path, "work_root", None) or \
        getattr(cfg_or_path, "project_root", cfg_or_path)


def own_commit(here: Path = Path(__file__).parent) -> str | None:
    """The commit this symbion runs from (`git describe --always --dirty`),
    or None for an install: the metadata version never moves in a checkout.
    It asks whether THIS file is tracked, not whether a repo encloses it: a
    venv inside a project sits in that project's work tree, and its HEAD is
    not symbion's."""
    if _git(here, "ls-files", "--error-unmatch", "cli.py").returncode != 0:
        return None
    return _git(here, "describe", "--always", "--dirty").stdout.strip()


def _peel(cfg, ref) -> str | None:
    """`ref` peeled to a COMMIT's full id in `cfg`'s repo, or None.
    `^{commit}` is what makes this a commit target rather than an object
    target: plain --verify accepts HEAD^{tree} and returns a tree id."""
    r = _git(cfg, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def peel(cfg, ref) -> str | None:
    """`ref`'s full commit id, or None where git knows no such commit. In a
    store with [repos], the repo the cwd stands in peels first, so HEAD and
    a branch name mean its own; else each other listed repo does. A name
    that peels to different commits in several (a branch typed from outside
    every repo) is refused, naming them. One sha in several repos (a fork)
    is one commit."""
    if not getattr(cfg, "repos", None):
        return _peel(cfg, ref)
    if cfg.here and (sha := _peel(cfg.repo(cfg.here), ref)):
        return sha
    found = {n: s for n in cfg.repos if n != cfg.here and (s := _peel(cfg.repo(n), ref))}
    if len(set(found.values())) > 1:
        raise catalog.CatalogError(
            f"commit:{ref} names a different commit in each of {', '.join(found)}; "
            f"write its sha, or run from inside the repo it means")
    return next(iter(found.values()), None)


def canonical_commit(cfg, ref: str) -> str:
    """Peel to a COMMIT object and return its full id. On a miss (rebased
    away, shallow clone) fall back to the typed string, like any departed
    target."""
    return peel(cfg, ref) or ref


# (store, sha) -> the listed repo that holds the commit. A commit's repo does
# not change, so a process asks once. A miss is not kept: a fetch can bring
# the object.
_HOLDERS: dict = {}


def holder(cfg, sha) -> str | None:
    """The listed repo whose history holds commit `sha`, the cwd's first;
    None when none does, or the store lists no repos."""
    if not getattr(cfg, "repos", None) or not sha:
        return None
    key = (str(cfg.store), sha)
    if key not in _HOLDERS:
        order = ([cfg.here] if cfg.here else []) + [n for n in cfg.repos if n != cfg.here]
        name = next((n for n in order if _peel(cfg.repo(n), sha)), None)
        if name is None:
            return None
        _HOLDERS[key] = name
    return _HOLDERS[key]


def stamp_repo(cfg, prov) -> str | None:
    """The listed repo a stamp reads in: its `repo` while the store lists
    it, else the repo that holds its sha. A stamp written before the store
    had [repos] names none, and in a shared store those came from whichever
    repo each session stood in, so the sha decides (the repos design)."""
    name = prov.get("repo") if isinstance(prov, dict) else None
    return name if name in cfg.repos else holder(cfg, store.stamp_sha(prov))


def stamp_cfg(cfg, prov):
    """The config a stamp's git questions run in: `cfg` itself in a store
    without [repos], else its repo's, which has no roots when no listed repo
    holds the stamp, so it reads `unverifiable` as a departed sha does."""
    if not getattr(cfg, "repos", None):
        return cfg
    return cfg.repo(stamp_repo(cfg, prov))


def only_on_default_branch(cfg, name) -> str | None:
    """`cfg.default_branch` when `name` is a path THERE and not in this
    worktree, else None.

    A catalog runs in the checkout you are in (catalog.run_configured), so a
    row about a file only the default branch holds missed the catalog with
    nothing said, and a substring resolved against a different file set
    (2026-09-28). "Not in this worktree" is the other half of the test: a
    catalog that FILTERS (`git ls-files '*.py'`) misses plenty of paths that
    are right there, and the note about one of those would be a lie."""
    root = getattr(cfg, "work_root", None)
    if not root or not name or Path(root, name).exists():
        return None
    b = cfg.default_branch
    return b if _git(cfg, "cat-file", "-e", f"{b}:{name}").returncode == 0 else None


def other_worktree(cfg, name) -> str | None:
    """`<path from the cwd> (<branch>)` of another worktree of this repo
    that holds `name`, when this one does not. A catalog lists the checkout
    you stand in, so a file only a linked worktree held read as a typo
    (2026-10-13)."""
    root = getattr(cfg, "work_root", None)
    if not root or not name or Path(root, name).exists():
        return None
    for rec in _git(cfg, "worktree", "list", "--porcelain").stdout.split("\n\n"):
        f = dict(ln.partition(" ")[::2] for ln in rec.splitlines())
        wt = f.get("worktree")
        if wt and Path(wt).resolve() != Path(root).resolve() and Path(wt, name).exists():
            try:
                wt = Path(os.path.relpath(wt)).as_posix()
            except ValueError:                   # Windows: another drive
                pass
            return f"{wt} ({f.get('branch', '').removeprefix('refs/heads/') or 'detached'})"
    return None


def blob_text(cfg, rev, name) -> str | None:
    """`name` as `rev` holds it, when it is a file there that reads as
    UTF-8; else None. The viewer's copy of a file only the default branch
    holds (only_on_default_branch)."""
    try:
        r = _git(cfg, "cat-file", "blob", f"{rev}:{name}")
    except UnicodeDecodeError:
        return None
    return r.stdout if r.returncode == 0 else None


def ignored_here(cfg, name) -> bool:
    """`name` is a file in this worktree that git ignores. The default file
    catalog (`--exclude-standard`) drops it, so a task on a gitignored
    CLAUDE.md got the typo's note (2026-10-12). Both halves are tested: an
    ignore pattern matches paths that do not exist, and a filtering catalog
    leaves out files git does not ignore."""
    root = getattr(cfg, "work_root", None)
    return bool(root and name and Path(root, name).exists()
                and _git(cfg, "check-ignore", "-q", "--", name).returncode == 0)


def tracked(cfg, path) -> bool:
    """`path`, inside this worktree, is a file git tracks."""
    return _git(cfg, "ls-files", "--error-unmatch", "--", str(path)).returncode == 0


def markdown_files(cfg_or_path) -> list[str]:
    """The markdown files under a checkout or a directory in one, as the
    file catalog lists them: tracked, or new and not ignored. Read-only, the
    public demo, tracked only: the leak scan read those. [] outside a repo."""
    new = () if store.read_only() else ("--others", "--exclude-standard")
    r = _git(cfg_or_path, "ls-files", "-z", "--cached", *new, "--", ":(icase)*.md")
    return list(dict.fromkeys(n for n in r.stdout.split("\0") if n)) if r.returncode == 0 else []


def canon_name(cfg, target_type, name, stored=()):
    """The stored/queried form of a target name -- shared by the write side
    (`add`) and the read side (`list --name`, `context --target`), so an
    abbreviation used on write also matches on read.

    `stored` is a read's: the names the store holds for this type. One of
    them typed verbatim is that object, before the catalog runs: a deleted
    file whose name recurs deeper in the tree read as ambiguous, or resolved
    to a live path holding none of its rows (2026-10-07). A write passes
    none, so a new row on a deleted path still meets the catalog.

    `commit` gets the one built-in exception: peeled to a full object id, so
    a symbolic ref (HEAD, a branch) is frozen at write time instead of
    silently drifting as the ref advances (design spec, commit targets). This lives in gitref,
    not catalog, because it needs canonical_commit; catalog must not import
    gitref (that would invert the layering), but gitref already imports
    catalog, so this is the one place both sides can share it from."""
    if name is None:
        return None
    if (q := catalog.nfc(name)) in stored:
        if target_type in cfg.catalogs:
            # A write of `q` lands where the catalog sends it, so a read that
            # skips the catalog must say where that is, or the row just
            # written reads as missing.
            try:
                pool = catalog.pool(cfg, target_type)
                got = catalog.resolve_with(cfg, target_type, q, pool) \
                    if catalog.resolver(cfg, target_type) else catalog.resolve(q, pool)
            except (catalog.CatalogError, catalog.AmbiguousName):
                got = q
            if got != q:
                print(f"note: {q!r} is a stored {target_type} name, read as typed; "
                      f"the catalog resolves it to {got!r}", file=sys.stderr)
        return q
    if target_type == "commit":
        sha = peel(cfg, name)
        if sha is None:
            # A typo stored silently, where `context --commit` says so (2026-10-09).
            where = "in any listed repo" if getattr(cfg, "repos", None) else "here"
            print(f"note: git does not know commit {name!r} {where}; taken as typed",
                  file=sys.stderr)
            return name
        return sha
    if target_type in catalog.NON_CANONICAL_TYPES:
        return catalog.nfc(name)
    return catalog.canonical(cfg, target_type, name)


# ---- `file:` as input: a path from where you stand ----
_PATH_LEADS = ("/", "~", "./", "../", ".\\", "..\\")


def is_path(name) -> bool:
    """A `file:` name typed as a path: absolute, under `~`, or from the cwd.
    symbion never prints a name in this form, so reading it from the cwd
    changes no pasted name (the repos design, "Paths in a store without
    repos"). A `.` or `..` segment anywhere makes one too: no catalog lists
    `sub/../x.py`, which was stored as typed (2026-10-14 review)."""
    return isinstance(name, str) and (
        name.startswith(_PATH_LEADS) or Path(name).is_absolute()
        or not {".", ".."}.isdisjoint(name.replace("\\", "/").split("/")))


def _from_path(cfg, q) -> tuple:
    """(the listed repo, the name from its root, the resolved Path) for a
    `file:` path read from the cwd. Git says which checkout holds it,
    so a linked worktree's file names its repo. In a store without [repos]
    the repo is None, and the path must be in the project."""
    p = (Path.cwd() / Path(q).expanduser()).resolve()
    d = p if p.is_dir() else next(a for a in p.parents if a.is_dir())
    try:
        main, top = config.project_root(d), config.work_root(d)
    except (subprocess.CalledProcessError, RuntimeError):
        main = top = None
    repos = getattr(cfg, "repos", None) or {}
    repo = next((n for n, path in repos.items() if main is not None and path == main), None)
    if repos and repo is None:
        raise catalog.CatalogError(
            f"file:{q} is {p}, in none of this store's repos ("
            + ", ".join(f"{n} {path}" for n, path in repos.items()) + ")")
    if not repos and (main is None or main != cfg.project_root):
        raise catalog.CatalogError(f"file:{q} is {p}, outside this project "
                                   f"({cfg.project_root})")
    return repo, p.relative_to(top).as_posix(), p


def file_target(cfg, name, extra=None, say: bool = True, cache=None,
                reading: bool = False) -> tuple:
    """`file:NAME` as typed: (type, name from its repo's root), the rule
    every write and read of `file:` meets (the repos design, "`file` in a
    store with repos").

    A path (is_path) resolves from the cwd, in any store. In a store with
    [repos] a bare name does too, and the first of these that answers wins:
    the name joined to the cwd's directory in the repo you stand in; that
    repo's catalog from its root, by the built-in rule; the other repos'
    catalogs by the same rule at once, where one holder takes it and several
    refuse; else a new file, at the name joined to the cwd. From outside
    every repo, only one holder answers.

    A [resolvers] entry is skipped: a resolver may answer a name outside its
    catalog, so it cannot say whether its repo holds one. `extra` maps a repo
    to names added to its pool: a write's pending names, a read's stored
    ones. `say` off keeps the notes, for a caller that needs only the repo.
    `cache`, a write's dict, keeps each repo's catalog for the write's
    other names, so a batch runs each catalog once. `reading` words a miss
    as a read's: `list` said `a new file` of a name it only looked up."""
    q = catalog.nfc(name)

    def note(s):
        if say:
            print(f"note: {s}", file=sys.stderr)
    here, extra, cache = cfg.here, extra or {}, {} if cache is None else cache

    def pool(r):
        if r not in cache:
            cache[r] = catalog.names(cfg, r, allow_empty=True)
        return [*cache[r], *extra.get(r, ())]

    def closed(typ, got, what):          # a write's new name, where only listed ones go
        if not reading and cfg.resolvers.get(typ) == catalog.CLOSED and got not in pool(typ):
            raise catalog.CatalogError(f"file:{q} {what}, and the {typ} catalog is closed: it "
                                       f"takes only the names it lists ([resolvers] {typ} = "
                                       f"\"closed\" in symbion.toml)")
    if is_path(q):
        repo, rel, p = _from_path(cfg, q)
        typ = repo or "file"
        closed(typ, rel, f"is {typ}:{rel}, which it does not list")
        # A directory took a file's name with no word of it (the repos review).
        note(f"file:{q} is {typ}:{rel}" + ("" if p.is_file() else ", a directory"
                                           if p.is_dir() else ", which does not exist"
                                           + ("" if reading else " yet")))
        return typ, rel
    joined = q
    if here:
        sub = Path.cwd().resolve().relative_to(cfg.work_root).as_posix()
        joined = posixpath.normpath(posixpath.join(sub, q))
        own = pool(here)
        if sub != "." and joined in own:
            note(f"file:{q} is {here}:{joined}")
            return here, joined
        hits = catalog.across(q, {here: own})
        if len(hits) > 1:
            raise catalog.CatalogError(f"file:{q} matches {catalog.listed(hits)}; "
                                       f"write one of them, or its path")
        if hits:
            note(f"file:{q} is {here}:{hits[0][1]}")
            return hits[0]
    others = [r for r in cfg.repos if r != here]
    if gone := [r for r in others if cfg.missing(r)]:
        note(f"file:{q} skips {', '.join(gone)}: not on this machine")
    hits = catalog.across(q, {r: pool(r) for r in others if r not in gone})
    if len(hits) > 1:
        raise catalog.CatalogError(f"file:{q} matches {catalog.listed(hits)}; "
                                   f"write one of them, or its path")
    if hits:
        note(f"file:{q} is {hits[0][0]}:{hits[0][1]}, the one repo that holds it")
        return hits[0]
    if here:
        closed(here, joined, "matches nothing in any repo")
        note(f"file:{q} matches nothing in any repo; " + (f"read as {here}:{joined}" if reading
                                                          else f"taken as {here}:{joined}, a new file"))
        return here, joined
    raise catalog.CatalogError(f"file:{q} matches nothing in any repo; give its path, "
                               f"or write REPO:{q} ({', '.join(cfg.repos)})")


def read_target(cfg, ttype, name, notes) -> tuple:
    """A read's (type, name), as `list` and `context --target` match rows:
    canon_name's stored form, so an abbreviation typed on read finds what a
    write stored. A `file:` name meets file_target where a write would (a
    store with [repos], or a path in any store), each repo's stored names in
    its pool, so a departed file's rows still read. With no name it stays
    `file`, which `list` reads as every repo's."""
    repos = getattr(cfg, "repos", None) or {}
    if ttype == "file" and name is not None and (repos or is_path(name)):
        return file_target(cfg, name, {r: store.stored_names(notes, r) for r in repos},
                           reading=True)
    return ttype, canon_name(cfg, ttype, name, store.stored_names(notes, ttype))


def _dirty_paths(cfg) -> list[str]:
    """`git status --porcelain -z` paths, a rename's new name. Split on NUL:
    the line form quotes a path with a space or a non-ASCII byte, and the
    stamp kept `"caf\\303\\251.txt"`, quotes and all (2026-10-09 review).
    `--no-optional-locks`: else status rewrites the project's index under
    index.lock, and the user's own `git add` beside it refuses."""
    fields = iter(_git(cfg, "--no-optional-locks", "status", "--porcelain", "-z")
                  .stdout.split("\0"))
    out = []
    for f in fields:
        if f:
            out.append(f[3:])
            if "R" in f[:2] or "C" in f[:2]:
                next(fields, None)               # -z puts the old name after the new
    return out



def provenance_stamp(cfg, spec, repo=None):
    """Verdict kinds only: the bit, not the label, decides. A configured
    override is a configured command like any other — it runs through
    catalog.run_configured, which supplies cwd=work_root,
    stdin=DEVNULL and the command timeout, and turns a hang into
    CatalogError instead of letting it block forever. `repo`, when given,
    is added to the stamp: the listed repo it was taken in (the repos
    design, "Stamps")."""
    if not spec.verdict:
        return None
    if cfg.provenance_command:
        r = catalog.run_configured(cfg, cfg.provenance_command)
        if r.returncode:
            # A stamp printed before a failure was written at exit 0, where
            # a catalog's exit status is checked first (2026-10-09 review).
            raise ValueError(f"provenance command {cfg.provenance_command!r} exited "
                             f"{r.returncode}: {r.stderr.strip()[:400]}")
        try:
            prov = json.loads(r.stdout)
        except ValueError:
            prov = None
        # Unparsed output was stamped `provenance: null` at exit 0: the
        # unstamped row api._stamp refuses to write. Any object is a stamp;
        # one without a "sha" is a project's own schema (design spec) and
        # reads `unverifiable` by choice.
        if not isinstance(prov, dict):
            first = (r.stdout.strip().splitlines() or [""])[0]
            raise ValueError(f"provenance command {cfg.provenance_command!r} printed "
                             f"{first!r}, not a JSON object; fix [provenance] command "
                             f"in symbion.toml")
        return prov if repo is None else {**prov, "repo": repo}
    r = _git(cfg, "rev-parse", "--verify", "-q", "HEAD")
    if r.returncode != 0:
        # Plain `rev-parse HEAD` on an unborn branch prints `HEAD` and exits
        # 128; stored as the sha, it read `behind 0` once commits existed.
        branch = _git(cfg, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD"
        raise ValueError(f"no commits yet on {branch}: a verdict entry is stamped with the "
                         f"commit it ran at. Commit first, or add it with --external if "
                         f"the check read nothing in the tree")
    head = r.stdout.strip()
    # The paths, not only the flag: whether an uncommitted CLAUDE.md (no
    # verdict depends on it) should make a stamp dirty cannot be measured
    # from a boolean (2026-09-27: about a quarter of adopter stamps dirty).
    paths = _dirty_paths(cfg)
    # What a rewrite keeps, so `successor` finds the commit's new sha after
    # git prunes the old one, and the tree for its same-tree test (the
    # rewritten-stamps design, 2026-10-26). A failure stamps without them.
    # --no-show-signature: with log.showSignature set, git printed `No
    # signature` above the format line, and a stamp kept it as its date.
    r = _git(cfg, "show", "-s", "--no-show-signature", "--format=%ad%x00%s%x00%T",
             "--date=raw", head, "--", errors="replace")
    key = r.stdout.rstrip("\n").split("\0") if r.returncode == 0 else []
    return {"sha": head, "dirty": bool(paths), **({"repo": repo} if repo else {}),
            **(dict(zip(("author_date", "subject", "tree"), key)) if len(key) == 3 else {}),
            **({"dirty_count": len(paths), "dirty_paths": paths[:20]} if paths else {})}


_HEX = re.compile(r"[0-9a-fA-F]{4,64}")
_RAW_DATE = re.compile(r"\d+ [+-]\d{4}")         # git's --date=raw
# (repo, stamp, HEAD) -> (state, distance). Two commits relate the same way
# for good, so a long-running `serve` asks git once per stamp per HEAD. A
# failure is not kept: a fetch can bring the missing object.
# ponytail: grows by one entry per stamp per HEAD a process sees; bound it if
# a serve's memory ever shows it.
_RELATIONS: dict = {}


def head_sha(cfg) -> str:
    """HEAD's full id; "" on an unborn branch or outside a repository."""
    return _git(cfg, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()


_HEADS: ContextVar[dict | None] = ContextVar("symbion_heads", default=None)


@contextmanager
def heads_once():
    """Within it, check_state reads each repo's HEAD once. A page of checks
    stamped in a repo the serve does not stand in read that repo's HEAD per
    row: a home page of 200 took 2.07 s against 0.16 s (2026-10-14). A
    scope, not a TTL: a read outside one is fresh, as an in-process test
    that commits and reads back needs. A page or a command is one."""
    token = _HEADS.set({})
    try:
        yield
    finally:
        _HEADS.reset(token)


def _head_once(cfg) -> str:
    """head_sha, read once per repo inside heads_once()."""
    seen = _HEADS.get()
    if seen is None:
        return head_sha(cfg)
    key = str(_root(cfg))
    if key not in seen:
        seen[key] = head_sha(cfg)
    return seen[key]


def unmerged(cfg, shas) -> dict:
    """{sha: a branch that holds it} for each commit HEAD cannot reach. One
    walk for all: a merge-base a sha took 0.6 s over one target's resolved
    rows (2026-10-13). A commit no branch holds is left out: a squash
    or rebase merge leaves the fix's own commit on none once its branch is
    deleted, and it would read "not merged" for good. A name that is no
    commit id, or an object this clone lacks, is skipped."""
    if getattr(cfg, "repos", None):
        shas, found = list(shas), {}
        for name in cfg.repos:                    # each repo's HEAD, its own shas
            found |= unmerged(cfg.repo(name), shas)
        return found
    shas = [s for s in dict.fromkeys(shas) if _HEX.fullmatch(s)]
    out, found = [], {}
    for i in range(0, len(shas), SHA_BATCH):     # --stdin drops --ignore-missing
        r = _git(cfg, "rev-list", "--ignore-missing", *shas[i:i + SHA_BATCH], "--not", "HEAD",
                 "--")
        if r.returncode:
            return {}
        out += r.stdout.split()
    for s in shas:
        if any(o.startswith(s.lower()) for o in out):
            b = _git(cfg, "for-each-ref", "--count=1", "--contains", s,
                     "--format=%(refname:short)", "refs/heads", "refs/remotes").stdout.strip()
            if b:
                found[s] = b
    return found


def check_state(cfg, prov, head: str | None = None):
    """(state, distance). `dirty` outranks every sha relationship. `head` is
    head_sha(cfg), passed by a caller that reads many rows against one HEAD.

    `behind N` and `ahead N` are the two sides of one line of development:
    the stamp is N commits back from this HEAD, or N commits past it.
    `diverged` is kept for what it says -- neither is an ancestor of the
    other. Either of those two whose stamp no branch or tag holds is
    `dangling`: the run is on a commit only the reflog keeps. It is
    `rewritten` instead when one live commit has the stamp commit's author
    date and subject (`successor`): what an amend or a rebase made of it.

    A check stamped dirty:true satisfies sha == HEAD — the common case, since
    the stamp records HEAD at write time — so without precedence it would read
    `current`, contradicting the very thing the flag exists to record.

    An external stamp (`add --external`) outranks both: the check read
    something outside the tree, so no commit or edit bears on it.

    No stamp at all is `unstamped`: a row migrated from a store that predates
    stamping (symbion stamps every verdict it writes). Nothing about it is
    suspect, so it is not `unverifiable`, which a migrated store's many such
    rows turned into noise over the stamps that are (2026-10-03)."""
    if prov and prov.get("external"):
        return ("external", None)
    if not prov:
        return ("unstamped", None)
    if not store.stamp_sha(prov):
        return ("unverifiable", None)
    if prov.get("dirty"):
        return ("unverifiable", None)
    # A commit id, never a ref: the literal `HEAD` an unborn branch once
    # stamped read `behind 0` for good.
    stamp = prov["sha"]
    if not _HEX.fullmatch(stamp):
        return ("unverifiable", None)
    if getattr(cfg, "repos", None):
        routed = stamp_cfg(cfg, prov)
        if routed.work_root != cfg.work_root:
            head = None            # the caller's head is its own repo's
        cfg = routed
    head = _head_once(cfg) if head is None else head
    if not head:
        return ("unverifiable", None)
    key = (str(_root(cfg)), stamp, head)
    state = _RELATIONS.get(key)
    if state is None:
        # One walk answers every state: the commits only the stamp reaches,
        # and those only HEAD reaches. It took four git calls a row, three
        # quarters of a GUI page's render (2026-10-01).
        r = _git(cfg, "rev-list", "--left-right", "--count", f"{stamp}^{{commit}}...{head}", "--")
        try:
            only_stamp, only_head = map(int, r.stdout.split())
        except ValueError:
            only_stamp = only_head = None
        if r.returncode or only_stamp is None:
            # Rebased away, squashed, shallow; an object missing between the two.
            # A pruned stamp still finds its rewrite by the key it keeps.
            s = successor(cfg, prov, head)
            return ("rewritten", s.distance) if s else ("unverifiable", None)
        if only_stamp and only_head:
            state = ("diverged", None)             # another line of development
        elif only_stamp:
            # HEAD is an ancestor of the stamp: the SAME line, read from a
            # checkout that lags it. A dated baseline stamped on the default
            # branch read `diverged` -- "another line of development" -- from
            # every worktree behind it (2026-09-28).
            state = ("ahead", only_stamp)
        else:
            state = ("behind", only_head) if only_head else ("current", 0)
        _RELATIONS[key] = state
    # A stamp HEAD cannot reach may sit on no branch at all: an amend or a
    # rebase after the check left it to the reflog, and it read `diverged`,
    # which says "look on that branch" where this says "re-run" (2026-10-06).
    # Asked on every read: a branch can go while a serve keeps one HEAD. A
    # git error keeps the relation, as before this test existed, unless git
    # has pruned the stamp since the relation was kept: then it reads as the
    # walk above reads a missing stamp.
    if state[0] in ("ahead", "diverged"):
        r = _git(cfg, "for-each-ref", "--count=1", "--contains", stamp,
                 "refs/heads", "refs/remotes", "refs/tags")
        if r.returncode == 0 and not r.stdout.strip():
            # After each rewrite, agents typed old-to-new sha maps into their
            # checks by hand (2026-10-26): git's own metadata names the commit.
            s = successor(cfg, prov, head)
            return ("rewritten", s.distance) if s else ("dangling", None)
        if r.returncode and _git(cfg, "cat-file", "-e", f"{stamp}^{{commit}}").returncode:
            del _RELATIONS[key]
            s = successor(cfg, prov, head)
            return ("rewritten", s.distance) if s else ("unverifiable", None)
    return state


class Successor(NamedTuple):
    """The live commit a rewrite made of a stamp's commit. `state` and
    `distance` are its own relation to HEAD; `same_tree` is None when the
    old tree is unknown."""
    sha: str
    state: str
    distance: int | None
    same_tree: bool | None


def _live_commits(cfg, since: int) -> dict:
    """{(author date, subject): [(sha, tree)]} over the refs the dangling
    test reads, committed from a day before `since` (epoch seconds) on. A
    rebase dates its commits now and filter-repo keeps the old dates, so a
    successor is not older than the stamp's author date; the day is for
    skew. A full walk of a 63,000-commit repo took 0.8 s, bounded 16 ms
    (2026-10-26). Kept for one heads_once scope, not across scopes: a serve
    keeps one HEAD while an agent rewrites a branch in another worktree."""
    seen, key = _HEADS.get(), ("live", str(_root(cfg)))
    if seen is not None and key in seen and seen[key][0] <= since:
        return seen[key][1]
    # A second build in a scope walks the whole history: `list` reads newest
    # first, and each older stamp lowered the bound and built it again.
    # ponytail: two dangling stamps of different ages make every page that
    # shows them walk it all (0.8 s at 63,000 commits); widen the bound
    # geometrically if a big repo's store shows that.
    widen = seen is not None and key in seen
    floor = datetime.fromtimestamp(since - 86400, timezone.utc).isoformat()
    r = _git(cfg, "log", "--branches", "--remotes", "--tags", "--no-show-signature",
             *(() if widen else (f"--since={floor}",)),
             "--format=%H%x00%ad%x00%s%x00%T", "--date=raw", "--", errors="replace")
    live: dict = {}
    # On "\n" only: splitlines() also splits at U+2028 in a subject.
    for record in r.stdout.split("\n") if r.returncode == 0 else ():
        if len(f := record.split("\0")) == 4:
            live.setdefault((f[1], f[2]), []).append((f[0], f[3]))
    if seen is not None:
        seen[key] = (-math.inf if widen else since, live)
    return live


def successor(cfg, prov, head: str | None = None) -> Successor | None:
    """The one live commit with the stamp commit's author date and subject:
    what an amend, a rebase, a cherry-pick or filter-repo made of it. None
    for no match or several, as after a reword or a squash. Asked once per
    stamp in a heads_once scope: `check_state` and the printer both ask."""
    sha = store.stamp_sha(prov)
    if not isinstance(sha, str) or not _HEX.fullmatch(sha):
        return None
    if getattr(cfg, "repos", None):
        routed = stamp_cfg(cfg, prov)
        if routed.work_root != cfg.work_root:
            head = None            # the caller's head is its own repo's
        cfg = routed
    head = _head_once(cfg) if head is None else head
    # The stored key too: a bare stamp of the same pruned sha finds none.
    seen, key = _HEADS.get(), ("successor", str(_root(cfg)), sha, head,
                               *(str(prov.get(k)) for k in ("author_date", "subject", "tree")))
    if seen is not None and key in seen:
        return seen[key]
    found = _successor(cfg, prov, sha, head)
    if seen is not None:
        seen[key] = found
    return found


def _successor(cfg, prov, sha, head) -> Successor | None:
    """successor's search. The key is the stamp's own (provenance_stamp
    keeps it), else git's while the old commit exists: also when the stamp's
    date is not git's raw one, as a [provenance] command may write it."""
    full, date, subject, tree = sha, prov.get("author_date"), prov.get("subject"), prov.get("tree")
    if not (isinstance(date, str) and isinstance(subject, str) and _RAW_DATE.fullmatch(date)):
        r = _git(cfg, "show", "-s", "--no-show-signature", "--format=%H%x00%ad%x00%s%x00%T",
                 "--date=raw", f"{sha}^{{commit}}", "--", errors="replace")
        got = r.stdout.rstrip("\n").split("\0") if r.returncode == 0 else []
        if len(got) != 4:
            return None
        full, date, subject, tree = got[0], got[1], got[2], tree or got[3]
    try:
        since = int(date.split()[0])
    except (ValueError, IndexError):
        return None
    hits = [h for h in _live_commits(cfg, since).get((date, subject), ())
            if not h[0].startswith(full.lower())]
    if len(hits) != 1:
        return None
    new, new_tree = hits[0]
    state, distance = check_state(cfg, {"sha": new, "dirty": False}, head)
    if state not in ("current", "behind", "ahead", "diverged"):
        return None
    return Successor(new, state, distance, None if not tree else tree == new_tree)


def branch_commits(cfg, ref: str, since: str | None = None) -> set:
    """A range git cannot read is an error, not an empty set: `context
    --branch no-such-ref`, or a branch in a repo whose default is not the
    configured one, read `0 notes` at exit 0 (2026-09-27)."""
    base = since or cfg.default_branch
    if getattr(cfg, "work_root", None) is None and getattr(cfg, "repos", None):
        # git's `not a git repository`, where a check or a catalog names the
        # repos (the repos review).
        raise ValueError(f"--branch reads the repo you stand in, and {Path.cwd()} is in none "
                         f"of this store's repos ({', '.join(cfg.repos)}); run from one of them")
    r = _git(cfg, "rev-list", f"{base}..{ref}")
    if r.returncode != 0:
        why = (r.stderr.strip().splitlines() or ["rev-list failed"])[0]
        raise ValueError(f"git cannot list {base}..{ref}: {why}; the base is --since REF, "
                         f"else default_branch in symbion.toml ({cfg.default_branch!r})")
    return set(r.stdout.split())


SHA_BATCH = 500                 # full shas per git call, well under 32,767 characters


def subjects(cfg, shas) -> dict:
    """sha -> subject line, in batched calls.

    `--ignore-missing` is load-bearing: without it, `log --no-walk` on a batch
    containing even one unresolvable sha (rebased away, squashed, shallow
    clone — exactly what canonical_commit and check_state elsewhere in this
    module anticipate) exits 128 with EMPTY stdout, blanking every subject in
    the batch, not just the missing one. With the flag, unknown shas are
    dropped and every known sha in the same batch still resolves; a caller
    then renders a bare sha only for the ones actually missing.

    `--end-of-options` too: a commit name git does not know is stored as
    typed, and `--output=PATH` among the shas wrote the log to PATH.

    In batches of SHA_BATCH: a Windows command line holds at most 32,767
    characters, and 800 full shas raised WinError 206 there (2026-10-16).
    `--stdin` would not do: git drops `--ignore-missing` for revisions it
    reads from stdin, and one unknown sha failed the batch.

    Any commit's subject, read as the rewrite lookup reads it: bytes not
    UTF-8, a U+2028, or a signature line on stdout raised or split a record
    (2026-10-26)."""
    out = {}
    shas = list(shas)
    if not shas:
        return out
    if getattr(cfg, "repos", None):
        for name in cfg.repos:                    # each repo names its own shas
            if left := [s for s in shas if s not in out]:
                out |= subjects(cfg.repo(name), left)
        return out
    for i in range(0, len(shas), SHA_BATCH):
        r = _git(cfg, "log", "--no-walk", "--ignore-missing", "--no-show-signature",
                 "--format=%H%x00%s", "--end-of-options", *shas[i:i + SHA_BATCH],
                 errors="replace")
        if r.returncode != 0:
            continue
        for line in r.stdout.split("\n"):
            if "\0" in line:
                sha, subject = line.split("\0", 1)
                out[sha] = subject
    return out


def recent(cfg, n: int) -> dict:
    """sha -> subject for the newest `n` commits on HEAD, newest first: the
    head of the GUI's commit picker (the user, 2026-10-16). Over a store's
    several repos, the newest `n` of them all, by commit time. Where git
    cannot answer (no repo, no commit yet) it is empty."""
    got = []
    for c in [cfg.repo(r) for r in cfg.repos] if getattr(cfg, "repos", None) else [cfg]:
        r = _git(c, "log", f"-n{n}", "--no-show-signature", "--format=%ct%x00%H%x00%s",
                 errors="replace")
        if r.returncode == 0:
            got += [ln.split("\0", 2) for ln in r.stdout.split("\n") if ln.count("\0") == 2]
    got.sort(key=lambda t: -int(t[0]))        # stable: one repo keeps git's order
    return {sha: subject for _, sha, subject in got[:n]}


def oneline(cfg, rng: str, n: int) -> list[str]:
    """`git log --oneline` of a range, newest first, at most `n` lines; []
    when git cannot read the range."""
    r = _git(cfg, "log", "--no-show-signature", "--format=%h %s", f"-n{n}", rng,
             errors="replace")
    return [ln for ln in r.stdout.split("\n") if ln] if r.returncode == 0 else []


def _file(cfg, ttype, name) -> str | None:
    """`name` when `ttype` is a catalog type and `name` a file in this
    checkout. A catalog lists other things too (URLs, part numbers), so
    the file must be here, and inside the checkout."""
    root = _root(cfg)
    if root is None or not name or ttype not in cfg.catalogs or ttype == "doc":
        return None                  # a doc is the store's file, not the checkout's
    root = Path(root).resolve()
    p = (root / name).resolve()
    return name if p.is_file() and p.is_relative_to(root) else None


def target_blob(cfg, ttype, name) -> str | None:
    """The blob id of a row's target file as the row saw it, uncommitted
    edits included. `hash-object` applies the file's filters, so this is the
    blob git stores once that content is committed: commits_since anchors
    on it."""
    return target_blobs(cfg, ttype, [name]).get(name)


def target_blobs(cfg, ttype, names) -> dict:
    """`target_blob` for many names in one `hash-object`: a seed sweep stamps
    a whole catalog. A name that is no file here has no entry."""
    cfg = cfg.for_type(ttype)
    return _blobs(cfg, [n for n in dict.fromkeys(names) if _file(cfg, ttype, n)])


def _blobs(cfg, paths) -> dict:
    """path -> the blob `hash-object` makes of it, filters applied; {} when
    git fails. The paths go on stdin, C-quoted: git unquotes a line that
    starts with `"`, and a name that did failed the whole call."""
    if not paths:
        return {}
    quoted = "".join('"' + p.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                     + '"\n' for p in paths)
    r = _git(cfg, "hash-object", "--stdin-paths", input=quoted)
    out = r.stdout.split() if r.returncode == 0 else []
    return dict(zip(paths, out)) if len(out) == len(paths) else {}


# (repo, HEAD's sha, the question) -> git's answer, for changed_since: a file
# history read back from one commit, and the commits from an anchor to it,
# are the same for good. A serve page of 100 cards asked again on every load,
# a quarter second of it on a store whose repo has a merge (the user,
# 2026-10-09). A failure is not kept.
# ponytail: grows with each HEAD and set of files a process asks about, as
# _RELATIONS does; bound it if a serve's memory ever shows it.
_HISTORY: dict = {}


def _cached_git(cfg, key, *args, input: str | None = None):
    """`_git(cfg, *args)`'s stdout, or None when git failed; kept under
    `key` when `key` names a commit (an unborn HEAD names none)."""
    full = (str(_root(cfg)), *key)
    if key[0] and full in _HISTORY:
        return _HISTORY[full]
    r = _git(cfg, *args, input=input)
    out = r.stdout if r.returncode == 0 else None
    if key[0] and out is not None:
        _HISTORY[full] = out
    return out


def _file_history(cfg, paths, head: str) -> tuple[dict, float]:
    """(path -> [(sha, committer time, blob after, position)], the position
    of the first merge), in one walk in topological order: a commit before
    its parents. A merge has no `--raw` record, so no path lists one; the
    walk lists those that matter to `paths`. NUL-delimited: a header
    `SHA TIME PARENTS`, then per changed path a `:modes blobs status` record
    and the path. No paths, no walk: an empty pathspec reads every commit.
    The revision and the pathspecs go on stdin, which git reads as it reads
    them on argv, `:(literal)` included."""
    if not paths:
        return {}, math.inf
    spec = "".join(f":(literal){p}\n" for p in paths)
    out = _cached_git(cfg, (head, "log", *paths), "log", "--stdin", "-z", "--topo-order",
                      "--no-renames", "--raw", "--no-abbrev", "--format=%H %ct %P",
                      input=f"{head or 'HEAD'}\n--\n{spec}")
    toks = out.split("\0") if out is not None else []
    hist, top, merge, i, pos = {}, None, None, 0, -1
    while i < len(toks):
        t = toks[i].lstrip("\n")
        if t.startswith(":") and top and i + 1 < len(toks):
            hist.setdefault(toks[i + 1], []).append((*top, t.split()[3], pos))
            i += 2
            continue
        if t:
            sha, ct, *parents = t.split()
            pos += 1
            top = (sha, int(ct))
            if len(parents) > 1 and merge is None:
                merge = pos
        i += 1
    return hist, math.inf if merge is None else merge


def changed_since(cfg, notes, head: str | None = None) -> dict:
    """id -> the commits, newest first, that changed the row's target file
    after the content the row saw; None when the target is no file in this
    checkout.

    The list is empty while the file is what the row saw (`target_blob`).
    Otherwise it starts after the newest commit that holds that content and
    goes by ancestry: the commit carrying an edit the row was written over is
    not in it, and a merged branch's commits are, whatever their dates.
    Merge commits are left out; the commits they bring are in. A row with no
    stamp (written before stamping), or whose content no commit holds (edited
    again before the commit, or rewritten by a rebase), gets the commits
    dated after its chain's first row: a revision inherits the stamp, and
    a row with none keeps the time the target was read. `head` is
    head_sha(cfg), passed by a caller that has it; git's answers are kept
    for it (_HISTORY).

    In a store with [repos], each row's file is asked of its own repo.
    `head` is the HEAD of the repo `cfg` stands in; another repo's is read
    once per call."""
    if not getattr(cfg, "repos", None):
        return _changed_since(cfg, notes, head)
    groups = {}
    for n in notes:
        groups.setdefault(n.target.type if n.target.type in cfg.repos else None, []).append(n)
    out = {}
    for name, rows in groups.items():
        at = cfg if name is None else cfg.repo(name)
        out |= _changed_since(at, rows, head if at.work_root == cfg.work_root else None)
    return out


def _changed_since(cfg, notes, head: str | None = None) -> dict:
    """changed_since in one repo: `cfg`'s roots, no routing."""
    files = {n.id: _file(cfg, n.target.type, n.target.name) for n in notes}
    now = _blobs(cfg, sorted({p for p in files.values() if p}))
    moved = sorted({files[n.id] for n in notes if files[n.id]
                    and now.get(files[n.id]) != n.target_blob})
    if moved and head is None:
        head = head_sha(cfg)
    hist, merge = _file_history(cfg, moved, head)
    anchored, out = {}, {}
    for n in notes:
        path = files[n.id]
        if path is None or (n.target_blob and now.get(path) == n.target_blob):
            out[n.id] = None if path is None else []
            continue
        commits = hist.get(path, [])
        t = store.written_at(n, n.first_written).timestamp()
        out[n.id] = [sha for sha, ct, *_ in commits if ct > t]
        anchor, at = next(((sha, pos) for sha, _, blob, pos in commits
                           if blob == n.target_blob), (None, None))
        if anchor and at < merge:
            # No merge above the anchor: every commit listed before it is a
            # descendant of it, and every descendant is listed before it.
            out[n.id] = [sha for sha, _, _, pos in commits if pos < at]
            continue
        if anchor and (path, anchor) not in anchored:
            c = _cached_git(cfg, (head, "rev-list", anchor, path), "rev-list", "--no-merges",
                            f"{anchor}..{head or 'HEAD'}", "--", f":(literal){path}")
            anchored[path, anchor] = c.split() if c is not None else None
        if anchored.get((path, anchor)) is not None:
            out[n.id] = anchored[path, anchor]
    return out


def commits_since(cfg, notes, head: str | None = None) -> dict:
    """id -> how many commits changed_since lists, None where it has none:
    the count every read prints."""
    return {k: None if v is None else len(v)
            for k, v in changed_since(cfg, notes, head).items()}


def uncommitted(store_dir) -> tuple[int, bool]:
    """(entries not committed, registry modified).

    The entries `commit` would take — a count git status cannot give, since
    200 appends are one changed path. A line count read an entry edited
    twice before a commit as two (2026-10-14). arcs.jsonl is rewritten in
    place, so a line delta there is meaningless; it is a boolean. Computed
    unconditionally, never only after a write."""
    # ponytail: an untracked notes.jsonl beside a HEAD (only by hand-run git)
    # reads 0; pending_rows would need an ls-files to read it whole.
    return len(store.entries(store.pending_rows(store_dir))), registry_modified(store_dir)


def registry_modified(store_dir) -> bool:
    """arcs.jsonl changed in the work tree, staged, or untracked and not
    empty. One status call, without the index lock (store.pending_rows): the
    page shell read it with three, and the pending rows twice (2026-10-16)."""
    store_dir = Path(store_dir)
    out = _git(store_dir, "--no-optional-locks", "status", "--porcelain", "-z", "--",
               store.ARCS_FILE).stdout
    for entry in filter(None, out.split("\0")):
        if not entry.startswith("??") or (store_dir / entry[3:]).read_text(
                encoding="utf-8").strip():
            return True
    return False


def committed(store_dir) -> str:
    """What HEAD took, as `commit` and the GUI's button say it: the `Rows:`
    trailer, then by name every path but the rows' file. `add -A` takes
    every path in the store, and a plan, then a script, went in under a line
    that named only the rows (2026-10-02, 2026-10-05); `arcs.jsonl` went in
    unnamed, though the dry run listed it (2026-10-15). `-z`: a name git
    would quote prints as written."""
    rows = _git(store_dir, "log", "-1", "--format=%(trailers:key=Rows,valueonly)").stdout.strip()
    paths = [p for p in _git(store_dir, "show", "-z", "--name-only", "--format=", "HEAD")
             .stdout.split("\0") if p.strip()]
    if not paths:                                 # a wrap with nothing pending
        return "committed: the message alone, no entries or files"
    files = [p for p in paths if p != store.NOTES_FILE]
    said = [f"entries {rows}"] if rows else []
    if files:
        more = f", +{len(files) - 3} more" if len(files) > 3 else ""
        said.append(f"{len(files)} file{'' if len(files) == 1 else 's'}: "
                    f"{', '.join(files[:3])}{more}")
    return "committed" + (": " + "; ".join(said) if said else "")


def last_wrap(store_dir, cfg, side: str | None = None) -> dict | None:
    """The newest store commit a wrap made (`commit --wrap`, a `Wrap:`
    trailer), as summary prints it: subject, body without the trailers, who
    wrapped, when, and how many commits the repo's HEAD gained since (None
    outside a repo). None when no commit carries the trailer. `--since` is
    inclusive to the second, so a repo commit in the wrap's own second
    counts. In a store with [repos], every repo's commits, and
    `commits_by_repo` names each repo that has some.

    With `side`, the newest wrap whose `Side:` trailer is `side` or absent:
    a shared store holds two sides' wraps, and the other's is not your
    handoff. One `git log`, read newest-first; -z and NUL between the fields,
    since no commit text holds a NUL (a subject can hold a U+2028, which
    splitlines would cut)."""
    # ponytail: with a side, every wrap's text comes back, not the first match's;
    # thousands of wraps pay for it. Upgrade: log --format=%H, then show the match.
    out = _git(store_dir, "log", "-z", "--no-show-signature", "--grep=^Wrap: ",
               *(["-1"] if side is None else []),
               "--format=%cI%x00%(trailers:key=Wrap,valueonly)%x00"
               "%(trailers:key=Side,valueonly)%x00%(trailers)%x00%B").stdout
    f = out.split("\0")
    for i in range(0, len(f) - 4, 5):
        at, who, got, trailers, full = f[i:i + 5]
        if side is None or got.strip() in ("", side):
            break
    else:
        return None
    gist, _, body = full.strip().removesuffix(trailers.strip()).strip().partition("\n")
    def since(c):
        n = _git(c, "rev-list", "--count", "HEAD", f"--since={at}").stdout.strip()
        return int(n) if n.isdigit() else None
    by_repo = None
    if getattr(cfg, "repos", None):
        counts = {name: since(cfg.repo(name)) for name in cfg.repos}
        known = [v for v in counts.values() if v is not None]
        n = sum(known) if known else None
        by_repo = {k: v for k, v in counts.items() if v}
    else:
        n = since(cfg)
    # %cI is in the committer's offset; rows are stamped in UTC, so this is too.
    at = datetime.fromisoformat(at).astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"gist": gist, "body": body.strip(), "author": who.strip(), "at": at,
            "commits_since": n, "commits_by_repo": by_repo}


def wraps_since(store_dir, since_iso: str | None) -> dict[str, int]:
    """Wrap commits per `Side:` trailer since `since_iso` (every one when
    None). A wrap with no `Side:` names no side, so it counts for none."""
    out = _git(store_dir, "log", "-z", "--no-show-signature", "--grep=^Wrap: ",
               *([f"--since={since_iso}"] if since_iso else []),
               "--format=%(trailers:key=Side,valueonly)").stdout
    return dict(Counter(v for v in map(str.strip, out.split("\0")) if v))


def unpushed(store) -> int | None:
    """Local commits on no remote-tracking ref, or None when the store has
    no remote at all.

    `--branches --not --remotes`, never `@{u}..`: a branch with NO upstream
    makes the latter error and print nothing, which piped to a count reads as
    "0 unpushed" (measured 2026-09-17: local-only commits read as 0). Not
    `--all`: it counts refs/stash, and one stash read as 2 unpushed that no
    push could send. With no remote the negation is empty and every commit
    would count, so that case is None -- "nowhere to push", not a number."""
    store = Path(store)
    if not _git(store, "remote").stdout.strip():
        return None
    out = _git(store, "rev-list", "--count", "--branches", "--not", "--remotes").stdout.strip()
    return int(out) if out.isdigit() else None


def set_upstream(store, write: bool = True) -> str | None:
    """Point the store's branch at origin/<branch> when it tracks nothing and
    an `origin` exists, so a bare `git push` works. `git push origin --all`
    sets no upstream (measured 2026-09-22: a later bare push did nothing),
    and `--set-upstream-to` refuses until the remote branch has been fetched,
    so this writes the two config keys `push -u` writes. Returns the
    upstream it set (or, with `write=False`, would set), else None."""
    store = Path(store)
    if "origin" not in _git(store, "remote").stdout.split():
        return None
    branch = _git(store, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if not branch or _git(store, "config", "--get", f"branch.{branch}.remote").returncode == 0:
        return None
    if not write:
        return f"origin/{branch}"
    _git(store, "config", f"branch.{branch}.remote", "origin")
    _git(store, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    return f"origin/{branch}"


_MERGE_ATTRS = ("notes.jsonl merge=symbion", "arcs.jsonl merge=symbion")


def ensure_merge_driver(store) -> bool:
    """Register `symbion merge-driver` for the store's clone: the config keys
    and the two attribute lines, in the clone's own git dir, so nothing is
    committed and a clone without symbion merges as git does. The attributes
    file lives in the common dir of a linked worktree, so git names it.
    Returns whether it changed anything."""
    store = Path(store)
    out = _git(store, "rev-parse", "--git-path", "info/attributes")
    if out.returncode != 0:
        return False
    attrs = store / out.stdout.strip()               # absolute when git says so
    # sh word: Windows backslashes would be read as escapes
    # -P: git runs the driver from the repo root, and `-m` would put that
    # directory first on sys.path, so a file the store commits could shadow a module
    driver = f"{shlex.quote(Path(sys.executable).as_posix())} -P -m symbion merge-driver %O %A %B %P"
    changed = False
    for key, value in (("merge.symbion.name", "symbion store merge"),
                       ("merge.symbion.driver", driver)):
        if _git(store, "config", "--local", "--get", key).stdout.strip() != value:
            _git(store, "config", "--local", key, value)
            changed = True
    text = attrs.read_text(encoding="utf-8") if attrs.exists() else ""
    missing = [ln for ln in _MERGE_ATTRS if ln not in text.splitlines()]
    if missing:
        attrs.parent.mkdir(parents=True, exist_ok=True)
        lead = "\n" if text and not text.endswith("\n") else ""
        with open(attrs, "a", encoding="utf-8", newline="\n") as f:
            f.write(lead + "".join(ln + "\n" for ln in missing))
        changed = True
    return changed


def run_timed(argv, timeout: float) -> subprocess.CompletedProcess:
    """Run `argv` with its output captured, stdin closed and git unable to
    ask for a password. Past `timeout` seconds it is stopped and
    `subprocess.TimeoutExpired` is raised. On POSIX it runs in a session of
    its own and the stop reaches every process it started: an ssh git ran
    has no terminal to prompt on, and none outlives it holding the pipe. On
    Windows taskkill stops its process tree: kill() stops git alone, and the
    wait for the pipe its child holds would outlast the timeout."""
    posix = os.name == "posix"
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                          start_new_session=posix) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if posix:
                with suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
            else:
                # By path: a bare name is looked up in the cwd first, which may be the store.
                kill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                    "taskkill.exe")
                with suppress(OSError, subprocess.SubprocessError):
                    subprocess.run([kill, "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, stdin=subprocess.DEVNULL, timeout=5)
                p.kill()                         # if taskkill could not
            p.communicate()
            raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def fetch(store, timeout: float) -> str | None:
    """`git fetch origin`, stopped past `timeout` seconds (run_timed). None
    when it worked, else one line that says why."""
    try:
        r = run_timed(["git", "-C", str(store), "fetch", "-q", "origin"], timeout)
    except subprocess.TimeoutExpired:
        return f"git fetch origin took over {timeout:g}s and was stopped"
    if r.returncode == 0:
        return None
    said = next((ln.strip() for ln in r.stderr.split("\n") if ln.strip()),
                f"exit {r.returncode}, no output")
    return f"git fetch origin failed: {said}"


def behind(store) -> int | None:
    """The commits on the branch's upstream that HEAD lacks, as of the last
    fetch; None with no upstream."""
    out = _git(Path(store), "rev-list", "--count", "HEAD..@{u}").stdout.strip()
    return int(out) if out.isdigit() else None


_FETCH_MARK = "symbion-fetched"


def _git_file(store, name) -> Path | None:
    out = _git(Path(store), "rev-parse", "--git-path", name)
    return Path(store) / out.stdout.strip() if out.returncode == 0 else None


def stamp_fetch(store) -> None:
    """Record that a fetch was tried, whatever came of it. FETCH_HEAD moves
    only when a fetch works, so an origin that is down would be tried again
    by every write."""
    if mark := _git_file(store, _FETCH_MARK):
        with suppress(OSError):
            mark.touch()


def fetched_age(store) -> float | None:
    """Seconds since the store's last fetch, by FETCH_HEAD's time or the
    last try's (stamp_fetch), whichever is newer; None before the first."""
    times = []
    for name in ("FETCH_HEAD", _FETCH_MARK):
        if path := _git_file(store, name):
            with suppress(FileNotFoundError):
                times.append(path.stat().st_mtime)
    return time.time() - max(times) if times else None
