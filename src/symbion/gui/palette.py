"""The palette's rows, which `_PALETTE_JS` in chrome.py asks `/palette` for
as the person types (the user's design, 2026-10-16). Empty, it lists
the places, the actions the top bar offers now and the other stores a serve
runs on. Typed, it lists entries whose id ends so, here then in the other
stores; places, arcs, targets and tags whose name holds every word; the
entries here that say it, open first; and how many entries say it in each
other store, each a link to that store's search.

A row is a dict. What it shows: `icon` or `kind` (a dot), `text` or `html`
(escaped, with the words in <mark>), and `key`, `meta`, `kind_name`, `id`,
`store`. What it does: `href`, or first `peek` (an entry here, shown in the
peek) or `click` (a top-bar button, pressed)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .. import store as S
from .. import summary as summ
from . import servers
from .chrome import _PLACES, git_state
from .filters import href, id_hit, search_pattern
from .notes import kind_class, marked, snippet, target_href

# Rows a typed query shows: entries here, and each kind of place.
ENTRIES = 4
EACH = 2

# What a row's text holds of an entry's body: one line, cut by the browser.
_CHARS = 120

# (icon, label, the top-bar button it presses, its key). The buttons in
# `_LIVE` show only when they would do something, as the top bar's do.
_ACTIONS = (("add", "New entry", ".sb-new", "n"),
            ("save", "Commit your entries", ".sb-commit", ""),
            ("cloud_upload", "Push the store", ".sb-push", ""),
            ("undo", "Undo your last write", ".sb-undo", ""),
            ("keyboard", "Keyboard shortcuts", ".sb-keys", "?"),
            ("help_outline", "How symbion works", ".sb-help", ""))
_LIVE = {".sb-commit", ".sb-push", ".sb-undo"}

# store dir -> ((mtime_ns, size), heads). ponytail: one entry per store a
# serve has run on since this one started, never dropped.
_HEADS: dict = {}


def heads(store) -> list:
    """A store's heads, parsed again only when its file changed. A cold read
    of the running stores took about 0.2 s, 90% of it parsing (2026-10-16),
    and the palette reads every running store at each pause in the typing."""
    try:
        st = S.notes_path(store).stat()
    except OSError:
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    got = _HEADS.get(str(store))
    if got is None or got[0] != stamp:
        got = _HEADS[str(store)] = (stamp, S.heads(S.load(store)))
    return got[1]


def rows(ctx, author: str, q: str) -> list[dict]:
    """The palette's groups, `{"head", "aside", "rows"}`, for what is typed."""
    q = q.strip()
    here = heads(ctx.store_dir)
    me = str(Path(ctx.store_dir).resolve())
    others = sorted((r for r in servers.running() if r["store"] != me),
                    key=lambda r: servers.name(r["store"]))
    if not q:
        active = sum(not a.archived for a in S.load_arcs(ctx.store_dir))
        meta = {"/notes": str(len(here)), "/arcs": f"{active} active" if active else ""}
        return [g for g in (
            _group("go to", [{"icon": icon, "text": label, "href": path, "meta": meta.get(path, "")}
                             for label, path, icon in _PLACES]),
            _group("do", _actions(ctx, author, lambda _: True)),
            _group("other stores", [_store(r, summ._count(len(heads(r["store"])), "entry"),
                                           r["url"]) for r in others])) if g["rows"]]

    words = q.split()
    low = [w.lower() for w in words]

    def has(text: str) -> bool:
        return all(w in text.lower() for w in low)

    groups = []
    # An id, or its tail as summary and list print it: a pasted id is how
    # an agent's entry reaches the person.
    tail = [S.id_tail(q)]
    by_id = [_entry(n, (), tail) for n in here if id_hit(n.id, q)] + \
        [_entry(n, (), tail, r) for r in others for n in heads(r["store"]) if id_hit(n.id, q)]
    if by_id:
        groups.append(_group("entries by id", by_id, "here first, then other stores"))

    places = [{"icon": icon, "html": marked(label, words), "href": path}
              for label, path, icon in _PLACES if has(label)]
    arcs = [{"icon": "linear_scale", "html": marked(a.name, words),
             "meta": "arc · {}/{}".format(*S.arc_progress(here, a.id)),
             "href": target_href("arc", a.id)}
            for a in sorted(S.load_arcs(ctx.store_dir), key=lambda a: a.archived)
            if has(f"{a.id} {a.name}")]
    # Targets as the Targets page counts them: every entry on one or citing it.
    on = Counter(t for n in S.newest_first(here)
                 for t in dict.fromkeys([(n.target.type, n.target.name),
                                         *((r.type, r.name) for r in n.refs)])
                 if t[1] and t[0] != "arc")
    targets = [{"icon": "my_location", "html": marked(name, words), "mono": typ != "item",
                "meta": f"{typ} · {summ._count(c, 'entry')}", "href": target_href(typ, name)}
               for (typ, name), c in on.most_common() if has(f"{typ}:{name}")]
    tags = [{"icon": "tag", "html": marked(f"#{t}", words), "mono": True,
             "meta": f"tag · {summ._count(c, 'entry')}", "href": href(tag=t)}
            for t, c in Counter(t for n in here for t in n.tags).most_common() if has(f"#{t}")]
    found = places + arcs + targets + tags
    shown = places + arcs[:EACH] + targets[:EACH] + tags[:EACH]
    if shown:
        groups.append(_group("go to", shown, _of(len(shown), len(found))))
    if acts := _actions(ctx, author, has):
        groups.append(_group("do", acts))

    grep = search_pattern(q)
    hits = S.query(here, grep=grep)
    if hits:
        # Open first, then entries with no status, then closed: newest first in each.
        rank = {"open": 0, None: 1}
        hits = sorted(S.newest_first(hits), key=lambda n: rank.get(S.read_status(n), 2))
        groups.append(_group(
            "entries here",
            [_entry(n, words) for n in hits[:ENTRIES]]
            + [{"icon": "search", "text": f"All {summ._count(len(hits), 'entry')} here "
                                          f"that say “{q}”",
                "meta": "All entries", "href": href(q=q)}],
            " · ".join(filter(None, (_of(min(len(hits), ENTRIES), len(hits)), "open first")))))
    say = [(r, len(S.query(heads(r["store"]), grep=grep))) for r in others]
    say = sorted(((r, c) for r, c in say if c), key=lambda rc: -rc[1])
    # "none of N" beside the entries here that say it; beside an id found in
    # one of them it read as a contradiction, and alone the box says nothing
    # matched.
    if say or (hits and others):
        groups.append(_group(
            "other stores",
            [_store(r, summ._count(c, "entry"), f"{_url(r)}{href(q=q)}") for r, c in say],
            f"{len(say)} of {len(others)} say it" if say else f"none of {len(others)} say it"))
    return groups


def _group(head: str, rows: list, aside: str = "") -> dict:
    return {"head": head, "aside": aside, "rows": rows}


def _of(shown: int, every: int) -> str:
    return f"{shown} of {every}" if every > shown else ""


def _url(r: dict) -> str:
    return r["url"].rstrip("/")


def _store(r: dict, meta: str, to: str) -> dict:
    return {"icon": "swap_horiz", "text": servers.name(r["store"]), "meta": meta, "href": to}


def _actions(ctx, author: str, has) -> list[dict]:
    """The actions whose label holds every word. The top bar's own git read
    runs only when one of its buttons is among them."""
    picked = [a for a in _ACTIONS if has(a[1])]
    state = {}                          # button -> (shown, meta)
    if any(a[2] in _LIVE for a in picked):
        mine, _, registry, unpushed, undoable = git_state(ctx, author)
        state = {".sb-commit": (mine or registry,
                                summ._count(mine, "entry") if mine else "registry"),
                 ".sb-push": (unpushed, f"{summ._count(unpushed or 0, 'commit')} not on its remote"),
                 ".sb-undo": (undoable, "")}
    return [{"icon": icon, "text": label, "click": button, "key": key, "meta": meta,
             "notable": button in (".sb-commit", ".sb-push")}
            for icon, label, button, key in picked
            for shown, meta in [state.get(button, (True, ""))] if shown]


def _entry(n, words, tail=(), r: dict | None = None) -> dict:
    """An entry: here, `peek` shows it beside the page; in another store `r`,
    its page there opens. Its id's tail is marked when the id was typed."""
    row = {"kind": kind_class(n.kind), "kind_name": n.kind,
           "html": marked(snippet(n.body or n.checked or n.result or "", words, _CHARS), words),
           "id": marked(n.id[-10:], [t for t in tail if t and n.id.endswith(t)]),
           "done": S.read_status(n) == "resolved", "href": href(id=n.id)}
    if r is None:
        row["peek"] = n.id
    else:
        row.update(store=servers.name(r["store"]), href=_url(r) + row["href"])
    return row
