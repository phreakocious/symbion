"""Git is symbion's one authoritative catalog and its provenance source."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import catalog, store


def _git(cfg_or_path, *args):
    """HEAD, branches and dirtiness are per-worktree, so this must run
    against `work_root` (the CURRENT checkout), never `project_root` (which
    is deliberately the MAIN checkout -- see config.project_root). A bare
    path (as `uncommitted(store)` passes) has neither attribute and is used
    as-is."""
    root = _root(cfg_or_path)
    if root is None:
        # A store named from outside any repository (api.resolve): every git
        # question answers as it does for a departed sha -- a failed run --
        # so a check reads `unverifiable`, a subject falls back to its sha.
        return subprocess.CompletedProcess(["git", *args], 128, "", "not a git repository")
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


def _root(cfg_or_path):
    return getattr(cfg_or_path, "work_root", None) or \
        getattr(cfg_or_path, "project_root", cfg_or_path)


def canonical_commit(cfg, ref: str) -> str:
    """Peel to a COMMIT object and return its full id.

    `^{commit}` is what makes this a commit target rather than an object
    target: plain --verify accepts HEAD^{tree} and returns a tree id. On a miss
    (rebased away, shallow clone) fall back to the typed string, like any
    departed target."""
    r = _git(cfg, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ref


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


def canon_name(cfg, target_type, name):
    """The stored/queried form of a target name -- shared by the write side
    (`add`) and the read side (`list --name`, `context --target`), so an
    abbreviation used on write also matches on read.

    `commit` gets the one built-in exception: peeled to a full object id, so
    a symbolic ref (HEAD, a branch) is frozen at write time instead of
    silently drifting as the ref advances (design spec, commit targets). This lives in gitref,
    not catalog, because it needs canonical_commit; catalog must not import
    gitref (that would invert the layering), but gitref already imports
    catalog, so this is the one place both sides can share it from."""
    if name is None:
        return None
    if target_type == "commit":
        return canonical_commit(cfg, name)
    if target_type in catalog.NON_CANONICAL_TYPES:
        return catalog.nfc(name)
    return catalog.canonical(cfg, target_type, name)


def _dirty_paths(cfg) -> list[str]:
    """`git status --porcelain` paths, a rename's new name."""
    lines = _git(cfg, "status", "--porcelain").stdout.splitlines()
    return [ln[3:].split(" -> ")[-1] for ln in lines if ln.strip()]



def provenance_stamp(cfg, spec):
    """Verdict kinds only: the bit, not the label, decides. A configured
    override is a configured command like any other — it runs through
    catalog.run_configured, which supplies cwd=work_root,
    stdin=DEVNULL and the command timeout, and turns a hang into
    CatalogError instead of letting it block forever."""
    if not spec.verdict:
        return None
    if cfg.provenance_command:
        r = catalog.run_configured(cfg, cfg.provenance_command)
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
        return prov
    r = _git(cfg, "rev-parse", "--verify", "-q", "HEAD")
    if r.returncode != 0:
        # Plain `rev-parse HEAD` on an unborn branch prints `HEAD` and exits
        # 128; stored as the sha, it read `behind 0` once commits existed.
        branch = _git(cfg, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD"
        raise ValueError(f"no commits yet on {branch}: a verdict row is stamped with the "
                         f"commit it ran at. Commit first, or add it with --external if "
                         f"the check read nothing in the tree")
    head = r.stdout.strip()
    # The paths, not only the flag: whether an uncommitted CLAUDE.md (no
    # verdict depends on it) should make a stamp dirty cannot be measured
    # from a boolean (2026-09-27: 26% of 536 adopter stamps dirty).
    paths = _dirty_paths(cfg)
    return {"sha": head, "dirty": bool(paths),
            **({"dirty_count": len(paths), "dirty_paths": paths[:20]} if paths else {})}


_HEX = re.compile(r"[0-9a-fA-F]{4,64}")
# (repo, stamp, HEAD) -> (state, distance). Two commits relate the same way
# for good, so a long-running `serve` asks git once per stamp per HEAD. A
# failure is not kept: a fetch can bring the missing object.
# ponytail: grows by one entry per stamp per HEAD a process sees; bound it if
# a serve's memory ever shows it.
_RELATIONS: dict = {}


def head_sha(cfg) -> str:
    """HEAD's full id; "" on an unborn branch or outside a repository."""
    return _git(cfg, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()


def check_state(cfg, prov, head: str | None = None):
    """(state, distance). `dirty` outranks every sha relationship. `head` is
    head_sha(cfg), passed by a caller that reads many rows against one HEAD.

    `behind N` and `ahead N` are the two sides of one line of development:
    the stamp is N commits back from this HEAD, or N commits past it.
    `diverged` is kept for what it says -- neither is an ancestor of the
    other.

    A check stamped dirty:true satisfies sha == HEAD — the common case, since
    the stamp records HEAD at write time — so without precedence it would read
    `current`, contradicting the very thing the flag exists to record.

    An external stamp (`add --external`) outranks both: the check read
    something outside the tree, so no commit or edit bears on it."""
    if prov and prov.get("external"):
        return ("external", None)
    if not store.stamp_sha(prov):
        return ("unverifiable", None)
    if prov.get("dirty"):
        return ("unverifiable", None)
    # A commit id, never a ref: the literal `HEAD` an unborn branch once
    # stamped read `behind 0` for good.
    stamp = prov["sha"]
    if not _HEX.fullmatch(stamp):
        return ("unverifiable", None)
    head = head_sha(cfg) if head is None else head
    if not head:
        return ("unverifiable", None)
    key = (str(_root(cfg)), stamp, head)
    if key in _RELATIONS:
        return _RELATIONS[key]
    # One walk answers every state: the commits only the stamp reaches, and
    # those only HEAD reaches. It took four git calls a row, three quarters
    # of a GUI page's render (2026-10-01).
    r = _git(cfg, "rev-list", "--left-right", "--count", f"{stamp}^{{commit}}...{head}", "--")
    try:
        only_stamp, only_head = map(int, r.stdout.split())
    except ValueError:
        only_stamp = only_head = None
    if r.returncode or only_stamp is None:
        # Rebased away, squashed, shallow; an object missing between the two.
        return ("unverifiable", None)
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
    return state


def branch_commits(cfg, ref: str, since: str | None = None) -> set:
    """A range git cannot read is an error, not an empty set: `context
    --branch no-such-ref`, or a branch in a repo whose default is not the
    configured one, read `0 notes` at exit 0 (2026-09-27)."""
    base = since or cfg.default_branch
    r = _git(cfg, "rev-list", f"{base}..{ref}")
    if r.returncode != 0:
        why = (r.stderr.strip().splitlines() or ["rev-list failed"])[0]
        raise ValueError(f"git cannot list {base}..{ref}: {why}; the base is --since REF, "
                         f"else default_branch in symbion.toml ({cfg.default_branch!r})")
    return set(r.stdout.split())


def subjects(cfg, shas) -> dict:
    """sha -> subject line, in one batched call.

    `--ignore-missing` is load-bearing: without it, `log --no-walk` on a batch
    containing even one unresolvable sha (rebased away, squashed, shallow
    clone — exactly what canonical_commit and check_state elsewhere in this
    module anticipate) exits 128 with EMPTY stdout, blanking every subject in
    the batch, not just the missing one. With the flag, unknown shas are
    dropped and every known sha in the same batch still resolves; a caller
    then renders a bare sha only for the ones actually missing."""
    out = {}
    if not shas:
        return out
    r = _git(cfg, "log", "--no-walk", "--ignore-missing", "--format=%H%x00%s", *shas)
    if r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        if "\0" in line:
            sha, subject = line.split("\0", 1)
            out[sha] = subject
    return out


def oneline(cfg, rng: str, n: int) -> list[str]:
    """`git log --oneline` of a range, newest first, at most `n` lines; []
    when git cannot read the range."""
    r = _git(cfg, "log", "--format=%h %s", f"-n{n}", rng)
    return r.stdout.splitlines() if r.returncode == 0 else []


def uncommitted(store) -> tuple[int, bool]:
    """(new note rows, registry modified).

    notes.jsonl is append-only, so ADDED LINES are new notes — a count git
    status cannot give, since 200 appends are one changed path. arcs.jsonl
    is rewritten in place, so a line delta there is meaningless; it is a
    boolean. Computed unconditionally, never only after a write."""
    store = Path(store)
    notes, registry = 0, False
    for args in (["diff", "--numstat"], ["diff", "--cached", "--numstat"]):
        r = _git(store, *args)
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, _removed, path = parts
            if path.endswith("notes.jsonl") and added.isdigit():
                notes += int(added)
            elif path.endswith("arcs.jsonl"):
                registry = True
    untracked = _git(store, "ls-files", "--others", "--exclude-standard").stdout.split()
    for rel in untracked:
        if rel.endswith("notes.jsonl"):
            with open(store / rel, encoding="utf-8") as f:
                notes += sum(1 for _ in f)
        elif rel.endswith("arcs.jsonl"):
            registry = registry or bool((store / rel).read_text().strip())
    return notes, registry


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
