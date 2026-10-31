"""The map: a store's heads as a treemap (the spec, 2026-10-29).

A tile's area is its entries, its colour its open ones, and the tiles nest on
up to three axes the URL names. As with the browse view's filters, the view
IS the query string: a control is a link to another view, so the back button,
bookmarks and shared links need nothing more.

No nicegui import: these are pure functions and they test without the extra.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product
from math import log
from typing import Mapping
from urllib.parse import urlencode

from .. import store as S
from .. import summary as summ
from . import filters

# Axis key -> the label its control shows, in the controls' order.
AXES = {"target": "target", "tag": "tag", "kind": "kind", "status": "status",
        "author": "author", "arc": "arc", "week": "week written"}
SHOWS = {"all": "every entry", "open": "open only (parked too)",
         "status": "status kinds only"}
COLOURS = {"open": "open count", "kind": "kind", "new": "newest entry"}
MAX_AXES = 3


@dataclass(frozen=True)
class View:
    by: tuple[str, ...] = ("target",)
    show: str = "all"
    color: str = "open"
    q: str = ""                     # the search box's words, as /notes reads them


def view_from(params: Mapping[str, str]) -> View:
    """Query params -> the view they name. A typed URL is a trust boundary:
    an unknown axis or a repeat drops, past three are cut, and an unknown
    value reads as the default."""
    by = tuple(dict.fromkeys(a for a in (s.strip() for s in (params.get("by") or "").split(","))
                             if a in AXES))[:MAX_AXES]
    show, color = params.get("show"), params.get("color")
    return View(by=by or View.by, show=show if show in SHOWS else View.show,
                color=color if color in COLOURS else View.color,
                q=(params.get("q") or "").strip())


def href(view: View) -> str:
    """The /map URL for this view, naming only what differs from the default."""
    q = {k: v for k, v, d in (("by", ",".join(view.by), ",".join(View.by)),
                              ("show", view.show, View.show),
                              ("color", view.color, View.color),
                              ("q", view.q, View.q)) if v != d}
    return f"/map?{urlencode(q)}" if q else "/map"


# A step down the tree: (axis, key, label). The axis is an AXES key, or "dir"
# for a target's type or a directory on its way to a "target" step, whose key
# is the (type, name) itself.
Step = tuple


@dataclass
class Tile:
    label: str
    steps: tuple = ()                               # from the root, this tile's last
    kids: dict = field(default_factory=dict)        # (axis, key) -> Tile
    own: list = field(default_factory=list)         # notes placed on this tile


def objects(n) -> list:
    """The objects an entry is on or cites, its own target first, each once:
    how /targets, /docs and /object count a target's entries."""
    return list(dict.fromkeys([(n.target.type, n.target.name),
                               *((r.type, r.name) for r in n.refs)]))


def live(n) -> bool:
    """Open work: open and not parked, as the Docs page's "N open" counts."""
    return S.read_status(n) == "open" and not n.spec.parked


def written(n) -> str:
    """When the entry was first written: a head's own stamp moves on every edit."""
    return n.first_written or n.created_at


def when(n) -> datetime:
    """`written` as an instant, as every order sorts: a string sort misorders
    two zones, and a hand-edited stamp that is not ISO reads oldest."""
    return S.written_at(n, written(n))


def _monday(n) -> str | None:
    """The Monday of the week an entry was written, in the zone the GUI shows
    times in; None for a stamp that is not ISO, before it overflows a zone
    west of UTC."""
    dt = when(n)
    if dt.year == 1:
        return None
    d = dt.astimezone(S.zone()).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def status(n) -> str:
    """open, parked, resolved, or plain for a kind with no status."""
    s = S.read_status(n) if n.spec.status else None
    return "parked" if s == "open" and n.spec.parked else s or "plain"


def _target(typ: str, name, path_types) -> tuple:
    if not name:
        return (("target", (typ, name), typ),)
    segs = ([s for s in name.split("/") if s] or [name]) if typ in path_types else [name]
    leaf = segs[-1] if typ in path_types else name[:7] if typ == "commit" else name
    return (("dir", typ, typ), *(("dir", s, s) for s in segs[:-1]), ("target", (typ, name), leaf))


def placements(n, axis: str, *, arcs: dict, path_types: frozenset) -> list:
    """Where one entry sits on one axis: a path of steps for each place, so an
    entry with two tags, or a target and a ref, sits in two."""
    if axis == "target":
        return [_target(t, name, path_types) for t, name in objects(n)]
    if axis == "tag":
        return [(("tag", t, "#" + t),) for t in n.tags] or [(("tag", None, "(no tag)"),)]
    if axis == "arc":
        return [(("arc", n.arc_id, arcs.get(n.arc_id, n.arc_id)) if n.arc_id
                 else ("arc", None, "(no arc)"),)]
    if axis == "status":
        s = status(n)
        return [(("status", s, s),)]
    if axis == "week":
        w = _monday(n)
        return [(("week", w, f"week of {w}" if w else "(no date)"),)]
    v = n.kind if axis == "kind" else n.author
    return [((axis, v, v),)]


_SHOWN = {"all": lambda n: True, "open": lambda n: S.read_status(n) == "open",
          "status": lambda n: bool(n.spec.status)}


def build(notes, view: View, *, arcs: dict, path_types: frozenset) -> Tile:
    """The tile tree of the heads among `notes` that `view.show` admits and
    `view.q` finds, nested on `view.by`. As on /notes, a pasted id joins the
    hits, and a superseded one brings its current row: `notes` holds every
    version, so its chain can be walked."""
    root = Tile("store")
    shown = list(filter(_SHOWN[view.show], S.heads(notes)))
    found = S.query(shown, grep=filters.search_pattern(view.q))
    if view.q:
        tips = {S.chain_tip(notes, n.id).id for n in notes if filters.id_hit(n.id, view.q)}
        got = {n.id for n in found}
        found += [n for n in shown if n.id in tips and n.id not in got]
    for n in found:
        for parts in product(*(placements(n, a, arcs=arcs, path_types=path_types)
                               for a in view.by)):
            t = root
            for step in (s for part in parts for s in part):
                if step[:2] not in t.kids:
                    t.kids[step[:2]] = Tile(step[2], t.steps + (step,))
                t = t.kids[step[:2]]
            t.own.append(n)
    _nest(root)
    return _compact(root)


def _nest(t: Tile) -> None:
    """A target named as a directory (src/pkg, beside src/pkg/a.py) goes inside
    that directory's tile, as a tile of its own."""
    dirs = {k.label: k for k in t.kids.values() if k.steps[-1][0] == "dir"}
    for key, k in list(t.kids.items()):
        if k.steps[-1][0] == "target" and k.label in dirs:
            del t.kids[key]
            dirs[k.label].kids[key] = k
            k.label += " itself"
    for k in t.kids.values():
        _nest(k)


def _compact(t: Tile) -> Tile:
    """A directory with one child and no entries of its own becomes that
    child, its label the path: file/src/pkg, not file holding src holding pkg."""
    t.kids = {key: _compact(k) for key, k in t.kids.items()}
    if t.steps and t.steps[-1][0] == "dir" and len(t.kids) == 1 and not t.own:
        (k,) = t.kids.values()
        k.label = f"{t.label}/{k.label}"
        return k
    return t


def count(t: Tile) -> int:
    """Placements in the subtree: the tile's area."""
    return len(t.own) + sum(count(k) for k in t.kids.values())


def entries(t: Tile) -> list:
    """The distinct entries in the subtree, open work first, then the newest."""
    seen, todo = {}, [t]
    while todo:
        x = todo.pop()
        seen.update((n.id, n) for n in x.own)
        todo.extend(x.kids.values())
    rows = sorted(seen.values(), key=when, reverse=True)
    rows.sort(key=lambda n: not live(n))             # stable: newest within each
    return rows


# Axis -> the /notes filter that lists the same entries.
_FILTERS = {"tag": "tag", "kind": "kind", "author": "author", "arc": "arc", "status": "status"}


def see_all(t: Tile, view: View):
    """Where a page lists exactly this tile's entries: ("object", its target)
    or ("notes", filters for filters.href), else None. /object has no filter,
    so it fits only a view of every entry and no search; /notes?status=open
    keeps parked, which the status axis splits off; /notes?type=&name= matches
    a row's own target only, so a target joined to another axis has no such
    page."""
    axes = {s[0] for s in t.steps}
    if t.steps and t.steps[-1][0] == "target" and axes <= {"dir", "target"}:
        typ, name = t.steps[-1][1]
        return ("object", {"type": typ, "name": name}) \
            if view.show == "all" and not view.q else None
    if not t.steps or view.show == "status" or not axes <= _FILTERS.keys() or \
            any(k is None or (a == "status" and k != "resolved") for a, k, _ in t.steps):
        return None
    q = {_FILTERS[a]: k for a, k, _ in t.steps} | ({"q": view.q} if view.q else {})
    return ("notes", q | {"status": "open"} if view.show == "open" else q)


# Days within which a tile's newest entry takes each step, brightest first.
NEW_DAYS = (1, 3, 7, 14, 21)


def options(root: Tile, view: View, *, kind_hex, palette, ramps, now: datetime) -> tuple:
    """ECharts options for the tree, and the tiles a node's `id` indexes: a
    click sends the id, and the server, not the chart, knows the entries. The
    id is ECharts' own too, the one `treemapRootToNode` finds a node by.
    `ramps` is theme's (MAP_EMPTY, MAP_TEXT, MAP_OPEN, MAP_NEW), passed in so
    this module holds no colour."""
    empty, text, opens, news = ramps
    tiles: list[Tile] = []
    leaves, todo = [], list(root.kids.values())
    while todo:
        x = todo.pop()
        (todo.extend(x.kids.values()) if x.kids else leaves.append(x))
    top = max((sum(map(live, x.own)) for x in leaves), default=0)

    def colour(rows: list) -> tuple[str, str]:
        """A tile's colours, from the entries in it, a group's as a leaf's: a
        group too small to show its children draws as a tile of its own."""
        if view.color == "kind":
            kinds = Counter(n.kind for n in rows)
            return kind_hex(min(kinds, key=lambda k: (-kinds[k], k))), text
        if view.color == "new":
            age = (now - max(map(when, rows))).total_seconds() / 86400
            step = next((i for i, d in enumerate(NEW_DAYS) if age <= d), None)
            return (empty, palette.text_emph) if step is None else (news[-1 - step], text)
        n = sum(map(live, rows))
        if not n:
            return empty, palette.text_emph
        # log, so one open entry still shows beside the tile with the most;
        # a group can hold more than any leaf
        last = len(opens) - 1
        return opens[last if top == 1 else min(last, round(last * log(n) / log(top)))], text

    def node(t: Tile) -> dict:
        tiles.append(t)
        rows = entries(t)
        n = sum(map(live, rows))
        # the tooltip's path is the chart's own: a path in each tile's data
        # repeated every ancestor's label, megabytes on a deep store
        d = {"name": t.label, "value": count(t), "id": len(tiles) - 1,
             "text": f"{t.label}\n{len(rows)}" + (f" · {n} open" if n else ""),
             "tip": f"{summ._count(len(rows), 'entry')}, {n} open"}
        bg, fg = colour(rows)
        d["itemStyle"], d["label"] = {"color": bg}, {"color": fg}
        if t.kids:
            d["children"] = [node(k) for k in t.kids.values()]
        return d

    data = [node(k) for k in root.kids.values()]
    level = lambda c, w, g: {"itemStyle": {"borderColor": c, "borderWidth": w, "gapWidth": g}}  # noqa: E731
    return {
        "tooltip": {"backgroundColor": palette.bg_page, "borderColor": palette.border_hi,
                    "textStyle": {"color": palette.text_emph},
                    ":formatter": "p => p.data && p.data.tip && p.treePathInfo.slice(1).map(x => "
                                  "x.name.replace(/[&<>\"']/g, c => `&#${c.charCodeAt(0)};`))"
                                  ".join(' › ') + '<br>' + p.data.tip"},
        "series": [{
            # the breadcrumb's root: unnamed, it was a blank chevron
            # the page's click fills the map with a group, as the breadcrumb does:
            # ECharts' zoomToNode scaled the whole tree and cut its neighbours off
            "type": "treemap", "name": root.label, "data": data, "roam": False,
            "nodeClick": False,
            # ECharts 6's treemap sits 20px in from the left: at width 100% it lost its right 20px
            "width": "100%", "height": "90%", "left": 0, "top": 0, "visibleMin": 60,
            "breadcrumb": {"show": True, "bottom": 0,
                           "itemStyle": {"color": palette.border, "borderColor": palette.border_hi,
                                         "textStyle": {"color": palette.text_emph}}},
            # ECharts formats its own root too, whose data has no text: its name stands in
            "label": {"show": True, "fontSize": 12, "overflow": "truncate",
                      ":formatter": "p => (p.data && p.data.text) || p.name"},
            "upperLabel": {"show": True, "height": 20, "fontSize": 12, "color": palette.text_emph,
                           ":formatter": "p => ((p.data && p.data.text) || p.name).replace('\\n', '  ')"},
            # A level past `levels` takes this: ECharts' own border is white,
            # and a group's header strip is drawn in its border's colour.
            "itemStyle": {"borderColor": palette.border_hi, "borderWidth": 2, "gapWidth": 1},
            "levels": [level(palette.bg_page, 0, 4), level(palette.border, 3, 2),
                       level(palette.border_hi, 2, 1), level(palette.border_hi, 2, 1)],
        }],
    }, tiles
