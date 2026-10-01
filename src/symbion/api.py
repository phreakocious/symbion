"""The shared write path. `cli` and `gui` both enter here; neither imports the
other, and this module imports neither of them.

`store.add_many` validates kind, status and target type. It does NOT
canonicalize names or refs, stamp provenance, or resolve an author -- those
four lived in `cli.py`, which made the CLI the only client that applied them.
A second write client that imports `store` directly drops all four silently,
so they live here instead, and `gui/` is forbidden from importing store's
writers at all (tests/test_gui_seam.py).
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import catalog, config, gitref, kinds as K, store


@dataclass(frozen=True)
class Ctx:
    """Everything a write needs that isn't the row itself. One per process for
    the CLI; one per request for the GUI, so an edited symbion.toml takes
    effect without a restart."""
    store_dir: Path
    cfg: config.Config
    target_types: frozenset
    arc_scopes: frozenset
    seed_scopes: frozenset
    kinds: dict
    # The store came from SYMBION_DIR rather than `--dir` or the tree. Both
    # are per-invocation, but only the env one is set by a wrapper the caller
    # may not be thinking about, and `init` must not make it durable: see
    # cli._record_pointer.
    store_from_env: bool = False
    # The store was named and belongs to a project other than the cwd's
    # (config.store_owner), so both roots are that project's. The CLI says
    # so on stderr: check state computed elsewhere must not read as local.
    followed_owner: bool = False
    # The store the CWD's own tree names (config.tree_store), or None outside
    # a repo. `store_dir != tree_default` means the store was named rather
    # than stood in: two summaries read back to back in one session were
    # otherwise identical, and neither said which project it was about
    # (an adopter driving a satellite repo from a hub, 2026-09-28).
    tree_default: Path | None = None


def resolve(dir_value=None, follow_owner=True) -> Ctx:
    """Lifted from cli.main() with no change in behavior. `dir_value` is an
    already-extracted `--dir` value: parsing argv stays in the CLI.

    Propagates `subprocess.CalledProcessError` when the process cwd is not
    inside a git repository AND nothing names the store -- the store is
    derived from the project, so there is nothing to open. cli.main() has a
    clause for it; a GUI caller needs one too.

    A store NAMED by `--dir` or SYMBION_DIR needs no project: with none in
    the cwd and none claiming the store, both roots are None, reads degrade (gitref answers as for a departed sha), a configured
    command and a provenance stamp refuse by name. Measured 2026-09-20 from
    outside any repository: every `--dir` listing exited 1.

    A NAMED store that its own project claims (config.store_owner) resolves
    in that project when the cwd's tree does not name it too: the store is
    what `--dir` chose, the cwd is incidental. Measured 2026-09-23 from
    another project: symbion's `file` catalog ran there, and a name present in
    both trees would have resolved to the wrong file, silently.
    `follow_owner=False` is for `init`, which installs into the cwd's project
    and points it at the store.

    A cwd inside a store names that store, and resolves in its owner as a
    named one does. Measured 2026-09-23 inside a store: the store repo was
    taken for a project, so every bare command read `<name>-notes-notes`.
    With no owner the store repo is still not a project, so both roots are
    None rather than a catalog listing the store's own files."""
    from_env = dir_value is None and bool(os.environ.get("SYMBION_DIR"))
    try:
        root = config.project_root()
        wroot = config.work_root()
    except subprocess.CalledProcessError:
        if dir_value is None and not from_env:
            raise
        root = wroot = None
    in_store = (dir_value is None and not from_env and store.is_store(root))
    store_dir = (Path(dir_value).resolve() if dir_value is not None
                 else root if in_store else config.store_dir(root))
    # What the CWD's own tree names, before follow_owner moves `root` -- so a
    # caller can say "this is not the store you get by standing here". The
    # tree, not config.store_dir: SYMBION_DIR names a store too, and a reader
    # who cannot see the environment is exactly who the label is for.
    tree_default = None if root is None else config.tree_store(root)
    followed = False
    if (follow_owner and (dir_value is not None or from_env or in_store)
            and (root is None or config.tree_store(root) != store_dir)):
        owner = config.store_owner(store_dir)
        if owner is not None:
            root = wroot = owner
            followed = True
        elif in_store:
            root = wroot = None
    try:
        cfg = config.load(store_dir, project_root=root, work_root=wroot)
    except tomllib.TOMLDecodeError as e:
        # cli.main()'s except clause reports this against store_dir, but
        # store_dir lives in THIS frame, not the caller's -- attach it so a
        # raise out of here doesn't leave the caller's own name unbound.
        e.store_dir = store_dir
        raise
    return Ctx(
        store_dir=store_dir,
        cfg=cfg,
        target_types=frozenset(store.BUILTIN_TARGET_TYPES | set(cfg.catalogs)),
        arc_scopes=frozenset(set(cfg.catalogs) | {"item", "project", "mixed"}),
        seed_scopes=frozenset(set(cfg.catalogs) | {"item"}),
        kinds=K.read_kinds(store_dir),
        store_from_env=from_env,
        followed_owner=followed,
        tree_default=tree_default,
    )


# ---- author resolution ----
def _git_user_name() -> str:
    """Isolated so both author rules (and their tests) can vary the identity
    without spawning git."""
    # Keep this `subprocess.run` reached through the module: tests/test_cli.py
    # patches `cli.subprocess.run`, which is the same module object
    # only while this file says `import subprocess`. A `from subprocess import
    # run` edit here silently unhooks that patch point.
    r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    return r.stdout.strip()


def author_default() -> str:
    """The CLI rule: SYMBION_AUTHOR > "claude" if CLAUDECODE > git user.name >
    "user". CLAUDECODE=1 is set in the agent's shell, which makes CLI
    attribution self-configuring."""
    if os.environ.get("SYMBION_AUTHOR"):
        return os.environ["SYMBION_AUTHOR"]
    if os.environ.get("CLAUDECODE"):
        return "claude"
    return _git_user_name() or "user"


def gui_author() -> str:
    """The GUI rule: SYMBION_AUTHOR > git user.name > "user", CLAUDECODE
    deliberately NOT consulted.

    The explorer is the human's interface, so the identity is a property of
    the surface, not of the environment. `symbion serve` is launched from the
    agent's shell and inherits CLAUDECODE=1; without this, every human click
    is stored as author="claude" in an append-only file."""
    if os.environ.get("SYMBION_AUTHOR"):
        return os.environ["SYMBION_AUTHOR"]
    return _git_user_name() or "user"


# ---- name canonicalization at the I/O boundary ----
def canon(cfg, target_type, name):
    """The READ-side wrapper (`list --name`, `context --target`): the
    stored/queried form of a target name, so an abbreviation typed on read
    matches what write stored. The write side is `_canonicalizer`, which
    also runs the catalog/resolver under the lock; this one never does."""
    return gitref.canon_name(cfg, target_type, name)


def check_name(typ: str, name) -> None:
    """A project target has no name and every other target has one. Every
    writer of a target or ref name passes here: add, --from-json, --ref,
    seed and rename. `project:foo` stored silently, and `context --target
    project:` never showed it (measured 2026-09-26: six rows in one adopter
    store, written for named subjects)."""
    if typ == "project":
        if name is not None:
            raise ValueError(f"a project target takes no name (got {name!r}); "
                             f"a named subject is item:{name}")
    elif not isinstance(name, str) or not name.strip():
        raise ValueError(f"target type {typ!r} needs a name: {typ}:NAME")


def kind_as_type(typ, kinds, what: str) -> str:
    """The tail of a refusal for a kind typed as a ref or target type:
    `--ref check:<id>` meant a row (reported 2026-09-30). Rows join
    through the object they share, and an id in the body is the pointer."""
    if typ not in kinds:
        return ""
    return (f"; {typ!r} is a kind: a {what} names an object, never a row. Give this "
            f"row that row's target, and cite its id in the body")


def check_refs(target_types, refs, kinds=()) -> list[dict]:
    """Shape and type of row-shaped refs, name NFC'd; NO catalog runs, so a
    caller outside the lock can report a bad ref before locking. Resolution
    is `canonicalize_rows`', under the lock."""
    out = []
    for r in refs or ():
        if not isinstance(r, dict) or not isinstance(r.get("type"), str):
            raise ValueError("refs must be [{type, name}]")
        if r["type"] not in target_types:
            raise ValueError(f"unknown ref type {r['type']!r} (choose from "
                             f"{', '.join(sorted(target_types))})"
                             + kind_as_type(r["type"], kinds, "ref"))
        check_name(r["type"], r.get("name"))
        out.append({"type": r["type"], "name": catalog.nfc(r.get("name"))})
    return out


# ---- the write-time name rule, run by the store under its lock ----
def _canonicalizer(ctx: Ctx):
    """One write's name rule: `gitref.canon_name` (commit peeled, built-ins
    NFC) plus, for a catalog type, the catalog ONCE per write and a `pending`
    list of the names this write has already resolved for that type, unioned
    with the catalog so a repeated query never sees a duplicate candidate."""
    pools: dict[str, list[str]] = {}
    pending: dict[str, list[str]] = {}

    def one(t):
        if t is None:
            return None
        typ, name = t["type"], t.get("name")
        if name is None or typ in catalog.NON_CANONICAL_TYPES or typ not in ctx.cfg.catalogs:
            return {"type": typ, "name": gitref.canon_name(ctx.cfg, typ, name)}
        if typ not in pools:
            pools[typ] = catalog.pool(ctx.cfg, typ)
        got = catalog.canonical(ctx.cfg, typ, name,
                                pending=pending.get(typ, ()), candidates=pools[typ])
        pending.setdefault(typ, []).append(got)
        return {"type": typ, "name": got}
    return one


def canonicalize_rows(ctx: Ctx, rows) -> list[dict]:
    """`store.add_many` / `store.supersede` callback: target then refs, row
    by row, so a later row's query sees an earlier row's answer.

    A resolution error names its row (1-based): a `--from-json` batch of 45
    rows says WHICH one was ambiguous rather than leaving the caller to bisect."""
    one = _canonicalizer(ctx)
    out = []
    for i, r in enumerate(rows, 1):
        r = dict(r)
        try:
            if "target" in r:
                r["target"] = one(r["target"])
            if r.get("refs"):
                r["refs"] = [one(x) for x in r["refs"]]
        except (catalog.AmbiguousName, catalog.CatalogError) as e:
            e.args = (f"row {i}: {e}",) + e.args[1:]
            raise
        out.append(r)
    return out


def rename_target(ctx: Ctx, target_type: str, old: str, new: str, *,
                  author: str, to_type: str | None = None) -> tuple[int, int, str]:
    """`store.rename_target` with `new` resolved the way `add` resolves a
    name (the store call takes it as typed). Returns (re-targeted,
    re-pointed, the name stored), so the caller can print what `new`
    became. `to_type` moves the object to another type: a 0.1.0 store holds
    `project:NAME` rows, which `check_name` now refuses, and they move to
    `item:NAME`."""
    to_type = to_type or target_type
    check_name(to_type, new)
    stored = [new]

    def canon(n):
        stored[0] = canonicalize_names(ctx, to_type, [n])[0]
        return stored[0]
    moved, refs = store.rename_target(ctx.store_dir, target_type, old, new,
                                      author=author, canonicalize=canon,
                                      new_type=to_type)
    return moved, refs, stored[0]


def canonicalize_names(ctx: Ctx, target_type: str, names) -> list[str]:
    """`store.seed_arc` callback for EXPLICIT names; a sweep never calls it."""
    one = _canonicalizer(ctx)
    return [one({"type": target_type, "name": n})["name"] for n in names]


# ---- hashtags in a body ----
# `#` NOT preceded by a word char, `/`, `&` or `#`, then a LETTER. Requiring a
# letter is what keeps a markdown heading a heading: `# Title` has a space,
# `#0a0a0a` a digit. Neither can match.
_HASHTAG = re.compile(r"(?<![\w/&#])#([A-Za-z][\w-]*)")
_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)
_HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")


def harvest_hashtags(body: str):
    """`("#gui do the thing", ...)` -> `(["gui"], "do the thing")`.

    Tags are a COLUMN, not prose: leaving `#gui` in the body would store the
    same fact twice and let the two disagree, and a line-leading `#gui` is
    eaten by markdown into an <h1> anyway -- the text renders without it
    either way, so the stripped body is what was already being displayed.

    Code spans and fences are masked before scanning, so a `#include` or a
    `#!/bin/sh` inside backticks stays code.

    ponytail: a 6-letter hex like #eee8d5 is skipped as a colour, but
    #deadbeef and other long hexes still read as tags. The GUI shows the
    harvest in an editable field, so a wrong one is visible and removable
    rather than silent; tighten only if that proves annoying in practice.
    """
    return _harvest(body, _HASHTAG, lambda tag: not _HEX6.match(tag))   # a hex is a colour


# `!task` names the row's kind as `#gui` names a tag, with the same guards.
# Only a declared kind is taken, so `!important` in prose stays prose.
_BANG = re.compile(r"(?<![\w/&!])!([A-Za-z][\w-]*)")


def harvest_kind(body: str, kinds):
    """`("!task fix it", kinds)` -> `("task", "fix it")`: the first kind of
    `kinds` the body names, or None, and the body without every one."""
    found, out = _harvest(body, _BANG, lambda k: k in kinds)
    return (found[0] if found else None), out


def _harvest(body, rx, take):
    """Each distinct name `rx` finds outside code and `take` accepts, and the
    body without them."""
    masked = _CODE.sub(lambda m: " " * len(m.group(0)), body or "")
    spans, tags = [], []
    for m in rx.finditer(masked):
        tag = m.group(1)
        if not take(tag):
            continue
        spans.append(m.span())
        if tag not in tags:
            tags.append(tag)
    out = body or ""
    for start, end in reversed(spans):     # right-to-left keeps earlier spans valid
        out = out[:start] + out[end:]
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return tags, out.strip()


def _stamp(ctx: Ctx, kind: str, spec, external: bool = False) -> dict | None:
    """Provenance for a verdict kind -- or a refusal when there is no tree to
    stamp it from (the store was named from outside any git repository, see
    `resolve`). An unstamped check would read `unverifiable` forever and say
    nothing about why; a write that cannot keep its promise refuses by name.

    An external check (DNS, a host's log, a live database) read nothing in
    the tree, so HEAD and a dirty tree say nothing about it: it is stamped
    with when it ran, which `supersede` inherits like a sha, and needs no
    repo. Measured 2026-09-28: in a store whose checks mostly read the world,
    most verdict rows were this shape, and every one read `behind N` or `unverifiable`."""
    if spec.verdict and external:
        return {"external": True, "at": store._now_iso()}
    if spec.verdict and ctx.cfg.work_root is None:
        raise ValueError(f"kind {kind!r} is stamped with provenance at write, and "
                         f"{os.getcwd()} is in no git repository to stamp it from; "
                         f"run from inside the project")
    return gitref.provenance_stamp(ctx.cfg, spec)


_HINT_CAP = 8


def no_arc(store_dir, arc_id: str) -> str:
    """A bare miss stopped at the miss (2026-09-22). A near miss gets its
    candidates, otherwise the legal values, and always the verb that lists
    them. One helper, so every site agrees."""
    ids = [a.id for a in store.load_arcs(store_dir)]
    near = difflib.get_close_matches(arc_id, ids, n=3, cutoff=0.65)
    if near:
        what = f"did you mean {', '.join(near)}?"
    elif ids:
        shown = ", ".join(ids[:_HINT_CAP])
        more = f", +{len(ids) - _HINT_CAP} more" if len(ids) > _HINT_CAP else ""
        what = f"arcs: {shown}{more}"
    else:
        return f"no arc {arc_id!r}; no arcs yet (arc create)"
    return f"no arc {arc_id!r}; {what} (arc list)"


def check_arc(ctx: Ctx, arc_id):
    """An `arc_id` naming no arc was stored at exit 0, on a row no arc's
    progress line could reach (2026-09-26). An archived arc passes: a closing
    note may be filed there, and summary lists its open rows outside arcs."""
    if arc_id and arc_id not in {a.id for a in store.load_arcs(ctx.store_dir)}:
        raise ValueError(no_arc(ctx.store_dir, arc_id))
    return arc_id


def check_arc_targets(ctx: Ctx, targets) -> None:
    """An `arc:` target or ref names an arc, as `--arc-id` must: `--target
    arc:nosuch` stored a row on nothing (2026-09-27)."""
    for t in targets:
        if t and t.get("type") == "arc":
            check_arc(ctx, t.get("name"))


def fields_from_row(ctx: Ctx, row: dict, author: str) -> dict:
    """A `list --json`-shaped row -> `store.add_many` fields: shape checked,
    kind/type/status validated, names NFC'd (resolution happens under the
    lock, in `canonicalize_rows`), provenance stamped."""
    missing = sorted({"kind", "target"} - set(row))
    if missing:
        raise ValueError(f"missing {missing}")
    spec = ctx.kinds.get(row["kind"])
    if spec is None:
        raise ValueError(f"unknown kind {row['kind']!r} (choose from {', '.join(ctx.kinds)}); "
                         f"kinds are declared in {ctx.store_dir / config.CONFIG_FILE} under [kinds]")
    t = row["target"]
    if not isinstance(t, dict) or not isinstance(t.get("type"), str):
        raise ValueError("target must be {type, name}")
    if t["type"] not in ctx.target_types:
        raise ValueError(f"unknown target type {t['type']!r} (choose from "
                         f"{', '.join(sorted(ctx.target_types))})"
                         + kind_as_type(t["type"], ctx.kinds, "target"))
    check_name(t["type"], t.get("name"))
    store.check_fields(row["kind"], spec, row)
    external = row.get("external", False)
    if not isinstance(external, bool):
        raise ValueError(f"external is true or false, not {external!r}")
    if external and not spec.verdict:
        raise ValueError(f"--external is not valid for kind {row['kind']!r}: it has no "
                         f"verdict bit, so it carries no provenance to stamp "
                         f"(`symbion schema` lists the verdict kinds)")
    check_arc_targets(ctx, [t, *(row.get("refs") or ())])
    return dict(
        kind=row["kind"],
        target={"type": t["type"], "name": catalog.nfc(t.get("name"))},
        body=row.get("body") or "", status=row.get("status"),
        checked=row.get("checked"), result=row.get("result"),
        arc_id=check_arc(ctx, row.get("arc_id")), due=row.get("due"),
        tags=list(row.get("tags") or ()),
        author=row.get("author") or author,
        refs=check_refs(ctx.target_types, row.get("refs"), ctx.kinds),
        provenance=_stamp(ctx, row["kind"], spec, external),
    )


_ITEM_NEEDS_NAMES = ("item scope requires at least one name: it has no catalog. "
                     "Other scopes sweep a command configured under [catalogs] in symbion.toml")


def seed_names(ctx: Ctx, scope: str, names) -> list[str]:
    """The UNLOCKED preview behind `arc seed --dry-run`: the names a real seed
    would resolve. A sweep is the catalog verbatim; explicit names resolve as
    `seed` will, minus the lock (nothing is written, so no race to close).

    Raises ValueError where cli._seed_names raised SystemExit: an exit is a
    CLI concept, and a GUI caller needs something it can put in a dialog."""
    for n in names or ():
        check_name(scope, n)
    if scope == "item":
        if not names:
            raise ValueError(_ITEM_NEEDS_NAMES)
        return [catalog.nfc(n) for n in names]
    if not names:
        return catalog.names(ctx.cfg, scope)
    return canonicalize_names(ctx, scope, [catalog.nfc(n) for n in names])


# ---- provenance reporting ----
def verdict_state(cfg, n) -> tuple[str, int | None]:
    """A verdict row's (state, distance). An open status+verdict row with
    no result is `pending`: its stamp is where it was registered, before the
    run, and `current` against that stamp read as run (2026-09-29). The
    result, not the open status, is the evidence of a run: two adopter
    stores keep finished verifications open under a two-bit kind, and those
    ran at their stamp. `resolve` restamps a pre-registration, and from then
    on it reads as any check does."""
    if n.spec.status and store.read_status(n) == "open" and not store.has_result(n.result):
        return ("pending", None)
    return gitref.check_state(cfg, n.provenance)


def why_unverifiable(prov) -> str:
    """Which of the three causes `gitref.check_state` collapsed into
    ("unverifiable", None). Same precedence as check_state itself. Without
    this the GUI badge cannot say WHY, and a dirty stamp -- the check's own
    claim made against a tree nobody can reconstruct -- reads identically to
    a squashed sha."""
    if not prov or not prov.get("sha"):
        return "no provenance"
    if prov.get("dirty"):
        return "dirty tree"
    return "commit unavailable"


def _chain_tip(notes, note_id: str) -> store.Note:
    """The live head of note_id's supersede chain. Pure -- no I/O, no write.

    Shared by retag (which then merges tags and writes) and cli's combined
    --add-tag path (which merges into fields and lets store.supersede write),
    so the two cannot drift apart on the guard again. Raises KeyError(note_id)
    when nothing matches, and ValueError on a repeat -- same message shape as
    store._supersede_unlocked's own fast-forward, which this mirrors: without
    the `seen` guard a hand-edited or racing-add()-produced cycle spins the
    walk forever instead of erroring."""
    by_supersedes = {n.supersedes: n for n in notes if n.supersedes}
    cur = next((n for n in notes if n.id == note_id), None)
    if cur is None:
        raise KeyError(note_id)
    seen = {cur.id}
    while cur.id in by_supersedes:
        nxt = by_supersedes[cur.id]
        if nxt.id in seen:
            raise ValueError(f"cycle in supersede chain at {nxt.id}")
        seen.add(nxt.id)
        cur = nxt
    return cur


def retag(ctx: Ctx, note_id: str, *, add=(), rm=(), author) -> store.Note:
    """Add and remove tags on a note, as one superseding row.

    `--add-tag` / `--rm-tag` were five inline lines in cli._dispatch with no
    function to call, so the GUI star button had nothing to reuse. They also
    read the NAMED row's tags while store.supersede fast-forwards to the chain
    tip, silently dropping any tag added between the two. This walks to the tip
    first, so the merge and the write agree about which row they are editing.

    A bare string is one tag, not its letters: `set("priority")` is six
    characters, and the GUI star button is exactly the caller that passes
    one."""
    add = [add] if isinstance(add, str) else add
    rm = [rm] if isinstance(rm, str) else rm
    notes = store.load(ctx.store_dir)
    cur = _chain_tip(notes, note_id)
    tags = (set(cur.tags) | set(add)) - set(rm)
    return store.supersede(ctx.store_dir, cur.id, author=author, tags=sorted(tags))


# ---- writes ----
# `author` is keyword-only with NO default on every one of these. A write site
# that forgets it raises TypeError at the call rather than writing a plausible
# wrong row into an append-only file -- the bug (author="claude" on human
# work) that the research notebook symbion was extracted from stopped by
# convention, made structural here.

def add(ctx: Ctx, row: dict, *, author: str) -> store.Note:
    return add_many(ctx, [row], author=author)[0]


def add_fields(ctx: Ctx, fields) -> list:
    """Store-shaped rows -> notes, names resolved under the lock.

    A caller here has already bypassed `fields_from_row`'s own author
    stamping, so the per-row guard below is what stands between a forgotten
    author and a plausible-looking row on disk -- `store.add_many` defaults a
    missing one to "unknown" rather than raising."""
    for i, f in enumerate(fields):
        if not f.get("author"):
            raise ValueError(f"row {i}: author is required")
    return store.add_many(ctx.store_dir, fields, target_types=ctx.target_types,
                          canonicalize=lambda rows: canonicalize_rows(ctx, rows))


def add_many(ctx: Ctx, rows, *, author: str) -> list:
    """Every row is built and validated before any is appended, so a bad row
    leaves nothing behind to supersede."""
    return add_fields(ctx, [fields_from_row(ctx, r, author) for r in rows])


def supersede(ctx: Ctx, note_id: str, *, author: str, append_body: str | None = None,
              add_refs=None, **fields) -> store.Note:
    """Target and provenance are INHERITED from the superseded row and never
    re-resolved, so this needs no canonicalization -- a departed target stays
    editable. `refs`, if given, is new input: checked here, resolved under
    the lock by `canonicalize_rows`, which sees only `fields` -- never the
    inherited target or refs.

    Inheritance is ENFORCED, not merely documented. store.supersede's own
    `note_from_dict(base)` gets no `target_types=`, so a `target` forwarded
    through **fields lands on disk uncanonicalized AND unvalidated -- measured:
    a short sha stayed short and type "bogus" was accepted, both of which
    add_many rejects. Nothing passes either, and this keeps it that way.

    ONE exception, and it is this function's, not the caller's: resolving an
    OPEN status+verdict row (a pre-registration) re-stamps provenance, so the
    original row keeps the commitment sha and the resolving row carries the
    sha the verdict was made at. Any other supersede -- a body edit on the
    same row, a corrected verdict on a check -- inherits, because a
    correction is about the same run. The result requirement itself is the
    store's (check_fields); this only adds the stamp.

    The tip is read outside the store lock, so two concurrent resolves of the
    same open pre-registration can both read it open and both re-stamp, the
    second landing as a stamped correction to an already-resolved row -- the
    same race shape as `retag`, and equally no data loss."""
    for key in ("target", "provenance"):
        if key in fields:
            raise ValueError(
                f"supersede cannot set {key!r}: it is inherited from the "
                f"superseded row and never re-resolved (add a new note instead)")
    if "refs" in fields:
        fields["refs"] = check_refs(ctx.target_types, fields["refs"], ctx.kinds)
    if add_refs:
        add_refs = check_refs(ctx.target_types, add_refs, ctx.kinds)
    check_arc_targets(ctx, [*(fields.get("refs") or ()), *(add_refs or ())])
    check_arc(ctx, fields.get("arc_id"))
    if fields.get("status") == "resolved":
        tip = _chain_tip(store.load(ctx.store_dir), note_id)
        if tip.spec.status and tip.spec.verdict and store.read_status(tip) == "open":
            fields["provenance"] = _stamp(ctx, tip.kind, tip.spec,
                                         bool((tip.provenance or {}).get("external")))
    return store.supersede(ctx.store_dir, note_id, author=author, append_body=append_body,
                           add_refs=add_refs,
                           canonicalize=lambda f: canonicalize_rows(ctx, [f])[0], **fields)


def seed(ctx: Ctx, arc_id: str, target_type: str, names=None, *, author: str,
         kind: str = "task") -> list:
    """`names` empty → a SWEEP: the catalog verbatim (strict — empty is an
    error), resolver run zero times. Given → EXPLICIT: NFC'd here, resolved
    under the lock against the catalog plus the ones before them. `item`
    has no catalog and is never resolved."""
    for n in names or ():
        check_name(target_type, n)
    if target_type == "item":
        if not names:
            raise ValueError(_ITEM_NEEDS_NAMES)
        return store.seed_arc(ctx.store_dir, arc_id, "item", [catalog.nfc(n) for n in names],
                              author=author, kind=kind)
    if not names:
        return store.seed_arc(ctx.store_dir, arc_id, target_type,
                              catalog.names(ctx.cfg, target_type), author=author, kind=kind)
    return store.seed_arc(ctx.store_dir, arc_id, target_type, [catalog.nfc(n) for n in names],
                          author=author, kind=kind,
                          canonicalize=lambda ns: canonicalize_names(ctx, target_type, ns))


def create_arc(ctx: Ctx, name: str, description: str, scope: str,
                    *, author: str) -> store.Arc:
    return store.create_arc(ctx.store_dir, name, description, scope,
                                 author=author, legal_scopes=ctx.arc_scopes)


# Pass-throughs. They forward and nothing else, which looks like ceremony --
# but they are what makes "gui/ never touches store's writers" a property a
# test can check (tests/test_gui_seam.py) rather than a habit reviewers must
# remember. store.py has eleven writers; an allowlist can only be absolute if
# every legitimate GUI write has a door here.
def rename_arc(ctx: Ctx, arc_id: str, new_name: str) -> store.Arc:
    return store.rename_arc(ctx.store_dir, arc_id, new_name)


def archive_arc(ctx: Ctx, arc_id: str) -> store.Arc:
    return store.archive_arc(ctx.store_dir, arc_id)


def commit(ctx: Ctx, message: str) -> bool:
    """A commit is not off this disk until a push, and a bare `git push`
    needs an upstream: set it here when the store has an origin, so the
    push hint the CLI prints works on the first try."""
    ok = store.commit(ctx.store_dir, message, ctx.cfg)
    gitref.set_upstream(ctx.store_dir)
    return ok
