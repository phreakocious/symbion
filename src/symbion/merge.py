"""Merging two copies of a store: a set merge of notes.jsonl, a field merge of
arcs.jsonl, the git merge driver that runs them, and a pull's pending changes
put back on what the merge left. The standard library only: store imports this."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_MISSING = object()


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)       # as store._dumps writes a row


def _show(v) -> str:
    return "(absent)" if v is _MISSING else _dumps(v)


def _obj(line: str):
    """The line as a dict holding a string id, else None: such a line is kept
    as written and takes part in no id rule."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("id"), str) else None


def said_split(kept: str, new: str) -> str:
    """A split, as the driver says it and a pull prints it (SAID_SPLIT reads it)."""
    return f"both sides edited {kept}: the upstream's keeps the id, yours is now {new}"


SAID_SPLIT = re.compile(r"both sides edited (.+): the upstream's keeps the id, yours is now (.+)")


def _fresh(id: str, taken: set) -> str:
    """`id` with its last 3 hex digits stepped to the first free ones."""
    if re.fullmatch(r"[0-9a-f]{3}", id[-3:]):
        head, n = id[:-3], int(id[-3:], 16)
    else:                                            # an id made outside symbion
        head, n = id + "-", 0
    for _ in range(4096):
        n = (n + 1) % 4096
        if (new := f"{head}{n:03x}") not in taken:
            return new
    raise ValueError(f"every id from {head}000 to {head}fff is taken")


def merge_notes(base: list[str], ours: list[str], theirs: list[str],
                dropped: list[str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """The lines of notes.jsonl as a set against the base: a line either side
    removed is dropped (an edit renames the line it replaces, so it shows as
    removed), a line either side added stays. Ours in our order, then the lines
    only theirs added. Where both sides edited one entry, two heads share its
    id: theirs keeps it and ours gets the next free one. A head that is the
    base line rewritten in place (a sed over the file: same id, same
    `supersedes`) is no new version: the other side's head stands without a
    split, and where both rewrote the line differently, theirs stands and a
    message goes to `dropped`. Returns the lines and the splits as
    (kept_id, new_id)."""
    removed = (set(base) - set(ours)) | (set(base) - set(theirs))
    have = set(ours)
    mine = [ln for ln in ours if ln not in removed]
    keep = mine + [ln for ln in theirs if ln not in have and ln not in removed]
    return _settle(keep, range(len(mine)), base, dropped)


def reapply(old_head: list[str], working: list[str], merged: list[str], renames: dict[str, str],
            dropped: list[str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """The pending changes to notes.jsonl, `working` against `old_head`, put
    back on `merged`, the file a pull's merge left: `merged` less the lines
    they removed, then the lines they added, in their order. A line the merge
    split (`renames`, old id to new) is read under its new id, so a pending
    edit of it continues the entry on its own side: a set merge left that
    version on two heads. A pending head that shares an id with a merged one
    is settled as merge_notes settles ours against theirs. Returns the lines
    and the splits as (kept_id, new_id)."""
    def moved(lines):
        return [_dumps({**d, "id": renames[d["id"]]}) if (d := _obj(ln)) and d["id"] in renames
                else ln for ln in lines]
    head, now = set(old_head), set(working)
    removed = set(moved([ln for ln in old_head if ln not in now]))
    stays = [ln for ln in merged if ln not in removed]
    have = set(stays)
    added = [ln for ln in moved([ln for ln in working if ln not in head]) if ln not in have]
    return _settle(stays + added, range(len(stays), len(stays) + len(added)), moved(old_head),
                   dropped)


def _settle(keep: list[str], mine: range, base: list[str],
            dropped: list[str] | None) -> tuple[list[str], list[tuple[str, str]]]:
    """`keep` with each id that a line of ours (an index in `mine`) shares
    with an upstream line (any other) settled, by merge_notes' rules."""
    objs = [_obj(ln) for ln in keep]
    was = {d["id"]: d for ln in base if (d := _obj(ln))}

    def rewrite(d):
        return d["id"] in was and d.get("supersedes") == was[d["id"]].get("supersedes")

    upstream = {}
    for j, d in enumerate(objs):
        if d and j not in mine:
            upstream.setdefault(d["id"], []).append(j)
    taken = {d["id"] for d in objs if d}
    gone, splits = set(), []
    for i in mine:
        d = objs[i]
        if not d or d["id"] not in upstream:
            continue
        theirs_at = upstream[d["id"]]
        edited = [j for j in theirs_at if not rewrite(objs[j])]
        if rewrite(d):
            gone.add(i)
            if not edited and dropped is not None:
                dropped.append(f"{d['id']} was rewritten in place on both sides: the "
                               f"upstream's rewrite stays, yours is dropped")
        else:
            gone.update(j for j in theirs_at if rewrite(objs[j]))
            if edited:
                new = _fresh(d["id"], taken)
                taken.add(new)
                splits.append((d["id"], new))
                keep[i] = _dumps({**d, "id": new})
    return [ln for k, ln in enumerate(keep) if k not in gone], splits


def _merge_arc(base: dict, ours: dict, theirs: dict, dropped: list) -> dict:
    merged = {}
    for k in [*ours, *(k for k in theirs if k not in ours)]:
        b, o, t = base.get(k, _MISSING), ours.get(k, _MISSING), theirs.get(k, _MISSING)
        if o == t or t == b:
            v = o
        elif o == b:
            v = t
        elif k == "archived_at" and ours.get("archived") is True and theirs.get("archived") is True:
            # ponytail: stamps compare as text, which holds while all are UTC
            v = min((x for x in (o, t) if isinstance(x, str)), default=t)
        else:
            v = t
            dropped.append(f"arc {ours['id']}: kept the upstream's {k} {_show(t)}, "
                           f"dropped yours, {_show(o)}")
        if v is not _MISSING:
            merged[k] = v
    return merged


def merge_arcs(base: list[str], ours: list[str], theirs: list[str]) -> tuple[list[str], list[str]]:
    """arcs.jsonl by arc id, field by field against the base: a field one side
    changed takes that side's value, both sides archiving keeps the earlier
    archived_at, and any other field both changed differently takes the
    upstream's. Ours in our order, then the arcs only theirs holds. Returns the
    lines and one message for each value dropped."""
    b = {d["id"]: d for ln in base if (d := _obj(ln))}
    t = {d["id"]: (d, ln) for ln in theirs if (d := _obj(ln))}
    removed = (set(base) - set(ours)) | (set(base) - set(theirs))
    out, dropped, held = [], [], set()
    for ln in ours:
        d = _obj(ln)
        if d is None:
            if ln not in removed:
                out.append(ln)
            continue
        held.add(d["id"])
        if d["id"] not in t:
            out.append(ln)
            continue
        td, tline = t[d["id"]]
        merged = _merge_arc(b.get(d["id"], {}), d, td, dropped)
        out.append(ln if merged == d else tline if merged == td else _dumps(merged))
    have = set(ours)
    for ln in theirs:
        d = _obj(ln)
        if d is None and ln not in have and ln not in removed:
            out.append(ln)
        elif d is not None and d["id"] not in held:
            held.add(d["id"])
            out.append(ln)
    return out, dropped


def _lines(path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")    # CRLF reads as LF
    return text.removesuffix("\n").split("\n") if text else []


def _rebasing() -> bool:
    """Whether the repo in the cwd is mid-rebase. git hands a rebase's driver
    the sides the other way round: %A is the branch rebased onto, %B the
    commit replayed. The git dir is GIT_DIR, which git sets for the driver in
    a linked worktree and under --git-dir, else the cwd's .git: a directory,
    or a file naming one (a linked worktree's own). No git runs: git runs the
    driver from the store's root, where on Windows a bare `git` finds a
    git.exe the other side committed first."""
    git = Path(os.environ.get("GIT_DIR") or ".git")
    if git.is_file():
        text = git.read_text(encoding="utf-8", errors="replace").partition("\n")[0]
        if text.startswith("gitdir:"):
            git = git.parent / text.removeprefix("gitdir:").strip()  # relative: from the file
    return (git / "rebase-merge").exists() or (git / "rebase-apply").exists()


def driver(base: Path, ours: Path, theirs: Path, name: str) -> int:
    """git's merge driver: the merge for the file `name`, written to `ours`
    with LF endings. Each split and each dropped value is said on stderr in one
    line. 0 is a merge git takes as clean. The upstream is `theirs`, and in a
    rebase it is `ours`: the sides are swapped back before the merge."""
    which = Path(name).name
    b, o, t = _lines(base), _lines(ours), _lines(theirs)
    if _rebasing():
        o, t = t, o
    if which == "notes.jsonl":
        said = []
        merged, splits = merge_notes(b, o, t, dropped=said)
        said += [said_split(kept, new) for kept, new in splits]
    elif which == "arcs.jsonl":
        merged, said = merge_arcs(b, o, t)
    else:
        print(f"symbion: no merge for {name}", file=sys.stderr)
        return 1
    Path(ours).write_text("".join(ln + "\n" for ln in merged), encoding="utf-8", newline="\n")
    for line in said:
        print(f"symbion: {line}", file=sys.stderr)
    return 0
