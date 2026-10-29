"""Catalogs are commands. A catalog type is authoritative because you re-run
it. A resolver is a command too: it replaces the match rule for one type."""
from __future__ import annotations

import contextlib
import difflib
import os
import re
import shutil
import signal
import subprocess
import sys
import unicodedata
from pathlib import Path

NON_CANONICAL_TYPES = {"item", "project", "arc", "commit"}
DEFAULT_COMMAND_TIMEOUT = 30


class CatalogError(RuntimeError):
    pass


class AmbiguousName(ValueError):
    def __init__(self, query, candidates):
        super().__init__(f"{query!r} matches {len(candidates)}: {candidates}")
        self.query, self.candidates = query, candidates


def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def posix_sh() -> str | None:
    """The sh a configured command runs in on Windows: one on PATH, else the
    one Git for Windows installs beside its git: Git/bin/sh.exe, from
    Git/cmd/git.exe (its installer puts only Git/cmd on PATH) or from
    Git/mingw64/bin or Git/ucrt64/bin (Git Bash puts those first)."""
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    for d in Path(git).resolve().parents[1:3] if git else ():
        if (d / "bin" / "sh.exe").exists():
            return str(d / "bin" / "sh.exe")
    return None


# The store's own docs/, built in so `--ref doc:NAME` holds in every store: a
# row's long detail lives in a file there (the user, 2026-10-14). A name is
# its path within docs/: `doc:docs/x.md` said docs twice (the user,
# 2026-10-15). A [catalogs] `doc` entry replaces it.
DOC_CATALOG = 'git -C "${SYMBION_DIR:?}/docs" ls-files --cached --others --exclude-standard'

# A pull that changes symbion.toml leaves this in the clone's git dir, never
# pushed, holding the HEAD before it; `symbion trust` removes it. A fresh
# clone is trusted: cloning a store is trusting it (the user, 2026-10-28).
# shortcut: a `git pull` run by hand in the store leaves none; a content hash
# would catch it, and then trip on each local edit.
UNTRUSTED_FILE = "symbion-untrusted"
UNTRUSTED = ("a pull changed symbion.toml, which names commands symbion runs: writes and "
             "those commands wait until the user reads the change and runs `symbion trust` "
             "in a terminal")
_UNTRUSTED_PATHS: dict = {}


def untrusted_path(store) -> Path | None:
    """The marker's path in `store`'s git dir; None outside a git repo.
    Asked once per store, as store._sessions_path is."""
    store = Path(store).absolute()
    if (p := _UNTRUSTED_PATHS.get(store)) is None:
        r = subprocess.run(["git", "-C", str(store), "rev-parse", "--git-path", UNTRUSTED_FILE],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0:
            p = _UNTRUSTED_PATHS[store] = Path(store, r.stdout.strip())
    return p


def untrusted(store) -> str | None:
    """The HEAD before the first pull that changed symbion.toml and is not
    yet trusted, or None."""
    p = untrusted_path(store)
    with contextlib.suppress(FileNotFoundError):
        return p.read_text(encoding="utf-8").strip() if p else None
    return None


def run_configured(cfg, cmd: str, input: str | None = None):
    """Run one configured shell command in the CURRENT worktree, bounded.

    Every configured command — catalogs, renames, resolvers, the provenance
    override — goes through here. It runs at `cfg.work_root`, not
    `cfg.project_root`: a catalog like `git ls-files '*.py'` must enumerate
    the checkout the caller is actually in (a feature branch's added files,
    e.g.), not the main worktree that project_root deliberately names for
    the store's location. stdin is closed so a command that prompts fails
    instead of hanging an unattended agent — unless `input` is given, which
    is the resolver protocol's stdin; the timeout bounds the rest.

    A configured command must not write to the store: writers run it while
    holding the store lock, so it would wait on that lock until the timeout
    kills it.

    SYMBION_DIR is set to the store in use, so a store-derived catalog
    (`symbion list --json | ...`) reads the SAME store as its parent. `--dir`
    is argv and cannot be inherited, so without this a command run under
    `--dir` resolved the default store instead and answered confidently from
    the wrong names."""
    if getattr(cfg, "store", None) is not None and untrusted(cfg.store):
        raise CatalogError(UNTRUSTED)
    if getattr(cfg, "work_root", None) is None:
        if getattr(cfg, "repos", None):
            raise CatalogError(
                f"{os.getcwd()} is in none of this store's repos ({', '.join(cfg.repos)}): "
                f"a configured command that is no repo's runs in the repo you stand in; "
                f"run from one of them: {cmd}")
        raise CatalogError(
            f"no git repository at {os.getcwd()}: a configured command runs in the "
            f"project's worktree, and this store was named from outside one: {cmd}")
    env = dict(os.environ)
    if getattr(cfg, "store", None) is not None:
        env["SYMBION_DIR"] = str(cfg.store)
    # git C-quotes a path with a byte over 0x7f while core.quotePath is on,
    # its default: `git ls-files` listed `"caf\303\251.md"`, and no typed
    # name matched it. Set here, it reaches the commands a store's toml
    # already holds. Git before 2.31 ignores it and quotes as before.
    k = int(env.get("GIT_CONFIG_COUNT") or 0)
    env.update({"GIT_CONFIG_COUNT": str(k + 1), f"GIT_CONFIG_KEY_{k}": "core.quotePath",
                f"GIT_CONFIG_VALUE_{k}": "false"})
    # Its own session, so a timeout kills what the shell forked too:
    # subprocess.run's timeout kills only the shell, and its children ran on
    # after the error said killed (found 2026-10-02). Out of the terminal's
    # process group, the command no longer sees a Ctrl-C, so any exit kills it.
    # ponytail: a child that calls setsid() itself escapes; a cgroup would not.
    # shell=True on Windows is cmd.exe, which ran `git ls-files '*.py'` with
    # the quotes kept and matched nothing. A store's symbion.toml is shared
    # across machines, so its commands mean the same sh on each.
    args, shell = cmd, True
    if os.name == "nt":
        sh = posix_sh()
        if sh is None:
            raise CatalogError(f"a configured command runs in sh, and no `sh` is on PATH "
                               f"or beside git; install Git for Windows: {cmd}")
        args, shell = [sh, "-c", cmd], False
    # Bytes both ways. Text mode on Windows wrote the resolver's lines as
    # CRLF, and a query read with `read q` kept the CR and matched nothing.
    # It also decodes in communicate()'s reader threads there, where a
    # UnicodeDecodeError dies with the thread and stdout reads as None.
    with subprocess.Popen(
            args, shell=shell, cwd=str(cfg.work_root), env=env,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as p:
        try:
            out, err = p.communicate(
                None if input is None else input.encode("utf-8"),
                timeout=getattr(cfg, "command_timeout", None) or DEFAULT_COMMAND_TIMEOUT)
        except BaseException as e:
            if os.name == "nt":                # no process groups: kill the tree
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            if isinstance(e, subprocess.TimeoutExpired):
                raise CatalogError(
                    f"command exceeded its timeout and was killed: {cmd}") from None
            raise
    # Per line: one Latin-1 file name, which git no longer quotes, refused
    # the whole catalog and every `file:` write with it.
    lines = out.splitlines(keepends=True)
    good = [s for s in (_utf8(ln) for ln in lines) if s is not None]
    if n := len(lines) - len(good):
        print(f"note: {n} {'line' if n == 1 else 'lines'} printed by {cmd} "
              f"{'is' if n == 1 else 'are'} not UTF-8, skipped", file=sys.stderr)
    # text=True's newlines: \r\n and a lone \r read as \n.
    out, err = ("".join(good), err.decode("utf-8", "replace"))
    out, err = (s.replace("\r\n", "\n").replace("\r", "\n") for s in (out, err))
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def _utf8(b: bytes) -> str | None:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lines(stdout: str) -> list[str]:
    """The protocol's line rule, shared by catalogs and resolvers: blank lines
    go, every other line is VERBATIM. Stripping would silently rename a legal
    path with a trailing space, and the task it seeds then never matches the
    catalog again. A catalog then splits each line at its first tab
    (`_split`)."""
    return [ln for ln in stdout.splitlines() if ln.strip()]


def _split(lines: list[str]) -> list[tuple[str, str]]:
    """Each catalog line as (name, description): the name is the part before
    the first tab, verbatim, and the description the rest, stripped. A line
    whose name is blank is skipped, as a blank line is."""
    rows = [(n, d.strip()) for n, _, d in (ln.partition("\t") for ln in lines)]
    return [(n, d) for n, d in rows if n.strip()]


def names(cfg, target_type: str, *, allow_empty: bool = False) -> list[str]:
    """The catalog's names: each line up to its first tab, verbatim.
    Non-zero exit is an error whatever stdout holds — checked BEFORE
    emptiness, so a failed producer never reads as an empty catalog.
    `allow_empty` is for a caller holding a query to resolve (see `pool`); a
    sweep must leave it off."""
    return [n for n, _ in _entries(cfg, target_type, allow_empty)]


def described(cfg, target_type: str) -> dict:
    """Each name the catalog lists with its description ('' for none), in
    the catalog's order; a repeated name keeps its first. Only the GUI's
    pickers read a description (the user, 2026-10-15): every other reader
    takes `names`. An empty catalog is an empty dict."""
    out: dict = {}
    for n, d in _entries(cfg, target_type, True):
        out.setdefault(n, d)
    return out


def _entries(cfg, target_type: str, allow_empty: bool) -> list[tuple[str, str]]:
    cmd = cfg.catalogs.get(target_type)
    if cmd is None:
        raise CatalogError(f"no catalog command for type {target_type!r}: add one under [catalogs] in symbion.toml")
    if why := cfg.missing(target_type):
        raise CatalogError(f"{why}; its entries still read, and a write on {target_type!r} "
                           f"waits for the repo")
    if cmd == DOC_CATALOG and cfg.store is not None and not Path(cfg.store, "docs").is_dir():
        out = []                         # git -C cannot enter a docs/ not yet made
    else:
        r = run_configured(cfg.for_type(target_type), cmd)
        if r.returncode != 0:
            raise CatalogError(f"catalog {target_type!r} exited {r.returncode}: {cmd}\n"
                               f"{r.stderr.strip()[:400]}")
        out = _split(_lines(r.stdout))
    if not out and not allow_empty:
        # Silent-empty is indistinguishable from a real result: seeding would
        # create 0 tasks and report success.
        raise CatalogError(f"catalog {target_type!r} produced no names: {cmd}")
    return out


def resolve(query: str, candidates) -> str:
    """Exact, else unique substring, else the one name equal but for case,
    else the input. Substring ambiguity refuses; case-only twins (README.md,
    readme.md) are a miss, which `match` names in its `did you mean`."""
    q = nfc(query)
    if q in candidates:
        return q
    hits = [c for c in candidates if q in c]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AmbiguousName(q, hits)
    # `Balancer` reached `Load Balancer` as a substring while the closer
    # `load balancer` missed (2026-10-03).
    same = [c for c in candidates if c.casefold() == q.casefold()]
    return same[0] if len(same) == 1 else q


def pool(cfg, target_type: str) -> list[str]:
    """Candidates for a caller that HOLDS A QUERY. Empty is legal only when a
    resolver is declared: it then sees zero candidates and decides what a
    first sighting stores. A sweep has no query and must call `names`.

    Without a resolver, an empty catalog here is a DEADLOCK rather than a bad
    producer, and saying "produced no names" sends the reader to debug a
    command that worked. A store-derived catalog (`symbion list … --type X`)
    is empty until its first X row exists, and that row cannot be written
    because the catalog is empty — no order of operations escapes it. Name the
    cause instead. Emptiness is judged only after `names` has ruled out a
    non-zero exit, so a failed producer keeps its own diagnosis."""
    out = names(cfg, target_type, allow_empty=True)
    if out or resolver(cfg, target_type):
        return out
    if cfg.resolvers.get(target_type) == CLOSED:
        raise CatalogError(f"catalog {target_type!r} produced no names, and it is closed, "
                           f"so it takes none: {cfg.catalogs.get(target_type)}")
    if target_type == "doc" and cfg.catalogs.get("doc") == DOC_CATALOG:
        raise CatalogError(f"the store has no file under docs/ yet: write "
                           f"{Path(cfg.store or '.', 'docs', 'NAME').as_posix()} first, "
                           f"then name it with doc:NAME")
    raise CatalogError(
        f"catalog {target_type!r} produced no names and no [resolvers] entry "
        f"is declared for it, so a first name cannot be minted: {cfg.catalogs.get(target_type)}\n"
        f"A store-derived catalog is empty until its first {target_type!r} entry exists. "
        f"Declare a resolver for {target_type!r} in symbion.toml to mint one.")


CLOSED = "closed"


def resolver(cfg, target_type: str) -> str | None:
    """The [resolvers] command for `target_type`, or None where the built-in
    rule picks: no entry, or `closed` (the user, 2026-10-14: an opt-in
    catalog that takes only the names it lists). Like `[renames] TYPE =
    "git"`, the keyword names a built-in, never a command."""
    cmd = cfg.resolvers.get(target_type)
    return None if cmd == CLOSED else cmd


def resolve_with(cfg, target_type: str, query: str, candidates) -> str:
    """The configured resolver, per the protocol: stdin is the NFC query then
    one candidate per line; exit 0 → first non-blank stdout line is the name
    (in or out of the list — a miss is the resolver's business); exit 2 →
    the non-blank lines are the matches, refused as AmbiguousName; anything
    else is an error. NEVER falls through to `resolve` or to the input: the
    failure this exists for was silent, and a broken resolver that minted
    duplicates would be the same failure with more moving parts."""
    cmd = cfg.resolvers[target_type]
    q = nfc(query)
    if "\n" in q or "\r" in q:
        raise CatalogError(f"resolver {target_type!r} cannot resolve a name containing a newline: {q!r}")
    r = run_configured(cfg.for_type(target_type), cmd, input="\n".join([q, *candidates]) + "\n")
    lines = _lines(r.stdout)
    if r.returncode == 2 and lines:
        raise AmbiguousName(q, lines)
    if r.returncode != 0:
        # Python exits 2 on a script path it cannot open: only stderr tells
        # that from a resolver that found no candidates (2026-10-06).
        why = " (ambiguous) but printed no candidates" if r.returncode == 2 else ""
        raise CatalogError(f"resolver {target_type!r} exited {r.returncode}{why}: {cmd}\n"
                           f"{r.stderr.strip()[:400]}")
    if not lines:
        raise CatalogError(f"resolver {target_type!r} printed no name for {q!r}: {cmd}")
    return lines[0]


def match(cfg, target_type: str, query: str, candidates, earlier=()) -> str:
    """The one picker: the declared resolver, else the built-in rule.
    `earlier`: candidates no catalog listed, which this write's earlier
    entries took, so a pick of one does not claim the catalog.

    A built-in miss is taken as typed, by design (a catalog disambiguates,
    it does not whitelist) -- but silently, so a typo'd or deleted path
    minted a second target that `list --name` never joined to the first
    (measured 2026-09-20). Say so on stderr; stdout and
    the exit code are unchanged, so nothing scripted moves. A resolver's
    miss is the resolver's business and gets no note.

    The note says "taken", not "stored": this is the one picker for reads and
    writes both, and it does not know the verb, so a `list --name` miss used
    to promise a write the read never made (adopter report, 2026-09-28). A
    batch write, which does know, still says "stored" (cli's from-json note).

    A closed catalog refuses the miss instead. A read of a name the store
    holds never gets here (gitref.canon_name takes it first)."""
    cfg = cfg.for_type(target_type)       # its notes ask the repo the name is in
    if resolver(cfg, target_type):
        return resolve_with(cfg, target_type, query, candidates)
    got = resolve(query, candidates)
    if got not in candidates:
        from . import gitref            # late: gitref imports this module
        b = gitref.only_on_default_branch(cfg, got)
        where = (f"; it is on {b}, not in this worktree" if b else
                 "; it is a gitignored file in this worktree" if gitref.ignored_here(cfg, got) else
                 f"; it is in {w}" if (w := gitref.other_worktree(cfg, got)) else "")
        guess = did_you_mean(got, candidates)
        if cfg.resolvers.get(target_type) == CLOSED:
            raise CatalogError(f"{got!r} matches nothing in the {target_type} catalog, which is "
                               f"closed: it takes only the names it lists{where}{guess} "
                               f"([resolvers] {target_type} = \"closed\" in symbion.toml)")
        print(f"note: {got!r} matches nothing in the {target_type} catalog; "
              f"taken as typed{where}{guess}", file=sys.stderr)
    elif got != nfc(query):
        # A non-exact pick is never silent:
        # `--name foo` landing on src/foo_test.py must be visible to undo.
        how = "same name but for case" if got.casefold() == nfc(query).casefold() else "unique substring"
        src = ", from an earlier entry of this write" if got in earlier else \
            f" in the {target_type} catalog"
        print(f"note: {query!r} resolved to {got!r} ({how}{src})", file=sys.stderr)
    return got


def did_you_mean(query: str, candidates) -> str:
    """`; did you mean 'a', 'b'?` for the candidates `query` is likely a
    typo of, else ''. 0.9: a typo scored 0.97 to 0.98 against its file in
    this repo, and a new file 0.84 against its siblings, which share its
    directory. Case is folded: 'load balancer' scored 0.85 against 'Load
    Balancer'. Names that differ only in digits are a series, not a typo:
    `PR #7` and `PR #8` scored 0.92."""
    q, folded = query.casefold(), {}
    for c in candidates:
        f = c.casefold()
        if f == q or re.sub(r"\d", "", f) != re.sub(r"\d", "", q):
            folded.setdefault(f, []).append(c)
    near = [c for f in difflib.get_close_matches(q, list(folded), n=3, cutoff=0.9)
            for c in folded[f]]
    return f"; did you mean {', '.join(map(repr, near))}?" if near else ""


def canonical(cfg, target_type: str, name, pending=(), candidates=None, earlier=None):
    """`candidates` lets a write that runs the catalog once pass the pool in;
    `pending` is the names that write has already resolved, unioned with the
    catalog/candidates (order kept, no duplicates — a duplicate would make a
    repeated query read as ambiguous). A caller that keeps that union itself
    passes it as `candidates`, and `earlier` the names in it no catalog
    listed."""
    if name is None or target_type in NON_CANONICAL_TYPES:
        return name
    if target_type == "doc":             # as git prints it, from the store's root
        name = name.removeprefix("docs/")
    if target_type not in cfg.catalogs:
        return nfc(name)
    base = pool(cfg, target_type) if candidates is None else candidates
    cands = dict.fromkeys([*base, *pending]) if pending else base
    if earlier is None:
        earlier = set(pending).difference(base) if pending else ()
    return match(cfg, target_type, name, cands, earlier=earlier)


def across(query, pools) -> list[tuple[str, str]]:
    """The built-in rule over several catalogs at once, each name kept with
    its catalog: the hits of the first step that has any (exact, else
    substring, else equal but for case). Every hit is returned, never
    raised, so a caller can name each in its refusal."""
    q = nfc(query)
    every = [(t, n) for t, ns in pools.items() for n in dict.fromkeys(ns)]
    for test in (lambda n: n == q, lambda n: q in n,
                 lambda n: n.casefold() == q.casefold()):
        if hits := [(t, n) for t, n in every if test(n)]:
            return hits
    return []


def listed(hits, cap: int = 8) -> str:
    """`a:x, b:y and c:z` for a refusal; past `cap`, `+N more`."""
    shown = [f"{t}:{n}" for t, n in hits[:cap]]
    if len(hits) > cap:
        return f"{', '.join(shown)}, +{len(hits) - cap} more"
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"


# ---- rename providers ----
def _git_rename_pairs(cfg):
    """[(old, new)] from git history, NUL-delimited so paths with spaces work.

    Records arrive as: 'R<score>\\0<old>\\0<new>\\0' repeated. `--format=` is
    load-bearing: without it commit headers interleave with the status
    records. `-M` is stated explicitly rather than relying on diff.renames
    defaulting on. `-z` is what makes paths with spaces parseable."""
    r = run_configured(cfg, "git log -z -M --diff-filter=R --name-status --format=")
    if r.returncode != 0:
        return []
    fields = [f for f in r.stdout.split("\0") if f]
    pairs, i = [], 0
    while i + 2 < len(fields) + 1:
        if not fields[i].startswith("R"):
            i += 1
            continue
        if i + 2 >= len(fields):
            break
        pairs.append((fields[i + 1], fields[i + 2]))
        i += 3
    return pairs


def _resolve_chains(pairs) -> dict:
    """old -> final new, following A->B->C to a fixed point.

    Ambiguous sources (A->B and A->C on different lines of development) are
    DROPPED, not guessed: the task then classifies as stale, and since
    --apply no longer closes stale items the cost is a line of noise.

    The walk tracks `seen` per starting node, so a cycle (or a chain that
    feeds into one) terminates instead of looping forever: once the pointer
    revisits a node already seen on this walk, the walk stops there."""
    direct = {}
    ambiguous = set()
    for old, new in pairs:
        if old in direct and direct[old] != new:
            ambiguous.add(old)
        direct[old] = new
    for a in ambiguous:
        direct.pop(a, None)
    out = {}
    for old in direct:
        seen, cur = {old}, direct[old]
        while cur in direct and cur not in seen:
            seen.add(cur)
            cur = direct[cur]
        out[old] = cur
    return out


def rename_map(cfg, target_type: str) -> dict:
    """old-name -> current-name for one target type, per `cfg.renames`.

    `renames[target_type] == "git"` uses git rename-detection history;
    any other value is a shell command producing tab-separated
    'old<TAB>new' lines, run the same bounded way as every other
    configured command. Unconfigured or a failing command yields {} —
    every target then falls through to `stale` in reconcile, never a guess."""
    spec = cfg.renames.get(target_type)
    if not spec:
        return {}
    cfg = cfg.for_type(target_type)
    if spec == "git":
        return _resolve_chains(_git_rename_pairs(cfg))
    r = run_configured(cfg, spec)
    if r.returncode != 0:
        return {}
    pairs = [tuple(ln.split("\t", 1)) for ln in r.stdout.splitlines()
             if "\t" in ln]
    return _resolve_chains(pairs)
