from __future__ import annotations

import pytest

pytest.importorskip("nicegui")

from nicegui import ui                    # noqa: E402
from nicegui.testing import User          # noqa: E402

from symbion import api, store            # noqa: E402
from symbion.gui.arcs import checklist   # noqa: E402

pytestmark = pytest.mark.usefixtures("tmp_store")


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
        checklist(ctx, act.id, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("one")
    await user.should_see("two")


async def test_ticking_a_row_resolves_its_task(user: User, seeded):
    ctx, act = seeded

    @ui.page("/t")
    def page():
        checklist(ctx, act.id, lambda: None, author="ada")

    item = store.arc_items(store.load(ctx.store_dir), act.id)[0]
    await user.open("/t")
    user.find(marker=f"toggle-{item.id}").click()
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
        checklist(ctx, act.id, lambda: None, author="ada")

    # The tick supersedes, so the live row carries a NEW id -- find the head.
    tip = next(n for n in store.arc_items(store.load(ctx.store_dir), act.id)
               if n.target.name == item.target.name)
    await user.open("/t")
    user.find(marker=f"toggle-{tip.id}").click()
    await user.should_see("one")

    assert store.arc_progress(store.load(ctx.store_dir), act.id)[0] == 0


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


async def test_a_project_row_on_the_checklist_is_clickable(user: User, seeded):
    """The bug: a project-target
    task filed under an arc rendered on the arc page with nothing
    to click. The checklist row is only ever `target_link`, and `project` has no
    object page -- so this is the one target type for which the row's link had
    to be the note itself. Both directions: the seeded item row still points at
    the object view."""
    ctx, act = seeded
    g = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "no object page for me", "arc_id": act.id},
                author="ada")

    @ui.page("/t")
    def page():
        checklist(ctx, act.id, lambda: None, author="ada")

    await user.open("/t")
    hrefs = {e.props.get("href") for e in user.find(marker="note-target").elements}
    assert f"/notes?id={g.id}" in hrefs, "the project row must link to the note"
    assert "/object?type=item&name=one" in hrefs, "the item row still links to its object"


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
        checklist(ctx, act.id, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker=f"toggle-{p.id}").click()
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
    user.find(marker="arc-seed").click()
    await user.should_see("seeded 2 new target(s)")
    assert calls(s) == before
    assert sorted(n.target.name for n in store.load(s) if n.arc_id == act.id) \
        == ["40.40", "44.00"]
