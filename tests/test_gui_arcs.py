from __future__ import annotations

import pytest

pytest.importorskip("nicegui")

from nicegui import ui                    # noqa: E402
from nicegui.testing import User          # noqa: E402

from symbion import api, store            # noqa: E402
from symbion.gui.arcs import checklist   # noqa: E402
from _cards import cards, not_see, part, parts, see   # noqa: E402

pytestmark = pytest.mark.usefixtures("tmp_store")


def _tick(user, note_id: str):
    """The resolve part of the box whose card carries `note_id`."""
    (box,) = [c for c in cards(user) if c._props["data-id"] == note_id]
    return part(user, "note-resolve", card=box)


@pytest.fixture
def seeded(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.seed(ctx, act.id, "item", ["one", "two"], author="ada")
    return ctx, act


async def test_checklist_shows_every_seeded_target(user: User, seeded):
    ctx, act = seeded

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("one")
    await user.should_see("two")


async def test_ticking_a_row_resolves_its_task(user: User, seeded):
    ctx, act = seeded

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    item = store.arc_items(store.load(ctx.store_dir), act.id)[0]
    await user.open("/t")
    _tick(user, item.id).click()
    await user.should_see("one")

    done, total = store.arc_progress(store.load(ctx.store_dir), act.id)
    assert (done, total) == (1, 2)


async def test_ticking_a_done_row_reopens_it(user: User, seeded):
    """The other direction. A toggle asserted only on the way to resolved
    passes on a button hardwired to resolve."""
    ctx, act = seeded
    item = store.arc_items(store.load(ctx.store_dir), act.id)[0]
    api.supersede(ctx, item.id, author="ada", status="resolved")
    assert store.arc_progress(store.load(ctx.store_dir), act.id)[0] == 1

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    # The tick supersedes, so the live row carries a NEW id -- find the head.
    tip = next(n for n in store.arc_items(store.load(ctx.store_dir), act.id)
               if n.target.name == item.target.name)
    await user.open("/t")
    _tick(user, tip.id).click()
    await user.should_see("one")

    assert store.arc_progress(store.load(ctx.store_dir), act.id)[0] == 0


async def test_a_ticked_rows_id_tip_holds_its_age_stamp(user: User, seeded):
    """The age takes no focus, so a keyboard reads its stamp in the id's tip
    (the GUI review, 2026-11-07). An open box shows no age, and no stamp."""
    ctx, act = seeded
    item = store.arc_items(store.load(ctx.store_dir), act.id)[0]
    api.supersede(ctx, item.id, author="ada", status="resolved")
    heads = {n.target.name: n.id for n in store.arc_items(store.load(ctx.store_dir), act.id)}

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see(item.target.name)
    done = _card(user, heads.pop(item.target.name)).props["card"]
    (box,) = (_card(user, nid).props["card"] for nid in heads.values())
    assert f"written {done['age']['tip']}\n" in done["sid"]["tip"]
    assert box["age"] is None and "written" not in box["sid"]["tip"]


def test_a_tick_is_authored_by_the_human_not_claude(seeded, monkeypatch):
    """The tick goes through api.supersede with the GUI author. A supersede
    inherits target and provenance but NEVER the author -- a correction is
    authored by whoever made it."""
    ctx, act = seeded
    item = store.arc_items(store.load(ctx.store_dir), act.id)[0]

    out = api.supersede(ctx, item.id, author="ada", status="resolved")

    assert out.author == "ada"
    assert out.target == item.target


def test_item_scope_is_not_offered_as_a_seed_all_scope(seeded):
    """`item` has no catalog by construction, so 'seed all items' can only
    ever raise. ctx.seed_scopes includes it for the explicit-names path;
    the sweep button must filter it back out."""
    from symbion.gui.arcs import seed_all_scopes
    ctx, _ = seeded
    assert "item" in ctx.seed_scopes
    assert "item" not in seed_all_scopes(ctx)
    with pytest.raises(ValueError, match="at least one name"):
        api.seed_names(ctx, "item", None)


async def test_a_bodiless_box_is_its_target_and_kinds_show_only_when_mixed(user: User, seeded):
    """A seeded box drew an empty text line, then `task` and its target in
    chips, on every row of a catalog-sized checklist. Both directions: a
    box with a body keeps its text, and a second kind brings the chips back."""
    ctx, act = seeded

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    assert len(parts(user, "note-headline")) == 2
    await not_see(user, "checklist-body")
    await not_see(user, "checklist-kind")
    api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "one"},
                  "body": "broken thing", "arc_id": act.id}, author="ada")
    await user.open("/t")
    await user.should_see("broken thing")
    assert len(parts(user, "checklist-kind")) == 3
    assert len(parts(user, "note-headline")) == 2


async def test_a_project_row_on_the_checklist_is_clickable(user: User, seeded):
    """The bug: a project-target
    task filed under an arc rendered on the arc page with nothing
    to click. The checklist row is only ever its target chip, and `project` has no
    object page -- so this is the one target type for which the row's link had
    to be the note itself. Both directions: the seeded item row still points at
    the object view."""
    ctx, act = seeded
    g = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "no object page for me", "arc_id": act.id},
                author="ada")

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    hrefs = {e.props.get("href") for e in parts(user, "note-target")}
    assert f"/notes?id={g.id}" in hrefs, "the project entry must link to the note"
    assert "/object?type=item&name=one" in hrefs, "the item entry still links to its object"


PREREG = ('[kinds]\nprediction = { status = true, verdict = true }\n'
          'task = { status = true }\n')


async def test_ticking_a_prediction_opens_the_dialog_and_saving_a_result_resolves_it(
        user: User, repo, tmp_path):
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(PREREG)
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "item", "name": "sweep"},
                      "arc_id": act.id}, author="ada")

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    _tick(user, p.id).click()
    assert store.arc_progress(store.load(tmp_path), act.id) == (0, 1), "a tick alone must not close it"
    user.find(marker="edit-result").elements.pop().set_value("FIRED")
    user.find(marker="edit-save").click()
    assert store.arc_progress(store.load(tmp_path), act.id) == (1, 1)


def test_seedable_needs_a_declared_eligible_task(repo, tmp_path):
    from symbion.gui.arcs import seedable
    assert seedable(api.resolve(str(tmp_path))) is True
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text('[kinds]\nfollowup = { status = true }\n')
    assert seedable(api.resolve(str(tmp_path))) is False
    (tmp_path / "symbion.toml").write_text('[kinds]\ntask = { status = true, verdict = true }\n')
    assert seedable(api.resolve(str(tmp_path))) is False


from _resolvers import reading_store, calls   # noqa: E402


async def test_seed_all_is_a_sweep_that_never_runs_the_resolver(user: User, repo, tmp_path):
    """The button hands api.seed no names: a sweep. With a resolver declared
    that counts its calls, the count must not move and every catalog name
    must be seeded."""
    from symbion.gui.pages import build_page
    s = reading_store(tmp_path, counter=True)
    ctx = api.resolve(str(s))
    for f in ("40.40", "44.00"):
        api.add(ctx, {"kind": "note", "target": {"type": "reading", "name": f}}, author="ada")
    act = api.create_arc(ctx, "camp", "d", "reading", author="ada")
    before = calls(s)
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={act.id}")
    user.find(marker="arc-edit").click()
    user.find(marker="arc-seed").click()
    await user.should_see("seeded 2 new target(s)")
    assert calls(s) == before
    assert sorted(n.target.name for n in store.load(s) if n.arc_id == act.id) \
        == ["40.40", "44.00"]


async def test_a_rows_text_links_to_its_note(user: User, seeded):
    """Only the id linked to the note, and a reader did not find it. A short
    body links too: the row leaves out the note's tags, refs and edit."""
    ctx, act = seeded
    b = api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "one"},
                      "body": "broken thing", "arc_id": act.id}, author="ada")

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    assert {e.props.get("href") for e in parts(user, "checklist-body")} \
        == {f"/notes?id={b.id}"}


async def test_a_box_carries_a_cards_star_due_date_and_tags(user: User, seeded):
    """A box had no star, no due date and no tags, so a priority box looked
    like any other (the user, 2026-10-27)."""
    ctx, act = seeded
    one = next(n for n in store.load(ctx.store_dir) if n.target.name == "one")
    api.retag(ctx, one.id, add=("priority", "ux"), author="ada")
    api.supersede(ctx, one.id, author="ada", due="2030-01-01")

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    stars = sorted(str(b.props.get("color")) for b in parts(user, "note-star"))
    assert stars == ["None", "warning"]
    assert len(parts(user, "note-due")) == 1
    await see(user, "tag-ux")
    rows = [r for r in user.find(marker="checklist-row").elements]
    assert all("sb-resolvable" in r.classes for r in rows)
    starred = [r for r in rows if "sb-starred" in r.classes]
    assert [r.props["data-id"] for r in starred] == [one.id]


async def test_a_box_id_is_one_prop_not_a_script(user: User, seeded):
    """As a card's: `x :title=1` as an id added a `:`-prop, which nicegui.js
    runs as JavaScript (audit 2026-10-28)."""
    ctx, act = seeded
    one = next(n for n in store.load(ctx.store_dir) if n.target.name == "one")
    evil = f"{one.id} :title=1"
    p = ctx.store_dir / "notes.jsonl"
    p.write_text(p.read_text().replace(one.id, evil))

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    (row,) = [r for r in user.find(marker="checklist-row").elements if one.id in r._props["data-id"]]
    assert row._props["data-id"] == evil and ":title" not in row._props


def _order(user):
    return [c._props["data-id"] for c in cards(user)]


def _pick(user, box, *args):
    from _cards import _emit
    _emit(user, box, "move", *args)


async def test_checklist_reads_in_write_order(user: User, repo, tmp_path):
    """The list sorted by target name; the user chose write time (2026-11-05)."""
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.seed(ctx, act.id, "item", ["zeta"], author="ada")
    api.seed(ctx, act.id, "item", ["alpha"], author="ada")

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    names = [part(user, "note-headline", card=c).text for c in cards(user)]
    assert [n.removeprefix("item") for n in names] == ["zeta", "alpha"]


async def test_the_phase_menu_moves_a_box_like_arc_move(user: User, repo, tmp_path):
    twin = tmp_path / "twin"
    store.ensure_store(twin)
    out = {}
    for d, name in ((tmp_path, "gui"), (twin, "twin")):
        ctx = api.resolve(str(d))
        act = api.create_arc(ctx, "camp", "d", "item", author="ada")
        api.seed(ctx, act.id, "item", ["one", "two"], author="ada")
        api.set_phases(ctx, act.id, ["todo", "doing"], author="user")
        out[name] = ctx, act
    ctx, act = out["gui"]
    one = next(n for n in store.arc_items(store.load(ctx.store_dir), act.id) if n.target.name == "one")

    @ui.page("/t")
    def page():
        checklist(ctx, store.load_arcs(ctx.store_dir)[0], lambda: None, author="ada")

    await user.open("/t")
    assert len(parts(user, "checklist-phase")) == 2
    (box,) = [c for c in cards(user) if c._props["data-id"] == one.id]
    assert [m["t"] for m in part(user, "checklist-phase", card=box).d["menu"]][:2] \
        == ["todo", "doing"]
    _pick(user, box, "phase", "doing")

    tctx, tact = out["twin"]
    tone = next(n for n in store.arc_items(store.load(tctx.store_dir), tact.id) if n.target.name == "one")
    api.move_box(tctx, tone.id, phase="doing", author="ada")

    def placed(c, a):
        (g,) = [x for x in store.load_arcs(c.store_dir) if x.id == a.id]
        return [{k: v for k, v in p.items() if k not in ("at", "id")} for p in g.placement]
    assert placed(ctx, act) == placed(tctx, tact)
    assert placed(ctx, act)[0]["phase"] == "doing"


async def test_up_moves_a_box_on_an_arc_without_phases(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.seed(ctx, act.id, "item", ["zeta"], author="ada")
    api.seed(ctx, act.id, "item", ["alpha"], author="ada")

    @ui.page("/t")
    def page():
        checklist(ctx, act, lambda: None, author="ada")

    await user.open("/t")
    first, second = _order(user)
    assert [m["t"] for m in part(user, "checklist-order", card=cards(user)[0]).d["menu"]] == ["down"]
    _pick(user, cards(user)[1], "up", "up")
    (g,) = store.load_arcs(ctx.store_dir)
    assert [p["id"] for p in g.placement][0] == second


async def test_the_phases_editor_sets_phases_and_shows_a_refusal(user: User, seeded):
    from symbion.gui.pages import build_page
    ctx, act = seeded
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={act.id}")
    user.find(marker="arc-edit").click()
    user.find(marker="arc-phases").elements.pop().set_value("todo, next step, doing")
    user.find(marker="arc-phases-save").click()
    assert store.load_arcs(ctx.store_dir)[0].phases == ("todo", "next step", "doing")
    await see(user, "checklist-phase")
    user.find(marker="arc-phases").elements.pop().set_value("todo, Done")
    user.find(marker="arc-phases-save").click()
    await user.should_see("reserved")
    assert store.load_arcs(ctx.store_dir)[0].phases == ("todo", "next step", "doing")


async def test_a_read_only_serve_offers_no_move(user: User, seeded, monkeypatch):
    ctx, act = seeded
    api.set_phases(ctx, act.id, ["todo", "doing"], author="user")
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")

    @ui.page("/t")
    def page():
        checklist(ctx, store.load_arcs(ctx.store_dir)[0], lambda: None, author="ada")

    await user.open("/t")
    assert cards(user)
    await not_see(user, "checklist-phase")
    await not_see(user, "checklist-order")


# ---- the board ----
import asyncio                                                   # noqa: E402
from uuid import uuid4                                           # noqa: E402

from nicegui.testing.user_interaction import UserInteraction    # noqa: E402


def _page(user, event: str, args=None) -> None:
    """What chrome's _BOARD_JS sends the page: the user fixture runs no script,
    so a page it opens is a narrow window that never says `sbArcWide`."""
    UserInteraction(user, {user.client.layout}, None).trigger(event, args)


def _columns(user) -> dict:
    """{data-phase: [box ids]} for each board column, in page order."""
    cols = sorted(user.find(marker="board-column").elements, key=lambda e: e.id)
    return {c.props["data-phase"]: [d.props["data-id"] for d in c.descendants()
                                    if "note-row" in d._markers] for c in cols}


def _card(user, nid):
    (c,) = [c for c in cards(user) if c.props["data-id"] == nid]
    return c


def _arc(ctx, act):
    return next(a for a in store.load_arcs(ctx.store_dir) if a.id == act.id)


def _col(ctx, act, name) -> list:
    return [b.id for b in dict(store.arc_board(store.load(ctx.store_dir), _arc(ctx, act)))[name]]


def _head(ctx, nid):
    return next(n for n in store.heads(store.load(ctx.store_dir)) if n.id == nid)


@pytest.fixture
def phased(repo, tmp_path):
    from symbion.gui.pages import build_page
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.seed(ctx, act.id, "item", ["one", "two", "three"], author="ada")
    api.set_phases(ctx, act.id, ["todo", "doing"], author="user")
    ids = {n.target.name: n.id for n in store.arc_items(store.load(ctx.store_dir), act.id)}
    build_page(ctx, author="ada")
    return ctx, act, ids


def _arrows(user, card) -> dict:
    """A board card's arrows, {direction: (enabled, tip)}, in the order it draws them."""
    return {p.d["d"]: (p.d["ok"], p.props["tip"])
            for d in ("left", "up", "down", "right") for p in parts(user, f"board-{d}", card=card)}


async def _board(user, act, *, click=True):
    """The arc's page on its board: by the toggle, or by the tab's choice once
    the page script says the window is wide."""
    await user.open(f"/arc?id={act.id}")
    if click:
        user.find(marker="arc-view-board").click()
    else:
        _page(user, "sbArcWide")
    await user.should_see(marker="board-column")


async def test_the_toggle_remembers_board_per_tab(user: User, phased):
    ctx, act, ids = phased
    other = api.create_arc(ctx, "other", "d", "item", author="ada")
    api.seed(ctx, other.id, "item", ["four"], author="ada")
    await user.open(f"/arc?id={act.id}")
    await user.should_see(marker="checklist-row")
    await user.should_not_see(marker="board-column")
    # one outlined group, so the word not picked is a button, not a label
    (group,) = user.find(marker="arc-view").elements
    assert "sb-seg" in group.classes
    user.find(marker="arc-view-board").click()
    await user.should_see(marker="board-column")

    await user.open(f"/arc?id={act.id}")
    await asyncio.sleep(0.1)
    await user.should_not_see(marker="board-column")         # narrow: List whatever the tab chose
    await _board(user, act, click=False)                       # wide: the tab's Board

    await user.open(f"/arc?id={other.id}")                     # one key per arc
    _page(user, "sbArcWide")
    await asyncio.sleep(0.1)
    await user.should_not_see(marker="board-column")

    user.tab_id = str(uuid4())                                 # another tab
    await user.open(f"/arc?id={act.id}")
    _page(user, "sbArcWide")
    await asyncio.sleep(0.1)
    await user.should_not_see(marker="board-column")
    user.find(marker="arc-view-board").click()
    await user.should_see(marker="board-column")
    user.find(marker="arc-view-list").click()                  # List is kept too
    await user.open(f"/arc?id={act.id}")
    _page(user, "sbArcWide")
    await asyncio.sleep(0.1)
    await user.should_not_see(marker="board-column")


async def test_board_columns_follow_arc_board(user: User, phased):
    ctx, act, ids = phased
    api.move_box(ctx, ids["three"], phase="doing", author="ada")
    api.supersede(ctx, ids["one"], author="ada", status="resolved")
    await _board(user, act)
    want = {n or "open": [b.id for b in rows]
            for n, rows in store.arc_board(store.load(ctx.store_dir), _arc(ctx, act))}
    assert _columns(user) == want == {"todo": [ids["two"]], "doing": [ids["three"]],
                                      "done": [ids["one"]]}
    await not_see(user, "checklist-phase")          # the column names it; the list keeps it
    api.set_phases(ctx, act.id, [], author="user")
    await _board(user, act, click=False)
    assert _columns(user) == {"open": [ids["two"], ids["three"]], "done": [ids["one"]]}
    await not_see(user, "checklist-order")


async def test_a_box_foot_names_its_kind_then_its_phase(user: User, phased):
    """A List row's foot runs kind, then status: the phase chip led it (the
    design system's alignment, 2026-11-07). A board card has no chip: its
    arrows move it."""
    ctx, act, ids = phased
    api.add(ctx, {"kind": "question", "target": {"type": "item", "name": "four"},
                  "body": "ship it?", "arc_id": act.id}, author="ada")     # mixed: kinds show
    await user.open(f"/arc?id={act.id}")
    foot = [p["m"] for p in _card(user, ids["two"]).props["card"]["foot"]]
    assert foot.index("checklist-kind") + 1 == foot.index("checklist-phase"), foot
    await _board(user, act)
    foot = [p["m"] for p in _card(user, ids["two"]).props["card"]["foot"]]
    assert "checklist-kind" in foot and "checklist-phase" not in foot, foot
    assert _arrows(user, _card(user, ids["two"]))


async def test_a_card_shows_its_mover_and_an_unmoved_one_nothing(user: User, phased,
                                                                   monkeypatch):
    """`not moved` on every card said nothing (the user, 2026-11-07). `one`
    gains an order-only placement, which names no mover. On a phased board
    the column names where a box is: its line says where it came from, and
    its tip the whole move."""
    ctx, act, ids = phased
    api.move_box(ctx, ids["three"], before=ids["two"], author="human")
    api.move_box(ctx, ids["two"], phase="doing", author="ada")
    assert ids["one"] in {p["id"] for p in _arc(ctx, act).placement}

    def moved():
        return {c.props["data-id"]: [(p.text, p.props["tip"].split("\n")[0])
                                     for p in parts(user, "board-moved", card=c)]
                for c in cards(user)}

    await _board(user, act)
    assert moved() == {ids["one"]: [], ids["two"]: [("from todo · now", "todo → doing · ada · now")],
                       ids["three"]: [("moved · now", "moved · human · now")]}
    assert all(c.props.get("draggable") == "true" for c in cards(user))
    # The demo moves nothing: no arrows, and a moved box still names its mover.
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await _board(user, act, click=False)
    assert [_arrows(user, c) for c in cards(user)] == [{}, {}, {}]
    assert [c.props["card"]["move"] is None for c in cards(user)] == [True, False, False]
    monkeypatch.delenv("SYMBION_READ_ONLY")
    # Without phases the line has room for the mover.
    api.set_phases(ctx, act.id, [], author="user")
    api.move_box(ctx, ids["one"], before=ids["three"], author="human")
    await _board(user, act, click=False)
    assert part(user, "board-moved", card=_card(user, ids["one"])).text == "moved · human · now"
    # A done box's foot names who ticked it, not its placement.
    api.supersede(ctx, ids["one"], author="ada", status="resolved")
    await _board(user, act, click=False)
    assert _card(user, ids["one"]).props["card"]["move"] is None


async def test_a_board_card_moves_without_a_drag_as_arc_move_does(user: User, phased):
    """Its arrows: a keyboard, or a finger, which never drags, moves a box on
    the board. The `order` chip read oddly as a control (the user,
    2026-11-07). An arrow that cannot move is drawn disabled, so each keeps
    its place card to card."""
    ctx, act, ids = phased
    await _board(user, act)
    assert _arrows(user, _card(user, ids["two"])) == {
        "left": (False, "the first phase"), "up": (True, "move up"),
        "down": (True, "move down"), "right": (True, "move to doing")}
    part(user, "board-right", card=_card(user, ids["two"])).click()
    await asyncio.sleep(0.1)
    gui = [{k: v for k, v in p.items() if k != "at"} for p in _arc(ctx, act).placement]
    assert gui == [{"id": ids["two"], "phase": "doing", "by": "ada", "from": "todo"}]
    assert _arrows(user, _card(user, ids["two"]))["right"] == (
        False, "the last phase: resolve the box to finish it")
    store.undo(ctx.store_dir, act.id, lambda *a: True)
    api.move_box(ctx, ids["two"], phase="doing", author="ada")
    assert [{k: v for k, v in p.items() if k != "at"} for p in _arc(ctx, act).placement] == gui


async def test_a_move_pick_hands_focus_back_to_the_moved_card(user: User, phased, monkeypatch):
    """A pick redraws the arc, and keyboard focus fell to the page's top: the
    move menu is the keyboard's way to move a box, so each pick lost its place."""
    from nicegui.outbox import Outbox
    ctx, act, ids = phased
    sent, emit = [], Outbox._emit

    async def record(self, message):
        if message[1] == "run_javascript" and "sbFocusMove" in message[2]["code"]:
            sent.append(message[2]["code"])
        await emit(self, message)

    monkeypatch.setattr(Outbox, "_emit", record)
    await _board(user, act)
    _pick(user, _card(user, ids["two"]), "phase", "doing")   # the board: its arrow
    await asyncio.sleep(0.1)
    _pick(user, _card(user, ids["two"]), "phase", "todo")
    await asyncio.sleep(0.1)
    await user.open(f"/arc?id={act.id}")                     # the List: its phase chip
    user.find(marker="arc-view-list").click()
    await user.should_see(marker="arc-list")
    _pick(user, _card(user, ids["one"]), "down", "down")
    await asyncio.sleep(0.1)
    rest = '".sb-move .q-btn:not(.disabled)")'
    assert sent == [f'sbFocusMove("{ids["two"]}", ".sb-mv-right:not(.disabled)", {rest}',
                    f'sbFocusMove("{ids["two"]}", ".sb-mv-left:not(.disabled)", {rest}',
                    f'sbFocusMove("{ids["one"]}")']
    # The tick and the star redrew it as well (the GUI review, 2026-11-07).
    from _cards import _emit
    sent.clear()
    _emit(user, _card(user, ids["three"]), "resolve")
    await asyncio.sleep(0.1)
    _emit(user, _card(user, ids["two"]), "star")
    await asyncio.sleep(0.1)
    assert sent == [f'sbFocusMove("{ids["three"]}", ".sb-resolve")',
                    f'sbFocusMove("{ids["two"]}", ".sb-star")']


async def test_a_board_card_on_an_arc_without_phases_moves_up_and_down(user: User, phased):
    ctx, act, ids = phased
    api.set_phases(ctx, act.id, [], author="user")
    await _board(user, act)
    (board,) = user.find(marker="arc-board").elements
    assert "sb-phases" in board.classes            # the narrow card's rules reach it
    arrows = {c.props["data-id"]: {d: ok for d, (ok, _) in _arrows(user, c).items()}
              for c in cards(user)}
    assert arrows == {ids["one"]: {"up": False, "down": True}, ids["two"]: {"up": True, "down": True},
                      ids["three"]: {"up": True, "down": False}}
    part(user, "board-up", card=_card(user, ids["three"])).click()
    await asyncio.sleep(0.1)
    assert _col(ctx, act, None) == [ids["one"], ids["three"], ids["two"]]


async def test_a_move_event_writes_the_placement(user: User, phased):
    """...and redraws the moved card alone, where the browser has put it
    already, so its foot names its mover: a redrawn board flickered."""
    ctx, act, ids = phased
    await _board(user, act)
    drawn = {e.id for e in user.find(marker="board-column").elements}
    _page(user, "sbMove", {"id": ids["two"], "phase": "doing", "before": None})
    await asyncio.sleep(0.1)
    (p,) = _arc(ctx, act).placement
    assert (p["id"], p["phase"], p["by"]) == (ids["two"], "doing", "ada")
    assert _columns(user)["doing"] == [ids["two"]]
    assert part(user, "board-moved", card=_card(user, ids["two"])).text == "from todo · now"
    _page(user, "sbMove", {"id": ids["three"], "phase": "todo", "before": ids["one"]})
    await asyncio.sleep(0.1)
    assert _col(ctx, act, "todo") == [ids["three"], ids["one"]]
    _page(user, "sbMove", {"id": ids["one"], "phase": "doing", "before": None,
                           "after": ids["two"]})
    await asyncio.sleep(0.1)
    assert _col(ctx, act, "doing") == [ids["two"], ids["one"]]
    assert _columns(user) == {"todo": [ids["three"]], "doing": [ids["two"], ids["one"]],
                              "done": []}
    assert {e.id for e in user.find(marker="board-column").elements} == drawn


async def test_a_drop_redraws_the_menus_of_the_columns_it_touched(user: User, phased):
    """Each card's up and down read the column it was drawn in. With only the
    moved card redrawn, a neighbour's `down` moved it up."""
    ctx, act, ids = phased

    def menus(col):
        return [[d for d, (ok, _) in _arrows(user, _card(user, i)).items()
                 if ok and d in ("up", "down")] for i in _columns(user)[col]]

    await _board(user, act)
    _page(user, "sbMove", {"id": ids["three"], "phase": "todo", "before": ids["one"]})
    await asyncio.sleep(0.1)
    assert _columns(user)["todo"] == [ids["three"], ids["one"], ids["two"]]
    assert menus("todo") == [["down"], ["up", "down"], ["up"]]
    _page(user, "sbMove", {"id": ids["three"], "phase": "doing", "before": None})   # leaves todo
    await asyncio.sleep(0.1)
    assert menus("todo") == [["down"], ["up"]] and menus("doing") == [[]]
    _pick(user, _card(user, ids["one"]), "down", "down")
    await asyncio.sleep(0.1)
    assert _col(ctx, act, "todo") == [ids["two"], ids["one"]]


async def test_a_drop_in_the_open_column_of_an_arc_without_phases_orders_it(user: User,
                                                                           phased):
    """The column reads `open`, a name the arc does not have: the move gives no phase."""
    ctx, act, ids = phased
    api.set_phases(ctx, act.id, [], author="user")
    await _board(user, act)
    _page(user, "sbMove", {"id": ids["three"], "phase": "open", "before": ids["one"]})
    await asyncio.sleep(0.1)
    assert _col(ctx, act, None) == [ids["three"], ids["one"], ids["two"]]
    assert _columns(user)["open"] == [ids["three"], ids["one"], ids["two"]]


async def test_a_drop_redraws_the_board_card_alone_and_leaves_the_peeks(user: User, phased):
    """The peek draws the entry as a card with the same id: the redraw after a
    drop took it too, and the peek's keys then found no buttons."""
    from symbion.gui.notes import Card
    ctx, act, ids = phased
    await _board(user, act)
    _page(user, "sbPeek", {"id": ids["two"]})
    await asyncio.sleep(0.1)

    def drawn():
        return [e for e in user.client.elements.values()
                if isinstance(e, Card) and e._props.get("data-id") == ids["two"]]
    assert len(drawn()) == 2
    _page(user, "sbMove", {"id": ids["two"], "phase": "doing", "before": None})
    await asyncio.sleep(0.1)
    assert len(drawn()) == 2
    assert _columns(user)["doing"] == [ids["two"]]


async def test_a_drop_after_the_last_shown_card_of_a_capped_column_lands_there(
        user: User, repo, tmp_path, monkeypatch):
    """A capped column ends short of the store's: `after` its last shown card
    puts the box there, not past the boxes the page left out."""
    from symbion.gui.pages import build_page
    monkeypatch.setitem(build_page.__globals__, "PAGE_ROWS", 2)
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    for name in ("n0", "n1", "n2", "n3", "x"):
        api.seed(ctx, act.id, "item", [name], author="ada")
    api.set_phases(ctx, act.id, ["todo", "doing"], author="user")
    ids = {n.target.name: n.id for n in store.arc_items(store.load(ctx.store_dir), act.id)}
    api.move_box(ctx, ids["x"], phase="doing", author="ada")
    build_page(ctx, author="ada")
    await _board(user, act)
    assert _columns(user)["todo"] == [ids["n0"], ids["n1"]]
    _page(user, "sbMove", {"id": ids["x"], "phase": "todo", "before": None, "after": ids["n1"]})
    await asyncio.sleep(0.1)
    assert _col(ctx, act, "todo") == [ids[n] for n in ("n0", "n1", "x", "n2", "n3")]
    assert _columns(user)["todo"] == [ids["n0"], ids["n1"], ids["x"]]


def _bar(bar) -> list:
    """An arc's progress bar as [(phase, count, its place on the board)], left to right."""
    return [(s.props["data-phase"], int(s._style["flex"].split()[0]), int(s._style["--i"]))
            for s in bar.default_slot.children]


async def test_a_drop_on_done_moves_the_header_count(user: User, phased):
    """The board is not redrawn after a drop, so the count and bar over it
    are updated on their own."""
    ctx, act, ids = phased
    api.supersede(ctx, ids["one"], author="ada", status="resolved")
    await _board(user, act)
    (count,) = user.find(marker="arc-count").elements
    (bar,) = user.find(marker="arc-progress").elements
    assert (count.text, bar.props["aria-valuenow"]) == ("1/3", 1)
    _page(user, "sbMove", {"id": ids["two"], "phase": "done", "before": None})
    await asyncio.sleep(0.1)
    assert (count.text, bar.props["aria-valuenow"]) == ("2/3", 2)
    assert _bar(bar) == [("done", 2, 3), ("todo", 1, 1)]
    _page(user, "sbMove", {"id": ids["one"], "phase": "doing", "before": None})
    await asyncio.sleep(0.1)
    assert (count.text, bar.props["aria-valuenow"]) == ("1/3", 1)
    assert _bar(bar) == [("done", 1, 3), ("doing", 1, 2), ("todo", 1, 1)]


async def test_an_arcs_bar_is_its_boxes_by_phase(user: User, phased):
    """A progress bar said only done or not (2026-11-08): it is the arc's boxes
    by phase, done first, then the board's columns right to left, each as
    wide as its count, its tip the phase and count, and a screen reader hears
    the whole. The arcs page draws the same bar, with the arc's own count of
    columns, and an arc without phases has open and done."""
    ctx, act, ids = phased
    api.move_box(ctx, ids["two"], author="ada", phase="doing")
    api.supersede(ctx, ids["three"], author="ada", status="resolved")
    plain = api.create_arc(ctx, "plain", "d", "item", author="ada")
    api.seed(ctx, plain.id, "item", ["four", "five"], author="ada")
    await _board(user, act)
    (bar,) = user.find(marker="arc-progress").elements
    assert _bar(bar) == [("done", 1, 3), ("doing", 1, 2), ("todo", 1, 1)]
    assert [s.props["data-tip"] for s in bar.default_slot.children] == ["done 1", "doing 1", "todo 1"]
    assert (bar.props["role"], bar.props["aria-valuemax"], bar._style["--sb-cols"]) == \
        ("progressbar", 3, "3")
    assert bar.props["aria-valuetext"] == "1 of 3 done; doing 1, todo 1"
    await user.open("/arcs")
    bars = {b.parent_slot.parent.default_slot.children[0].default_slot.children[0].text: b
            for b in user.find(marker="arc-bar").elements}
    assert _bar(bars["camp"]) == _bar(bar) and bars["camp"]._style["--sb-cols"] == "3"
    assert _bar(bars["plain"]) == [("open", 2, 1)] and bars["plain"]._style["--sb-cols"] == "2"



async def test_a_drop_on_done_resolves(user: User, phased):
    ctx, act, ids = phased
    await _board(user, act)
    _page(user, "sbMove", {"id": ids["one"], "phase": "done", "before": None})
    await asyncio.sleep(0.1)
    one = _head(ctx, ids["one"])
    assert (one.status, one.author) == ("resolved", "ada")
    assert _columns(user)["done"] == [ids["one"]]
    assert _card(user, ids["one"]).props["card"]["resolve"] == "reopen"


async def test_a_verdict_box_dropped_on_done_opens_the_dialog(user: User, repo, tmp_path):
    """As the checklist's tick: a pre-registration closes with its result, so
    the drop opens the dialog, and the box stays open, back in its column,
    until the dialog saves."""
    from symbion.gui.pages import build_page
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(PREREG)
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "item", "name": "sweep"},
                      "arc_id": act.id}, author="ada")
    build_page(ctx, author="ada")
    await _board(user, act)
    _page(user, "sbMove", {"id": p.id, "phase": "done", "before": None})
    await user.should_see(marker="edit-result")
    assert store.arc_progress(store.load(tmp_path), act.id) == (0, 1)
    assert _columns(user)["open"] == [p.id]
    user.find(marker="edit-result").elements.pop().set_value("FIRED")
    user.find(marker="edit-save").click()
    assert store.arc_progress(store.load(tmp_path), act.id) == (1, 1)


async def test_a_drop_out_of_done_reopens_and_places_in_one_write(user: User, phased):
    """One write, so one undo takes back the reopen and the placement both."""
    ctx, act, ids = phased
    api.supersede(ctx, ids["two"], author="ada", status="resolved")
    before = store.load_arcs(ctx.store_dir)
    await _board(user, act)
    _page(user, "sbMove", {"id": ids["two"], "phase": "doing", "before": None})
    await asyncio.sleep(0.1)
    assert _head(ctx, ids["two"]).status == "open"
    assert _col(ctx, act, "doing") == [ids["two"]]
    assert _columns(user)["doing"] == [ids["two"]]
    assert _card(user, ids["two"]).props["card"]["resolve"] == "resolve"
    store.undo(ctx.store_dir, None, lambda *a: True)
    assert _head(ctx, ids["two"]).status == "resolved"
    assert store.load_arcs(ctx.store_dir) == before


async def test_a_stale_drop_redraws_and_says_why(user: User, phased):
    """An agent moves the box the card is dropped before, after the page drew:
    the store refuses, the notice says why, and the board redraws as the
    store has it."""
    ctx, act, ids = phased
    await _board(user, act)
    assert _columns(user)["todo"] == [ids["one"], ids["two"], ids["three"]]
    store.move_box(ctx.store_dir, ids["two"], phase="doing", author="claude")
    _page(user, "sbMove", {"id": ids["three"], "phase": "todo", "before": ids["two"]})
    await user.should_see("not an open box")
    await asyncio.sleep(0.1)
    assert _columns(user) == {"todo": [ids["one"], ids["three"]], "doing": [ids["two"]],
                              "done": []}
    assert ids["three"] not in {p["id"] for p in _arc(ctx, act).placement}


async def test_a_column_caps_at_the_row_limit(user: User, repo, tmp_path, monkeypatch):
    from symbion.gui.pages import build_page
    monkeypatch.setitem(build_page.__globals__, "PAGE_ROWS", 2)
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.seed(ctx, act.id, "item", [f"n{i}" for i in range(7)], author="ada")
    for n in store.arc_items(store.load(ctx.store_dir), act.id)[:3]:
        api.supersede(ctx, n.id, author="ada", status="resolved")
    build_page(ctx, author="ada")
    await _board(user, act)
    assert {k: len(v) for k, v in _columns(user).items()} == {"open": 2, "done": 2}
    # A column is a board: its count is of every box in it, not the shown.
    counts = sorted(user.find(marker="board-count").elements, key=lambda e: e.id)
    assert [c.text for c in counts] == ["4", "3"]
    # A cut says so as every page does.
    more = sorted(user.find(marker="board-more").elements, key=lambda e: e.id)
    assert [m.text for m in more] == ["+2 more entries not shown — show all",
                                      "+1 more entry not shown — show all"]
    assert more[0].props["href"] == f"/arc?id={act.id}&all=1"
    await user.open(more[0].props["href"])
    _page(user, "sbArcWide")
    await user.should_see(marker="board-column")
    assert {k: len(v) for k, v in _columns(user).items()} == {"open": 4, "done": 3}


async def test_the_board_names_parked_entries(user: User, phased):
    ctx, act, ids = phased
    await _board(user, act)
    await user.should_not_see(marker="board-parked")
    api.add(ctx, {"kind": "idea", "target": {"type": "item", "name": "one"},
                  "body": "a side thought", "arc_id": act.id}, author="ada")
    await _board(user, act, click=False)
    (link,) = user.find(marker="board-parked").elements
    assert (link.text, link.props["href"]) == ("1 parked", f"/notes?arc={act.id}&kind=idea")


async def test_a_read_only_board_drags_and_its_drop_writes_nothing(user: User, phased,
                                                                   monkeypatch):
    """The demo drags, so a visitor sees that a box moves; the drop says so,
    writes nothing, and redraws the board, which puts the card back."""
    ctx, act, ids = phased
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await _board(user, act)
    assert cards(user) and all(c.props.get("draggable") == "true" for c in cards(user))
    assert all(_arrows(user, c) == {} for c in cards(user))
    drawn = _card(user, ids["one"]).id
    _page(user, "sbMove", {"id": ids["one"], "phase": "doing", "before": None})
    await user.should_see("Read-only demo")
    await asyncio.sleep(0.1)
    assert _arc(ctx, act).placement is None
    assert _card(user, ids["one"]).id != drawn, "the card goes back"
    assert _columns(user)["todo"] == [ids["one"], ids["two"], ids["three"]]


# ---- the list's drag ----
def _grips(user) -> dict:
    """{box id: its data-phase} for each list row with a grip, in page order."""
    return {c.props["data-id"]: c.props.get("data-phase") for c in cards(user)
            if c.props["card"].get("grip")}


async def test_an_open_list_row_has_a_grip_and_its_phase(user: User, phased):
    """The List drags an open row by its grip (the phases design); chrome's
    _BOARD_JS drops it beside an open row, in that row's phase, read from
    `data-phase`. A done row has neither; a board card drags whole."""
    ctx, act, ids = phased
    api.move_box(ctx, ids["three"], phase="doing", author="ada")
    api.supersede(ctx, ids["one"], author="ada", status="resolved")
    await user.open(f"/arc?id={act.id}")
    await user.should_see(marker="arc-list")
    assert _grips(user) == {ids["two"]: "todo", ids["three"]: "doing"}
    assert "data-phase" not in _card(user, ids["one"]).props
    assert not any("draggable" in c.props for c in cards(user)), "the grip drags, not the row"
    api.set_phases(ctx, act.id, [], author="user")
    await user.open(f"/arc?id={act.id}")
    assert _grips(user) == {ids["two"]: "open", ids["three"]: "open"}
    await _board(user, act)
    assert not _grips(user)


async def test_a_list_drop_reorders_and_moves_across_phases(user: User, phased):
    """What _BOARD_JS sends for a List drop: `before` or `after` the row it
    lands beside, in that row's phase. The list is drawn again as the store has it."""
    ctx, act, ids = phased
    await user.open(f"/arc?id={act.id}")
    await user.should_see(marker="arc-list")
    _page(user, "sbMove", {"id": ids["one"], "phase": "todo", "before": None,
                           "after": ids["three"]})
    await asyncio.sleep(0.1)
    assert _col(ctx, act, "todo") == [ids["two"], ids["three"], ids["one"]]
    assert _order(user) == [ids["two"], ids["three"], ids["one"]]
    api.move_box(ctx, ids["three"], phase="doing", author="ada")
    await user.open(f"/arc?id={act.id}")
    _page(user, "sbMove", {"id": ids["two"], "phase": "doing", "before": ids["three"],
                           "after": None})
    await asyncio.sleep(0.1)
    assert (_col(ctx, act, "todo"), _col(ctx, act, "doing")) == \
        ([ids["one"]], [ids["two"], ids["three"]])
    assert _grips(user) == {ids["one"]: "todo", ids["two"]: "doing", ids["three"]: "doing"}


async def test_a_list_drop_on_an_arc_without_phases_stores_a_null_phase(user: User, phased):
    ctx, act, ids = phased
    api.set_phases(ctx, act.id, [], author="user")
    await user.open(f"/arc?id={act.id}")
    await user.should_see(marker="arc-list")
    _page(user, "sbMove", {"id": ids["three"], "phase": "open", "before": ids["one"],
                           "after": None})
    await asyncio.sleep(0.1)
    assert _order(user) == [ids["three"], ids["one"], ids["two"]]
    assert {p["phase"] for p in _arc(ctx, act).placement} == {None}


async def test_a_read_only_list_has_grips_and_its_drop_writes_nothing(user: User, phased,
                                                                      monkeypatch):
    ctx, act, ids = phased
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open(f"/arc?id={act.id}")
    await user.should_see(marker="arc-list")
    assert _grips(user) == {ids["one"]: "todo", ids["two"]: "todo", ids["three"]: "todo"}
    drawn = _card(user, ids["one"]).id
    _page(user, "sbMove", {"id": ids["one"], "phase": "todo", "before": None,
                           "after": ids["three"]})
    await user.should_see("Read-only demo")
    await asyncio.sleep(0.1)
    assert _arc(ctx, act).placement is None
    assert _card(user, ids["one"]).id != drawn, "the row goes back"
    assert _order(user) == [ids["one"], ids["two"], ids["three"]]


async def test_arcs_sorts_by_last_change_and_says_it(user: User, repo, tmp_path, monkeypatch):
    """To tell a moving arc from a quiet one at a glance (2026-11-08): Arcs puts
    the most recently changed first, and each card says its age and its last
    change, each with its full stamp as the tip. An arc changed only by its
    creation says so once. The arc page says the same, and a drop updates it."""
    from symbion.gui.pages import build_page
    store.ensure_store(tmp_path)
    ctx = api.resolve(str(tmp_path))
    at = lambda hh: monkeypatch.setenv("SYMBION_NOW", f"2026-01-10T{hh}:00:00+00:00")
    at("01"); old = api.create_arc(ctx, "old", "d", "item", author="ada")
    at("02"); api.create_arc(ctx, "new", "d", "item", author="ada")
    at("03"); box = api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "x"},
                                  "arc_id": old.id}, author="bo")
    at("05")
    build_page(ctx, author="ada")
    await user.open("/arcs")
    cards_ = sorted(user.find(marker="arc-card").elements, key=lambda e: e.id)
    lines = sorted(user.find(marker="arc-when").elements, key=lambda e: e.id)
    texts = [" ".join(c.text for c in w.default_slot.children) for w in lines]
    assert [c.default_slot.children[0].default_slot.children[0].text for c in cards_] == ["old", "new"]
    assert texts == ["started 4h ago by ada · last change 2h ago by bo", "started 3h ago by ada"]
    (change,) = [e for e in user.find(marker="arc-last-change").elements]
    assert change.props["data-tip"] == store.shown("2026-01-10T03:00:00+00:00")
    api.set_phases(ctx, old.id, ["todo", "doing"], author="user")
    await _board(user, old)
    at("06")
    _page(user, "sbMove", {"id": box.id, "phase": "doing", "before": None})
    await asyncio.sleep(0.1)
    (head,) = user.find(marker="arc-when").elements
    assert " ".join(c.text for c in head.default_slot.children) == \
        "started 5h ago by ada · last change just now by ada"


async def test_a_box_id_reaches_the_page_script_as_a_string(user: User, phased, monkeypatch):
    """An id is taken as written, so a pushed row's could close the string
    around it in sbFocusMove and run script in the viewer's page (audit,
    2026-11-08). A star, a move and a tick each send it as JSON."""
    import json
    from nicegui import Client
    ctx, act, ids = phased
    evil = 'x");window.pwned=1;("'
    p = store.notes_path(ctx.store_dir)
    text = p.read_text(encoding="utf-8")
    assert text.count(json.dumps(ids["one"])) == 1
    p.write_text(text.replace(json.dumps(ids["one"]), json.dumps(evil)), encoding="utf-8")
    sent, real = [], Client.run_javascript
    monkeypatch.setattr(Client, "run_javascript",
                        lambda self, code, **kw: sent.append(code) or real(self, code, **kw))
    await _board(user, act)
    for press in ("note-star", "board-right", "note-resolve"):
        part(user, press, card=_card(user, evil)).click()
        await asyncio.sleep(0.2)
    focus = [s for s in sent if s.startswith("sbFocusMove(")]
    assert len(focus) == 3 and all(s.startswith(f"sbFocusMove({json.dumps(evil)}") for s in focus)


async def test_a_drop_counts_the_columns_it_redraws_again(user: User, phased):
    """A drop redrew the cards of the two columns it touched and left their
    counts as the page drew them: `todo` read 3 over 2 cards (audit, 2026-11-08)."""
    ctx, act, ids = phased
    await _board(user, act)
    _page(user, "sbMove", {"id": ids["one"], "phase": "doing", "before": None})
    await asyncio.sleep(0.2)
    counts = {c.props["data-phase"]: e.text for c in user.find(marker="board-column").elements
              for e in c.descendants() if "board-count" in e._markers}
    assert (counts["todo"], counts["doing"]) == ("2", "1"), counts
