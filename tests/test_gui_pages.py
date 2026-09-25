from __future__ import annotations

import pytest

pytest.importorskip("nicegui")

from nicegui.testing import User        # noqa: E402

from symbion import api                 # noqa: E402
from symbion.gui.pages import build_page  # noqa: E402


@pytest.fixture
def ctx_with_notes(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "alpha body", "tags": ["red"]}, author="ada")
    api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "widget"},
                  "body": "beta body", "tags": ["blue"]}, author="sam")
    build_page(ctx, author="ada")
    return ctx


async def test_notes_filters_by_tag(user: User, ctx_with_notes):
    await user.open("/notes?tag=red")
    await user.should_see("alpha body")
    await user.should_not_see("beta body")


async def test_notes_with_no_filters_shows_everything(user: User, ctx_with_notes):
    await user.open("/notes")
    await user.should_see("alpha body")
    await user.should_see("beta body")


async def test_a_stale_bookmark_renders_rather_than_erroring(user: User, ctx_with_notes):
    await user.open("/notes?tag=nonexistent&utm_source=x")
    await user.should_see("0 notes")


async def test_an_unknown_param_does_not_silently_widen_the_result(
        user: User, ctx_with_notes):
    """The other direction of the dropped-param rule. Dropping utm_source must
    not also drop the tag beside it -- that would render every note and read
    as success."""
    await user.open("/notes?tag=red&utm_source=x")
    await user.should_see("1 notes")
    await user.should_not_see("beta body")


async def test_tab_title_leads_with_the_project_name(user: User, ctx_with_notes):
    """Several stores open in several tabs; the tab strip truncates from the
    right, so the project name goes first and 'symbion' is the suffix."""
    await user.open("/")
    assert user.client.resolve_title() == \
        f"{ctx_with_notes.cfg.project_root.name} · symbion"


async def test_header_brand_names_the_project(user: User, ctx_with_notes):
    """Several stores can be open at once; a bare 'symbion' in the corner
    says nothing about which one this is."""
    await user.open("/")
    await user.should_see(f"{ctx_with_notes.cfg.project_root.name} · symbion")


async def test_tags_page_lists_the_vocabulary_with_counts(user: User, ctx_with_notes):
    await user.open("/tags")
    await user.should_see("red")
    await user.should_see("blue")


async def test_object_view_surfaces_notes_that_REF_the_target(user: User, repo, tmp_path):
    """heads_for carries a reverse edge query() cannot express -- that is why
    /object exists rather than being another canned filter."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "other"},
                  "body": "points at widget", "refs": [{"type": "item", "name": "widget"}]},
            author="ada")
    build_page(ctx, author="ada")

    await user.open("/object?type=item&name=widget")
    await user.should_see("points at widget")


async def test_object_view_handles_a_name_with_a_slash(user: User, repo, tmp_path):
    """render_note builds these hrefs for every file/item target, so the
    round trip through the query string is the common case, not an edge one."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "src/x.py"},
                  "body": "slashed target"}, author="ada")
    build_page(ctx, author="ada")

    await user.open("/object?type=item&name=src%2Fx.py")
    await user.should_see("slashed target")


from symbion import store   # noqa: E402


async def test_home_renders_one_board_per_visible_status_kind_and_per_verdict_kind(
        user: User, repo, tmp_path):
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nanomaly = { status = true }\nshelf = { status = true, parked = true }\n'
        'audit = { verdict = true }\nfact = {}\n')
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "anomaly", "target": {"type": "item", "name": "a"},
                  "body": "anomaly body"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    await user.should_see("open anomaly (1)")
    await user.should_see("recent audit (0)")
    await user.should_see("priority (0)")
    await user.should_not_see("open bug")
    await user.should_not_see("open shelf")
    await user.should_not_see("open fact")


async def test_arc_page_hides_the_seed_button_without_an_eligible_task(user: User, repo, tmp_path):
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nfollowup = { status = true }\n[catalogs]\nthing = "echo a"\n')
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "thing", author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={act.id}")
    await user.should_see("camp")
    with pytest.raises(AssertionError):
        user.find(marker="arc-seed")


async def test_arc_page_renders_the_seed_button_on_a_default_store(user: User, repo, tmp_path):
    """The other direction: a store with no [kinds] table at all (defaults
    apply, so `task` is declared and eligible) and a scope that IS in
    seed_all_scopes must show the button. Without this, the hidden-direction
    test above could pass even if the button never rendered at all."""
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text('[catalogs]\nthing = "echo a"\n')
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "thing", author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={act.id}")
    await user.should_see("camp")
    assert user.find(marker="arc-seed").elements


async def test_the_gui_serves_a_named_store_from_outside_any_git_repo(user: User, tmp_path, monkeypatch):
    """`symbion --dir ../x-notes serve` from a home directory: no project to
    name, so the header falls back to the store's own name."""
    store_dir = tmp_path / "x-notes"
    monkeypatch.chdir(tmp_path)
    ctx = api.resolve(str(store_dir))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "from nowhere"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    await user.should_see("x-notes · symbion")
    await user.should_see("from nowhere")
