"""Catalogs are commands. A catalog type is authoritative because you re-run
it. A resolver is a command too: it replaces the match rule for one type."""
from __future__ import annotations

import os
import subprocess
import sys
import unicodedata

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
    if getattr(cfg, "work_root", None) is None:
        raise CatalogError(
            f"no git repository at {os.getcwd()}: a configured command runs in the "
            f"project's worktree, and this store was named from outside one: {cmd}")
    io = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    env = dict(os.environ)
    if getattr(cfg, "store", None) is not None:
        env["SYMBION_DIR"] = str(cfg.store)
    try:
        return subprocess.run(
            cmd, shell=True, cwd=str(cfg.work_root), env=env,
            capture_output=True, text=True, **io,
            timeout=getattr(cfg, "command_timeout", None) or DEFAULT_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise CatalogError(
            f"command exceeded its timeout and was killed: {cmd}") from None


def _lines(stdout: str) -> list[str]:
    """The protocol's line rule, shared by catalogs and resolvers: blank lines
    go, every other line is VERBATIM. Stripping would silently rename a legal
    path with a trailing space, and the task it seeds then never matches the
    catalog again."""
    return [ln for ln in stdout.splitlines() if ln.strip()]


def names(cfg, target_type: str, *, allow_empty: bool = False) -> list[str]:
    """The catalog's names, verbatim. Non-zero exit is an error whatever stdout
    holds — checked BEFORE emptiness, so a failed producer never reads as an
    empty catalog. `allow_empty` is for a caller holding a query to resolve
    (see `pool`); a sweep must leave it off."""
    cmd = cfg.catalogs.get(target_type)
    if cmd is None:
        raise CatalogError(f"no catalog command for type {target_type!r}: add one under [catalogs] in symbion.toml")
    r = run_configured(cfg, cmd)
    if r.returncode != 0:
        raise CatalogError(f"catalog {target_type!r} exited {r.returncode}: {cmd}\n"
                           f"{r.stderr.strip()[:400]}")
    out = _lines(r.stdout)
    if not out and not allow_empty:
        # Silent-empty is indistinguishable from a real result: seeding would
        # create 0 tasks and report success.
        raise CatalogError(f"catalog {target_type!r} produced no names: {cmd}")
    return out


def resolve(query: str, candidates) -> str:
    """Exact, else unique substring, else the input. Ambiguity refuses."""
    q = nfc(query)
    if q in candidates:
        return q
    hits = [c for c in candidates if q in c]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AmbiguousName(q, hits)
    return q


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
    if out or target_type in cfg.resolvers:
        return out
    raise CatalogError(
        f"catalog {target_type!r} produced no names and no [resolvers] entry "
        f"is declared for it, so a first name cannot be minted: {cfg.catalogs.get(target_type)}\n"
        f"A store-derived catalog is empty until its first {target_type!r} row exists. "
        f"Declare a resolver for {target_type!r} in symbion.toml to mint one.")


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
    r = run_configured(cfg, cmd, input="\n".join([q, *candidates]) + "\n")
    lines = _lines(r.stdout)
    if r.returncode == 2:
        if not lines:
            raise CatalogError(f"resolver {target_type!r} exited 2 (ambiguous) but printed "
                               f"no candidates: {cmd}")
        raise AmbiguousName(q, lines)
    if r.returncode != 0:
        raise CatalogError(f"resolver {target_type!r} exited {r.returncode}: {cmd}\n"
                           f"{r.stderr.strip()[:400]}")
    if not lines:
        raise CatalogError(f"resolver {target_type!r} printed no name for {q!r}: {cmd}")
    return lines[0]


def match(cfg, target_type: str, query: str, candidates) -> str:
    """The one picker: the declared resolver, else the built-in rule.

    A built-in miss is taken as typed, by design (a catalog disambiguates,
    it does not whitelist) -- but silently, so a typo'd or deleted path
    minted a second target that `list --name` never joined to the first
    (measured 2026-09-20). Say so on stderr; stdout and
    the exit code are unchanged, so nothing scripted moves. A resolver's
    miss is the resolver's business and gets no note.

    The note says "taken", not "stored": this is the one picker for reads and
    writes both, and it does not know the verb, so a `list --name` miss used
    to promise a write the read never made (adopter report, 2026-09-28). A
    batch write, which does know, still says "stored" (cli's from-json note)."""
    if target_type in cfg.resolvers:
        return resolve_with(cfg, target_type, query, candidates)
    got = resolve(query, candidates)
    if got not in candidates:
        from . import gitref            # late: gitref imports this module
        b = gitref.only_on_default_branch(cfg, got)
        where = f"; it is on {b}, not in this worktree" if b else ""
        print(f"note: {got!r} matches nothing in the {target_type} catalog; "
              f"taken as typed{where}", file=sys.stderr)
    elif got != nfc(query):
        # A non-exact pick is never silent:
        # `--name foo` landing on src/foo_test.py must be visible to undo.
        print(f"note: {query!r} resolved to {got!r} (unique substring in the {target_type} catalog)",
              file=sys.stderr)
    return got


def canonical(cfg, target_type: str, name, pending=(), candidates=None):
    """`candidates` lets a write that runs the catalog once pass the pool in;
    `pending` is the names that write has already resolved, unioned with the
    catalog/candidates (order kept, no duplicates — a duplicate would make a
    repeated query read as ambiguous)."""
    if name is None or target_type in NON_CANONICAL_TYPES:
        return name
    if target_type not in cfg.catalogs:
        return nfc(name)
    base = pool(cfg, target_type) if candidates is None else candidates
    cands = list(dict.fromkeys([*base, *pending]))
    return match(cfg, target_type, name, cands)


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
    if spec == "git":
        return _resolve_chains(_git_rename_pairs(cfg))
    r = run_configured(cfg, spec)
    if r.returncode != 0:
        return {}
    pairs = [tuple(ln.split("\t", 1)) for ln in r.stdout.splitlines()
             if "\t" in ln]
    return _resolve_chains(pairs)
