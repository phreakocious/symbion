import pytest

from symbion import api
from symbion import store as S
from symbion.gui.treemap import (COLOURS, View, build, count, entries, href, live, placements,
                                 see_all, view_from, written)

pytestmark = pytest.mark.usefixtures("tmp_store")

PATHS = frozenset({"file", "doc"})


def _add(ctx, kind, typ, name, **row):
    return api.add(ctx, {"kind": kind, "target": {"type": typ, "name": name},
                         "body": row.pop("body", "about it"), **row}, author="ada")


def _ctx(tmp_path):
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    return api.resolve(str(tmp_path))


def _heads(tmp_path):
    return S.heads(S.load(tmp_path))


def _kid(tile, label):
    (k,) = [k for k in tile.kids.values() if k.label == label]
    return k


def _labels(tile):
    return sorted(k.label for k in tile.kids.values())


def test_a_view_reads_from_the_url_and_writes_back_to_it():
    v = view_from({"by": "tag,kind", "show": "open", "color": "kind"})
    assert v == View(by=("tag", "kind"), show="open", color="kind")
    assert href(v) == "/map?by=tag%2Ckind&show=open&color=kind"
    assert view_from(dict(p.split("=") for p in
                          href(v).split("?")[1].replace("%2C", ",").split("&"))) == v


def test_a_search_maps_only_what_it_finds(repo, tmp_path):
    """The search box's words, as /notes reads them: every word, any case,
    in body, target, tags. The URL keeps them; a tile's page to all carries
    them, and /object, which has no search, fits no searched tile."""
    ctx = _ctx(tmp_path)
    _add(ctx, "bug", "item", "x", body="Treemap colours", tags=["gui"])
    _add(ctx, "bug", "item", "y", body="other", tags=["treemap"])
    _add(ctx, "bug", "item", "z", body="colours elsewhere")
    v = view_from({"by": "tag", "q": "  treemap "})
    assert (v.q, href(v)) == ("treemap", "/map?by=tag&q=treemap")
    root = build(_heads(tmp_path), v, arcs={}, path_types=PATHS)
    assert (_labels(root), len(entries(root))) == (["#gui", "#treemap"], 2)
    assert see_all(_kid(root, "#gui"), v) == ("notes", {"tag": "gui", "q": "treemap"})
    two = View(q="treemap colours")
    x = _kid(build(_heads(tmp_path), two, arcs={}, path_types=PATHS), "item/x")   # one child
    assert see_all(x, two) is None


def test_the_default_view_is_a_bare_map():
    assert view_from({}) == View()
    assert href(View()) == "/map"
    assert href(View(color="kind")) == "/map?color=kind"


def test_a_hand_typed_url_reads_as_the_nearest_view():
    """Unknown axes and repeats drop, three at most, an unknown value reads
    as the default: a typed URL is a trust boundary."""
    v = view_from({"by": " nonsense, tag,tag ,kind,author,arc", "show": "x", "color": ""})
    assert v == View(by=("tag", "kind", "author"))
    assert view_from({"by": ",,"}) == View()


def test_a_target_counts_its_refs_and_nests_by_directory(repo, tmp_path):
    """As /targets counts: an entry on its target and on each object it
    cites. A directory with one child reads as one tile."""
    ctx = _ctx(tmp_path)
    _add(ctx, "note", "file", "src/pkg/a.py", refs=[{"type": "file", "name": "src/pkg/b.py"}])
    _add(ctx, "note", "item", "x")
    root = build(_heads(tmp_path), View(), arcs={}, path_types=PATHS)
    assert _labels(root) == ["file/src/pkg", "item/x"]
    pkg = _kid(root, "file/src/pkg")
    assert {k.label: count(k) for k in pkg.kids.values()} == {"a.py": 1, "b.py": 1}
    assert (count(root), len(entries(root))) == (3, 2)
    a = _kid(pkg, "a.py")
    assert see_all(a, View()) == ("object", {"type": "file", "name": "src/pkg/a.py"})
    assert see_all(a, View(show="open")) is None        # the object page has no filter


def test_a_target_named_as_a_directory_sits_inside_it(repo, tmp_path):
    ctx = _ctx(tmp_path)
    _add(ctx, "note", "file", "src/pkg")
    _add(ctx, "note", "file", "src/pkg/a.py")
    pkg = _kid(build(_heads(tmp_path), View(), arcs={}, path_types=PATHS), "file/src/pkg")
    assert _labels(pkg) == ["a.py", "pkg itself"]
    assert see_all(_kid(pkg, "pkg itself"), View()) == ("object", {"type": "file", "name": "src/pkg"})


def test_a_path_type_nests_by_directory_and_others_do_not(repo, tmp_path):
    """A store over several repos names a type per repo: each nests as file: does."""
    ctx = _ctx(tmp_path)
    n = _add(ctx, "note", "item", "app/x.ts")
    assert placements(n, "target", arcs={}, path_types=PATHS | {"item"}) == [
        (("dir", "item", "item"), ("dir", "app", "app"), ("target", ("item", "app/x.ts"), "x.ts"))]
    assert placements(n, "target", arcs={}, path_types=PATHS) == [
        (("dir", "item", "item"), ("target", ("item", "app/x.ts"), "app/x.ts"))]


def test_an_arc_and_a_tag_read_by_name(repo, tmp_path):
    ctx = _ctx(tmp_path)
    act = api.create_arc(ctx, "Release", "", "item", author="ada")
    n = _add(ctx, "task", "item", "x", arc_id=act.id, tags=["gui", "ux"])
    bare = _add(ctx, "note", "item", "y")
    assert placements(n, "arc", arcs={act.id: "Release"}, path_types=PATHS) == [
        (("arc", act.id, "Release"),)]
    assert placements(bare, "arc", arcs={}, path_types=PATHS) == [(("arc", None, "(no arc)"),)]
    assert placements(n, "tag", arcs={}, path_types=PATHS) == [
        (("tag", "gui", "#gui"),), (("tag", "ux", "#ux"),)]
    assert placements(bare, "tag", arcs={}, path_types=PATHS) == [(("tag", None, "(no tag)"),)]


def test_open_means_live_and_show_open_keeps_parked(repo, tmp_path):
    ctx = _ctx(tmp_path)
    t = _add(ctx, "task", "item", "x", body="live")
    done = _add(ctx, "task", "item", "x", body="done")
    _add(ctx, "idea", "item", "x")
    _add(ctx, "note", "item", "x")
    api.supersede(ctx, done.id, author="ada", status="resolved")
    notes = _heads(tmp_path)
    by = ("status",)
    assert _labels(build(notes, View(by=by), arcs={}, path_types=PATHS)) == [
        "open", "parked", "plain", "resolved"]
    assert _labels(build(notes, View(by=by, show="status"), arcs={}, path_types=PATHS)) == [
        "open", "parked", "resolved"]
    root = build(notes, View(by=by, show="open"), arcs={}, path_types=PATHS)
    assert _labels(root) == ["open", "parked"]
    assert [n.id for n in notes if live(n)] == [t.id]
    assert entries(root)[0].id == t.id               # live first, though the idea is newer


def test_written_is_the_first_stamp_not_the_heads(repo, tmp_path, monkeypatch):
    """A head's stamp moves on every edit: a retag sweep would move each row
    it touched into this week."""
    monkeypatch.delenv("TZ", raising=False)
    ctx = _ctx(tmp_path)
    monkeypatch.setenv("SYMBION_NOW", "2026-01-05T10:00:00Z")      # a Monday, far from today
    n = _add(ctx, "task", "item", "x")
    monkeypatch.setenv("SYMBION_NOW", "2026-01-14T10:00:00Z")
    api.supersede(ctx, n.id, author="ada", tags=["later"])
    (head,) = _heads(tmp_path)
    assert written(head).startswith("2026-01-05")
    assert placements(head, "week", arcs={}, path_types=PATHS) == [
        (("week", "2026-01-05", "week of 2026-01-05"),)]


def test_see_all_maps_only_where_a_page_lists_the_same_entries(repo, tmp_path):
    ctx = _ctx(tmp_path)
    _add(ctx, "bug", "item", "x", tags=["gui"])
    _add(ctx, "task", "item", "y")
    done = _add(ctx, "task", "item", "z")
    api.supersede(ctx, done.id, author="ada", status="resolved")
    notes = _heads(tmp_path)
    tk = View(by=("tag", "kind"))
    root = build(notes, tk, arcs={}, path_types=PATHS)
    assert see_all(_kid(_kid(root, "#gui"), "bug"), tk) == ("notes", {"tag": "gui", "kind": "bug"})
    assert see_all(_kid(root, "(no tag)"), tk) is None
    tko = View(by=("tag", "kind"), show="open")
    assert see_all(_kid(_kid(build(notes, tko, arcs={}, path_types=PATHS), "#gui"), "bug"), tko) \
        == ("notes", {"tag": "gui", "kind": "bug", "status": "open"})
    st = View(by=("status",))
    sroot = build(notes, st, arcs={}, path_types=PATHS)
    assert see_all(_kid(sroot, "resolved"), st) == ("notes", {"status": "resolved"})
    assert see_all(_kid(sroot, "open"), st) is None     # /notes' open keeps parked
    tg = View(by=("target", "kind"))
    x = _kid(_kid(build(notes, tg, arcs={}, path_types=PATHS), "item"), "x")
    assert see_all(x, tg) == ("object", {"type": "item", "name": "x"})
    assert see_all(_kid(x, "bug"), tg) is None


def test_an_empty_view_builds_an_empty_root(repo, tmp_path):
    root = build([], View(), arcs={}, path_types=PATHS)
    assert (count(root), root.kids, entries(root)) == (0, {}, [])


def _options(tmp_path, view, now=None):
    from datetime import datetime, timezone

    from symbion import term
    from symbion.gui import theme as Th
    from symbion.gui.treemap import options
    root = build(_heads(tmp_path), view, arcs={}, path_types=PATHS)
    return options(root, view, kind_hex=term.kind_hex, palette=Th.PALETTE,
                   ramps=(Th.MAP_EMPTY, Th.MAP_TEXT, Th.MAP_OPEN, Th.MAP_NEW),
                   now=now or datetime.now(timezone.utc))


def _nodes(opts):
    todo, out = list(opts["series"][0]["data"]), {}
    while todo:
        d = todo.pop()
        out[d["name"]] = d
        todo.extend(d.get("children", []))
    return out


def test_colour_by_open_count_steps_up_with_the_count(repo, tmp_path):
    from symbion.gui import theme as Th
    ctx = _ctx(tmp_path)
    _add(ctx, "note", "item", "none")
    _add(ctx, "task", "item", "one")
    for i in range(12):
        _add(ctx, "task", "item", "many", body=f"t{i}")
    got = {k: (d["itemStyle"]["color"], d["label"]["color"])
           for k, d in _nodes(_options(tmp_path, View())[0]).items() if k != "item"}
    assert got == {"none": (Th.MAP_EMPTY, Th.PALETTE.text_emph),
                   "one": (Th.MAP_OPEN[0], Th.MAP_TEXT), "many": (Th.MAP_OPEN[4], Th.MAP_TEXT)}


def test_a_parked_entry_lights_nothing(repo, tmp_path):
    from symbion.gui import theme as Th
    _add(_ctx(tmp_path), "idea", "item", "x")
    (x,) = [d for k, d in _nodes(_options(tmp_path, View())[0]).items() if k == "item/x"]
    assert (x["itemStyle"]["color"], x["text"]) == (Th.MAP_EMPTY, "item/x\n1")


def test_a_tile_says_its_entries_once_and_its_open_ones(repo, tmp_path):
    """Its area counts an entry once per place; its words count it once."""
    _add(_ctx(tmp_path), "task", "file", "src/a.py", refs=[{"type": "file", "name": "src/b.py"}])
    nodes = _nodes(_options(tmp_path, View())[0])
    assert (nodes["file/src"]["value"], nodes["file/src"]["text"]) == (2, "file/src\n1 · 1 open")
    assert nodes["a.py"]["text"] == "a.py\n1 · 1 open"


def test_a_tooltip_names_no_path_the_chart_has_it(repo, tmp_path):
    """Each tile's data repeated every ancestor's label, megabytes on a deep
    store. The formatter reads the path from the chart's own tree, and
    escapes each name there: a name is text, never markup."""
    _add(_ctx(tmp_path), "note", "file", "src/pkg/a<b>.py")
    opts = _options(tmp_path, View())[0]
    assert [d["tip"] for d in _nodes(opts).values()] == ["1 entry, 0 open"]
    assert opts["series"][0]["name"] == "store"            # the breadcrumb's root
    fmt = opts["tooltip"][":formatter"]
    assert "treePathInfo" in fmt and "&#${c.charCodeAt(0)};" in fmt


def test_the_chart_is_an_image_named_by_its_view(repo, tmp_path):
    """The canvas said nothing to a screen reader (the design critique,
    2026-11-07). An entry counts once in the label, as in a tile's words."""
    _add(_ctx(tmp_path), "task", "file", "a.py", refs=[{"type": "file", "name": "b.py"}])
    aria = _options(tmp_path, View(by=("target", "kind")))[0]["aria"]
    assert aria == {"enabled": True, "description": "A treemap of 1 entry by target then kind. "
                    "The map tiles toolbar after it names each tile."}


def test_a_pasted_id_maps_its_entry_and_a_superseded_one_its_head(repo, tmp_path):
    """As /notes reads the box: an id is not text the pattern reads."""
    ctx = _ctx(tmp_path)
    a = _add(ctx, "bug", "item", "x", body="nothing alike")
    _add(ctx, "bug", "item", "y", body="other")
    old = _add(ctx, "task", "item", "z", body="before")
    api.supersede(ctx, old.id, author="ada", body="after")
    rows = S.load(tmp_path)
    (saved,) = [n.id for n in rows if n.id != old.id and S.chain_tip(rows, n.id).id == old.id]
    for q, want in ((a.id[-10:], ["item/x"]), (saved, ["item/z"]),
                    (f"{a.id[-10:]} other", []), ("other", ["item/y"])):
        root = build(S.load(tmp_path), View(q=q), arcs={}, path_types=PATHS)
        assert _labels(root) == want, q


def test_an_id_finds_its_tile(repo, tmp_path):
    ctx = _ctx(tmp_path)
    _add(ctx, "note", "file", "src/a.py", tags=["gui"])
    _add(ctx, "bug", "item", "x")
    opts, tiles = _options(tmp_path, View(by=("tag", "target")))
    nodes = _nodes(opts)
    assert len(nodes) == len(tiles)
    assert all(tiles[d["id"]].label == name for name, d in nodes.items())


def test_colour_by_newest_entry_fades_over_three_weeks(repo, tmp_path, monkeypatch):
    from datetime import datetime

    from symbion.gui import theme as Th
    ctx = _ctx(tmp_path)
    for day, name in (("2026-01-30", "today"), ("2026-01-20", "ten"), ("2026-01-01", "old")):
        monkeypatch.setenv("SYMBION_NOW", f"{day}T10:00:00Z")
        _add(ctx, "note", "item", name)
    now = datetime.fromisoformat("2026-01-30T12:00:00+00:00")
    got = {k: d["itemStyle"]["color"]
           for k, d in _nodes(_options(tmp_path, View(color="new"), now)[0]).items() if k != "item"}
    assert got == {"today": Th.MAP_NEW[4], "ten": Th.MAP_NEW[1], "old": Th.MAP_EMPTY}


def test_colour_by_kind_takes_the_most_common(repo, tmp_path):
    from symbion import term
    ctx = _ctx(tmp_path)
    _add(ctx, "bug", "item", "x", body="b1")
    _add(ctx, "bug", "item", "x", body="b2")
    _add(ctx, "task", "item", "x")
    (x,) = [d for k, d in _nodes(_options(tmp_path, View(color="kind"))[0]).items() if k == "item/x"]
    assert x["itemStyle"]["color"] == term.kind_hex("bug")


def test_every_tile_carries_a_palette_colour(repo, tmp_path):
    """A directory too small to show its children draws as a tile of its own:
    with no colour of its own, ECharts gave it its default blue and white text."""
    from symbion import term
    from symbion.gui import theme as Th
    ctx = _ctx(tmp_path)
    _add(ctx, "task", "file", "src/a/x.py")
    _add(ctx, "bug", "file", "src/b/y.py")
    _add(ctx, "note", "item", "z")
    fills = {Th.MAP_EMPTY, *Th.MAP_OPEN, *Th.MAP_NEW, *map(term.kind_hex, ("task", "bug", "note"))}
    for colour in COLOURS:
        nodes = _nodes(_options(tmp_path, View(color=colour))[0])
        assert any("children" in d for d in nodes.values())
        for name, d in nodes.items():
            assert d.get("itemStyle", {}).get("color") in fills, (colour, name)
            assert d.get("label", {}).get("color") in {Th.MAP_TEXT, Th.PALETTE.text_emph}, (colour, name)


def test_entries_order_by_instant_and_a_bad_stamp_reads_oldest(repo, tmp_path, monkeypatch):
    """A string sort put 10-01T05:00+09:00 after 09-30T22:00+00:00, two hours
    later; one hand-edited stamp stopped the week and newest views."""
    from dataclasses import replace
    from datetime import datetime, timezone

    from symbion.gui import theme as Th
    from symbion.gui.treemap import options
    monkeypatch.delenv("TZ", raising=False)
    ctx = _ctx(tmp_path)
    for body in "abc":
        _add(ctx, "note", "item", "x", body=body)
    a, b, c = sorted(_heads(tmp_path), key=lambda n: n.body)
    notes = [replace(a, created_at="2026-10-01T05:00:00+09:00", first_written=None),
             replace(b, created_at="2026-09-30T22:00:00+00:00", first_written=None),
             replace(c, created_at="not a date", first_written=None)]
    assert [n.body for n in entries(build(notes, View(), arcs={}, path_types=PATHS))] == \
        ["b", "a", "c"]
    weeks = build(notes, View(by=("week",)), arcs={}, path_types=PATHS)
    assert _labels(weeks) == ["(no date)", "week of 2026-09-28"]
    new = View(color="new")
    options(build(notes, new, arcs={}, path_types=PATHS), new, kind_hex=lambda k: Th.MAP_TEXT,
            palette=Th.PALETTE, ramps=(Th.MAP_EMPTY, Th.MAP_TEXT, Th.MAP_OPEN, Th.MAP_NEW),
            now=datetime(2026, 10, 2, tzinfo=timezone.utc))
