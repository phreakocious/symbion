from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("nicegui")

from nicegui.testing import User        # noqa: E402

from symbion import api, gitref, store  # noqa: E402
from symbion.gui.pages import build_page  # noqa: E402

pytestmark = pytest.mark.usefixtures("tmp_store")


@pytest.fixture
def ctx_with_notes(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "alpha body", "tags": ["red"]}, author="ada")
    api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "widget"},
                  "body": "beta body", "tags": ["blue"]}, author="sam")
    build_page(ctx, author="ada")
    return ctx


def _count(user: User) -> str:
    """The count line, whole: `should_see("1 note")` also passes on "21 notes"."""
    (label,) = user.find(marker="result-count").elements
    return label.text


async def test_the_menu_button_takes_the_theme_colour(user: User, ctx_with_notes):
    """Its `text-body` is `!important` but unlayered, so the `text-primary` a
    NiceGUI button gets by default, `!important` in NiceGUI's last layer,
    won: the menu button was accent (2026-10-02). It carries no colour."""
    await user.open("/")
    (b,) = user.find(marker="menu").elements
    assert b.props.get("color") is None, b.props


async def test_notes_filters_by_tag(user: User, ctx_with_notes):
    await user.open("/notes?tag=red")
    await user.should_see("alpha body")
    await user.should_not_see("beta body")


async def test_the_commit_button_shows_a_hooks_refusal(user: User, ctx_with_notes,
                                                        tmp_path, refusing_hook):
    """The button notified 'nothing to commit' when a hook refused."""
    words = refusing_hook(tmp_path)
    await user.open("/notes")
    user.find(marker="commit-button").click()
    await user.should_see(words)
    assert not user.notify.contains("nothing to commit")


async def test_the_commit_button_sits_left_of_new_note(user: User, ctx_with_notes):
    """To its right, New note moved whenever the commit button came or went
    (the owner, 2026-10-02). Element ids rise in the order they are built."""
    await user.open("/notes")
    (commit,) = user.find(marker="commit-button").elements
    (new,) = user.find(marker="new-note").elements
    assert commit.id < new.id


async def test_a_check_with_a_non_string_sha_renders(user: User, repo, tmp_path):
    """`{"sha": 123}`, as a provenance command may print it, is no commit:
    the badge, its tip and the recent board read it as unverifiable."""
    ctx = api.resolve(str(tmp_path))
    store.add(tmp_path, kind="check", target={"type": "item", "name": "x"},
              checked="c", result="r", provenance={"sha": 123})
    build_page(ctx, author="ada")
    for page in ("/", "/notes"):
        await user.open(page)
        await user.should_see("unverifiable")


def _commit_with_origin(ctx, tmp_path_factory):
    bare = tmp_path_factory.mktemp("origin")
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(ctx.store_dir), "remote", "add", "origin", str(bare)],
                   check=True)
    api.commit(ctx, "notes")


async def test_the_push_button_shows_unpushed_commits_and_pushes_them(
        user: User, ctx_with_notes, tmp_path, tmp_path_factory):
    """After the GUI committed, pushing took a terminal (the owner, 2026-10-01)."""
    await user.open("/notes")
    await user.should_not_see(marker="push-button")      # no remote: nowhere to push
    _commit_with_origin(ctx_with_notes, tmp_path_factory)
    n = gitref.unpushed(tmp_path)
    await user.open("/notes")
    (button,) = user.find(marker="push-button").elements
    assert str(n) in [e.text for e in button.default_slot.children]
    user.find(marker="push-button").click()
    # The push runs in a thread: 0.3 s, the default wait, failed on a CI runner.
    await user.should_see("pushed", retries=40)
    assert gitref.unpushed(tmp_path) == 0
    await user.open("/notes")
    await user.should_not_see(marker="push-button")


async def test_the_git_buttons_follow_commits_and_pushes_made_elsewhere(
        user: User, ctx_with_notes, tmp_path, tmp_path_factory, monkeypatch):
    """A commit at a terminal left the commit button on the page (the owner,
    2026-10-01): each button is read again on a timer."""
    from symbion.gui import chrome
    monkeypatch.setattr(chrome, "_GIT_EVERY", 0.05)
    await user.open("/notes")
    await user.should_see(marker="commit-button")
    _commit_with_origin(ctx_with_notes, tmp_path_factory)      # as at a terminal
    await user.should_not_see(marker="commit-button", retries=40)
    await user.should_see(marker="push-button", retries=40)
    api.push(ctx_with_notes, capture=True)
    await user.should_not_see(marker="push-button", retries=40)
    api.add(ctx_with_notes, {"kind": "note", "target": {"type": "project", "name": None},
                             "body": "gamma"}, author="sam")
    await user.should_see(marker="commit-button", retries=40)


async def test_the_push_button_shows_gits_refusal(user: User, ctx_with_notes, tmp_path,
                                                  tmp_path_factory):
    _commit_with_origin(ctx_with_notes, tmp_path_factory)
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho 'hook: push refused' >&2\nexit 1\n")
    hook.chmod(0o755)
    await user.open("/notes")
    user.find(marker="push-button").click()
    await user.should_see("hook: push refused", retries=40)
    assert not user.notify.contains("pushed")


async def test_a_search_finds_rows_by_words_in_any_order(user: User, ctx_with_notes):
    await user.open("/notes?q=BODY%20beta")
    await user.should_see("beta")
    await user.should_not_see("alpha")
    assert _count(user).startswith("1 of 2 notes match")


async def test_a_search_narrows_the_filters_in_view(user: User, ctx_with_notes):
    """`keep` carries the page's filters into the search, and a search
    carries into the chips: both directions narrow."""
    await user.open("/notes?q=body&tag=blue")
    await user.should_see("beta")          # the hit is marked: "beta <mark>body</mark>"
    await user.should_not_see("alpha")
    assert _count(user).startswith("1 of 1 note match")


async def test_a_pasted_id_tail_finds_its_row(user: User, ctx_with_notes):
    """Ids are not in the text a search reads; a pasted one read 0."""
    nid = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    await user.open(f"/notes?q=%E2%80%A6{nid.rsplit('-', 1)[1]}")
    await user.should_see("beta")
    await user.should_not_see("alpha")


async def test_a_superseded_id_opens_its_current_row(user: User, ctx_with_notes):
    """Rows cite ids that were superseded since; /notes?id= read "0 notes"
    for them, and so did a search for one."""
    old = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    api.supersede(ctx_with_notes, old, author="ada", body="beta revised")
    await user.open(f"/notes?id={old}")
    await user.should_see("beta revised")
    await user.should_see(f"{old} was superseded")
    await user.open(f"/notes?q={old.rsplit('-', 1)[1]}")
    await user.should_see("beta revised")
    assert _count(user).startswith("1 of 2 notes match")


async def test_shift_enter_adds_a_note_and_keeps_the_composer_open(
        user: User, ctx_with_notes, monkeypatch):
    """The owner, 2026-10-01: shift+enter adds another note. ⌘Enter reloads
    the page, which closes the composer after each note."""
    from nicegui import ui
    reloads = []
    monkeypatch.setattr(user.navigate, "reload", lambda: reloads.append(1))   # the fixture's ui.navigate
    await user.open("/notes")
    user.find(marker="new-note").click()
    body = user.find(marker="note-body")
    for text in ("first of two", "second of two"):
        body.type(text).trigger("keydown.shift.enter.exact.prevent")
        assert user.notify.contains("added")
    assert {"first of two", "second of two"} <= \
        {n.body for n in store.load(ctx_with_notes.store_dir)}
    (dialog,) = user.find(ui.dialog).elements
    assert dialog.value and not body.elements.pop().value and not reloads
    dialog.close()                                    # Esc: the page shows them
    assert reloads == [1]


async def test_a_cited_tail_opens_its_row(user: User, ctx_with_notes):
    """Agents cite a row by its tail, and `show` takes one; /notes?id=<tail>
    read "0 notes", so a link built from a cited tail opened an empty page."""
    old = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    await user.open(f"/notes?id={old.split('-', 2)[2]}")
    await user.should_see("beta body")
    assert _count(user) == "1 note"
    api.supersede(ctx_with_notes, old, author="ada", body="beta revised")
    await user.open(f"/notes?id={old.split('-', 2)[2]}")
    await user.should_see("beta revised")
    await user.should_see(f"{old} was superseded")


async def test_a_tail_several_ids_end_in_lists_each(user: User, repo, tmp_path, monkeypatch):
    """Never pick one, as `show` does not: a 3-hex tail is often shared."""
    ctx = api.resolve(str(tmp_path))
    ids = iter(["20261001-000000-000001-abc", "20261001-000000-000002-abc"])
    monkeypatch.setattr(store, "new_id", lambda: next(ids))
    for body in ("first body", "second body"):
        api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": body}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes?id=abc")
    await user.should_see("first body")
    await user.should_see("second body")
    await user.should_see("2 rows end in -abc")


async def test_a_notes_own_view_lists_its_earlier_versions(user: User, ctx_with_notes):
    """Every version is kept; the GUI showed only the last one."""
    first = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    api.commit(ctx_with_notes, "c")          # each edit its own row (store.rewritable)
    mid = api.supersede(ctx_with_notes, first, author="ada", body="beta two").id
    api.commit(ctx_with_notes, "c")
    last = api.supersede(ctx_with_notes, mid, author="ada", body="beta three").id
    await user.open(f"/notes?id={last}")
    await user.should_see("earlier versions (2)")
    user.find(marker="history").click()           # an expansion renders its body lazily
    await user.should_see("beta two")
    await user.should_see("beta body")
    assert len(user.find(marker="note-edit").elements) == 1     # the current row's only


async def test_a_note_never_superseded_has_no_history(user: User, ctx_with_notes):
    nid = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    await user.open(f"/notes?id={nid}")
    await user.should_see("beta body")
    await user.should_not_see(marker="history")


async def test_a_search_result_shows_and_marks_a_hit_past_the_clip(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "filler words " * 40 + "the needle is here"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes?q=needle")
    (snip,) = user.find(marker="note-snippet").elements
    assert snip.content.startswith("…")
    assert "<mark>needle</mark>" in snip.content


async def test_the_header_search_keeps_the_filters_in_view(user: User, ctx_with_notes):
    await user.open("/notes?kind=bug")
    user.find(marker="search").type("widget").trigger("keydown.enter")
    await user.should_see('search "widget" · kind=bug')
    assert _count(user).startswith("1 of 1 note match")


async def test_notes_with_no_filters_shows_everything(user: User, ctx_with_notes):
    await user.open("/notes")
    await user.should_see("alpha body")
    await user.should_see("beta body")


async def test_a_stale_bookmark_renders_rather_than_erroring(user: User, ctx_with_notes):
    await user.open("/notes?tag=nonexistent&utm_source=x")
    await user.should_see(marker="result-count")
    assert _count(user) == "0 notes"


async def test_an_unknown_param_does_not_silently_widen_the_result(
        user: User, ctx_with_notes):
    """The other direction of the dropped-param rule. Dropping utm_source must
    not also drop the tag beside it -- that would render every note and read
    as success."""
    await user.open("/notes?tag=red&utm_source=x")
    await user.should_see(marker="result-count")
    assert _count(user) == "1 note"
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
    (brand,) = user.find(marker="brand").elements
    assert brand.text == ctx_with_notes.cfg.project_root.name


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


async def test_a_recent_board_holds_young_rows_and_those_current_at_head(
        user: User, repo, tmp_path):
    """The 25 newest checks of any age filled the board, nearly all `behind`
    (the owner, 2026-10-02). An old row still current at HEAD stays: in a
    quiet repo it is the live state. The rest are one link away."""
    import json

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    (repo / "g").write_text("y")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c1")
    old = {"kind": "check", "target": {"type": "project", "name": None},
           "created_at": "2020-01-01T00:00:00+00:00", "author": "ada", "body": "",
           "checked": "suite"}
    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        for tail, result, rev in (("aaa", "old behind", "HEAD~1"), ("bbb", "old current", "HEAD")):
            f.write(json.dumps({**old, "id": f"20200101-000000-000000-{tail}", "result": result,
                                "provenance": {"sha": git("rev-parse", rev), "dirty": False}})
                    + "\n")
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "check", "target": {"type": "project", "name": None},
                  "checked": "suite", "result": "fresh"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    await user.should_see("fresh")
    await user.should_see("old current")
    await user.should_not_see("old behind")
    (aside,) = user.find(marker="board-aside").elements
    assert (aside.text, aside.props["href"]) == ("· 1 more", "/notes?kind=check")


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
    # a heading's title and count, read as a pair: `find` returns a set
    boards = {e.text: e.parent_slot.parent.default_slot.children[1].text
              for e in user.find(marker="board-title").elements}
    assert boards == {"open anomaly": "1", "priority": "0", "recent audit": "0"}, boards


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


async def test_the_gui_serves_a_named_store_from_outside_any_git_repo(user: User,
                                                                     tmp_path_factory,
                                                                     monkeypatch):
    """`symbion --dir ../x-notes serve` from a home directory: no project to
    name, so the header falls back to the store's own name. Not tmp_path:
    tmp_store makes that a store, and so a git repo."""
    home = tmp_path_factory.mktemp("home")
    store_dir = home / "x-notes"
    store.ensure_store(store_dir)
    monkeypatch.chdir(home)
    ctx = api.resolve(str(store_dir))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "from nowhere"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    (brand,) = user.find(marker="brand").elements
    assert brand.text == "x-notes"
    await user.should_see("from nowhere")


# ---- gui spec drift (audit 2026-09-25): chips narrow, arc chip, arc notes ----

def _hrefs(user: User, marker: str) -> set:
    return {e.props.get("href") for e in user.find(marker=marker).elements}


async def test_a_chip_in_a_filtered_view_narrows_it(user: User, ctx_with_notes):
    """A chip dropped the current filters instead of adding one (href() got
    one param); the spec: clicking it from a filtered view narrows further.
    Off the filtered view a chip starts a filter."""
    await user.open("/notes?kind=note")
    assert _hrefs(user, "tag-red") == {"/notes?kind=note&tag=red"}
    await user.open("/")                       # the open bug sits on a board
    assert _hrefs(user, "tag-blue") == {"/notes?tag=blue"}


async def test_a_note_under_an_arc_links_to_it_and_the_arc_page_lists_its_notes(
        user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "Release", "", "mixed", author="ada").id
    api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "tag it"},
                  "arc_id": aid, "body": "a box"}, author="ada")
    api.add(ctx, {"kind": "decision", "target": {"type": "arc", "name": aid},
                  "body": "about the arc itself"}, author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": [{"type": "arc", "name": aid}], "body": "refs the arc"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes?kind=task")
    assert _hrefs(user, "note-arc") == {f"/arc?id={aid}"}
    await user.open(f"/arc?id={aid}")
    await user.should_see("about the arc itself")
    await user.should_see("refs the arc")


# ---- seen in the browser, 2026-09-27: what a human reads ----

async def test_a_board_clips_a_long_body_and_links_the_full_note(user: User, repo, tmp_path):
    """The boards printed every open row's body in full: one bug's four
    paragraphs filled the screen. A board is a list to scan; the note's own
    page is where it reads in full."""
    ctx = api.resolve(str(tmp_path))
    long = "Opening claim of the bug. " + "Detail sentence. " * 20 + "\n\nDEEP PARAGRAPH WORDS"
    n = api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "x"}, "body": long},
                author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    await user.should_see("Opening claim of the bug.")
    await user.should_not_see("DEEP PARAGRAPH WORDS")
    assert _hrefs(user, "note-full") == {f"/notes?id={n.id}"}   # the clipped text itself
    await user.should_not_see("full note")
    await user.open(f"/notes?id={n.id}")
    await user.should_see("DEEP PARAGRAPH WORDS")


async def test_a_checklist_row_says_which_box_it_is(user: User, repo, tmp_path):
    """Six boxes on one file drew six identical target lines."""
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "A", "", "mixed", author="ada").id
    for body in ("first box on the file", "second box on the file"):
        api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "same"},
                      "arc_id": aid, "body": body}, author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={aid}")
    await user.should_see("first box on the file")
    await user.should_see("second box on the file")


async def test_a_commit_ref_chip_is_a_short_sha(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}, "body": "b",
                  "refs": [{"type": "commit", "name": "HEAD"}]}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    texts = {e.text for e in user.find(marker="note-ref").elements}
    assert len(texts) == 1 and len(texts.pop().split(":", 1)[1]) == 7, texts


async def test_an_archived_arc_says_so_and_is_reachable(user: User, repo, tmp_path):
    """Its page offered `archive` and never said it was archived, and the
    index listed archived arcs as labels, not links."""
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "Old", "", "mixed", author="ada").id
    api.archive_arc(ctx, aid)
    build_page(ctx, author="ada")
    await user.open("/arcs")
    assert f"/arc?id={aid}" in _hrefs(user, "archived-arc")
    await user.open(f"/arc?id={aid}")
    await user.should_see("archived")
    await user.should_not_see(marker="arc-archive")


async def test_an_object_page_leads_with_open_rows_and_clips_resolved_ones(
        user: User, repo, tmp_path):
    """One file's page held 50 rows in full, open work buried in history
    (seen in the browser, 2026-09-27)."""
    ctx = api.resolve(str(tmp_path))
    done = api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "x"},
                         "body": "Old fixed bug. " + "history " * 60 + "\n\nOLD DEEP WORDS"},
                   author="ada")
    api.supersede(ctx, done.id, author="ada", status="resolved")
    api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "x"},
                  "body": "Open work. " + "detail " * 60 + "\n\nOPEN DEEP WORDS"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/object?type=item&name=x")
    await user.should_see("OPEN DEEP WORDS")
    await user.should_not_see("OLD DEEP WORDS")
    rows = [e for e in user.find(marker="note-row").elements]
    assert len(rows) == 2
    # a parked idea is a shelved thought: clipped like history
    api.add(ctx, {"kind": "idea", "target": {"type": "item", "name": "x"},
                  "body": "Shelved. " + "later " * 60 + "\n\nIDEA DEEP WORDS"}, author="ada")
    await user.open("/object?type=item&name=x")
    await user.should_not_see("IDEA DEEP WORDS")


async def test_a_row_carries_its_kinds_class(user: User, repo, tmp_path):
    """A row's left edge takes its kind's colour, as its chip does: the list
    read as one grey sheet (2026-10-01)."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "x"}, "body": "b"},
            author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    row, = user.find(marker="note-row").elements
    assert "sb-kind-bug" in row.classes


async def test_the_tags_page_names_near_duplicates(user: User, repo, tmp_path):
    """`flaky-test` beside `flaky-tests` split one subject in an adopter
    store; the page said "near-synonyms are drift" and showed none."""
    ctx = api.resolve(str(tmp_path))
    for tags in (["flaky-test"], ["flaky-test"], ["flaky-tests"], ["other"]):
        api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b", "tags": tags}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/tags")
    near = [e.text for e in user.find(marker="tag-near").elements]
    assert near == ["flaky-test (2) · flaky-tests (1)"], near


async def test_an_unknown_path_gets_symbions_404_not_niceguis(user: User, ctx_with_notes):
    """NiceGUI's page is its sad-face art with no way back (a gui-theming
    task). The replacement keeps the header and names the path. `user.open`
    refuses any status but 200, so this reads the response itself."""
    r = await user.http_client.get("/no/such/page")
    assert r.status_code == 404
    assert "nothing is served at /no/such/page" in r.text
    assert "writing as ada" in r.text                # the header came with it
    assert "sad_face" not in r.text and "<svg" not in r.text.split("<body")[1]


async def test_a_page_that_fails_says_why_inside_symbions_chrome(user: User, ctx_with_notes,
                                                                caplog):
    from nicegui import ui

    @ui.page("/boom")
    def boom():
        raise RuntimeError("kaput")

    r = await user.http_client.get("/boom")
    assert r.status_code == 500
    assert "RuntimeError: kaput" in r.text
    assert "writing as ada" in r.text
    # NiceGUI logs the exception, and the `user` fixture fails a test that
    # leaves an ERROR log behind: this one was expected.
    assert "kaput" in caplog.text
    caplog.clear()


async def test_a_long_list_says_what_it_did_not_show(user: User, repo, tmp_path, monkeypatch):
    """/notes renders a page of rows; the rest are named, never silently cut."""
    # Through build_page's own globals, not `import pages`: the `user`
    # fixture's teardown pops every module that defined a page from
    # sys.modules, so after the first test an import gets a fresh copy that
    # build_page (imported above) never reads, and the patch misses.
    monkeypatch.setitem(build_page.__globals__, "PAGE_ROWS", 2)
    ctx = api.resolve(str(tmp_path))
    for i in range(3):
        api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": f"row {i}", "tags": ["t"]}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes?tag=t")
    await user.should_see("row 2")
    await user.should_not_see("row 0")                     # the oldest is the one cut
    assert _count(user) == "3 notes; newest 2 shown"       # named at the top too
    (more,) = user.find(marker="show-all").elements
    assert more.text == "+1 older not shown — show all"
    assert more.props["href"] == "/notes?tag=t&all=1"      # the filters survive it
    await user.open(more.props["href"])
    await user.should_see("row 0")
    await user.should_not_see(marker="show-all")


# ---- the card layout's gaps, measured in the browser 2026-10-01 ----

async def test_a_board_names_the_open_rows_an_arc_holds(user: User, repo, tmp_path):
    """The sidebar counts every open row of a kind; a home board lists those
    on no active arc. Side by side, the two counts read as a miscount."""
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "Release", "", "mixed", author="ada").id
    for name, arc in (("loose", None), ("boxed", aid), ("boxed too", aid)):
        api.add(ctx, {"kind": "task", "target": {"type": "item", "name": name},
                      "arc_id": arc, "body": name}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    (aside,) = user.find(marker="board-aside").elements    # no other kind has one
    assert aside.text == "· 2 in arcs"
    assert aside.props["href"] == "/notes?kind=task&status=open"
    (side,) = user.find(marker="open-task").elements
    assert side.default_slot.children[-1].text == "3"        # 1 on the board + 2


async def test_the_top_bar_names_the_writer_while_the_sidebar_hides(user: User, ctx_with_notes):
    """Below the drawer's breakpoint the sidebar and its badge sit behind the
    menu button, and the page still writes: the top bar names who, first."""
    await user.open("/")
    (name,) = user.find(marker="author-badge-top").elements
    assert name.text == "ada"
    assert "sb-narrow" in name.parent_slot.parent.classes


async def test_the_fonts_ship_in_the_package_and_load_from_the_gui(user: User, ctx_with_notes):
    """From Google Fonts, every page load reached a third party."""
    import re
    import tomllib
    from pathlib import Path

    from nicegui import app

    from symbion.gui.theme import DARK_CSS, FONTS_DIR, FONTS_HTML, FONTS_URL, root_vars_css
    files = re.findall(rf"url\({FONTS_URL}/([^)]+)\)", FONTS_HTML)
    assert files and all((FONTS_DIR / f).is_file() for f in files), files
    assert not re.search(r"https?://", FONTS_HTML + DARK_CSS + root_vars_css())
    assert FONTS_URL + "/{path:path}" in {getattr(r, "path", None) for r in app.routes}
    meta = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "data/fonts/*" in meta["tool"]["setuptools"]["package-data"]["symbion"]


# ---- the owner's GUI rows of 2026-10-01 ----

async def test_the_top_bar_ends_in_the_pages_title(user: User, ctx_with_notes):
    """A title under the bar left a band of nothing above it."""
    for path, title in (("/", "Notebook"), ("/tags", "Tags"), ("/arcs", "Arcs"),
                        ("/object?type=item&name=widget", "item: widget"),
                        ("/notes?kind=bug", "kind=bug")):
        await user.open(path)
        (here,) = user.find(marker="page-title").elements
        assert here.text == title, path


async def test_new_note_opens_on_the_pages_object(user: User, ctx_with_notes, tmp_path):
    """The composer sat on two pages; now every page opens it, on its object."""
    await user.open("/object?type=item&name=widget")
    user.find(marker="new-note").click()
    await user.should_see(marker="note-body")
    assert user.find(marker="note-on-type").elements.pop().value == "item"
    assert user.find(marker="note-on-name").elements.pop().value == "widget"
    user.find(marker="note-body").type("from the dialog")
    user.find(marker="note-add").click()
    (row,) = [n for n in store.heads(store.load(tmp_path)) if n.body == "from the dialog"]
    assert (row.target.type, row.target.name) == ("item", "widget")


async def test_the_composer_puts_a_note_on_any_target(user: User, repo, tmp_path):
    """The owner, 2026-10-01: the GUI put a note only on the project, or on
    the object of the page it opened from. The name field offers the names
    this store has used for the type, and a catalog's for a catalog type."""
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text("""[catalogs]\nthing = 'printf "alpha\\nbeta\\n"'\n""")
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "widget"},
                  "body": "b"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    user.find(marker="new-note").click()
    await user.should_see(marker="note-body")
    (typ,) = user.find(marker="note-on-type").elements
    assert typ.value == "project"
    await user.should_not_see(marker="note-on-name")                   # a project has no name
    assert sorted(typ.options) == ["commit", "item", "project", "thing"]   # an arc: its select
    typ.value = "item"
    (name,) = user.find(marker="note-on-name").elements
    assert name.props["options"] == ["widget"]
    typ.value = "thing"
    assert name.props["options"] == ["alpha", "beta"]
    user.find(marker="note-body").type("on a thing")
    user.find(marker="note-add").click()
    assert user.notify.contains("name the thing")              # no name: nothing written
    assert [n.body for n in store.load(tmp_path)] == ["b"]
    name.value = "beta"                              # typed: the browser sends the text
    user.find(marker="note-add").click()
    (row,) = [n for n in store.load(tmp_path) if n.body == "on a thing"]
    assert (row.target.type, row.target.name) == ("thing", "beta")


async def test_new_note_on_an_arc_page_starts_in_that_arc(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "Release", "", "mixed", author="ada").id
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={aid}")
    (here,) = user.find(marker="page-title").elements
    assert here.text == "Release"
    user.find(marker="new-note").click()
    await user.should_see(marker="note-arc-select")
    assert user.find(marker="note-arc-select").elements.pop().value == aid


async def test_the_keys_list_names_the_shortcuts_and_the_kinds(user: User, ctx_with_notes):
    await user.open("/")
    user.find(marker="keys").click()
    await user.should_see(marker="keys-list")
    await user.should_see("!kind")
    await user.should_see("in a note: sets its kind, one of note, decision, bug, task, "
                          "question, idea, check; its first letters do, while one kind "
                          "alone starts with them")


async def test_the_sidebar_lists_the_newest_closed_rows(user: User, repo, tmp_path):
    """The owner, 2026-10-01: "sidebar should show recently closed items"."""
    ctx = api.resolve(str(tmp_path))
    ids = [api.add(ctx, {"kind": "task", "target": {"type": "item", "name": f"t{i}"},
                         "body": f"task {i}"}, author="ada").id for i in range(6)]
    closed = [api.supersede(ctx, i, author="ada", status="resolved").id for i in ids]
    api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "t6"},
                  "body": "newest, and open"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    items = sorted(user.find(marker="closed-item").elements, key=lambda e: e.id)  # a set
    assert [e.props["href"] for e in items] == [f"/notes?id={i}" for i in closed[:0:-1]]
    assert "task 5" in [getattr(c, "text", "") for c in items[0].default_slot.children]
    (head,) = user.find(marker="closed-all").elements
    assert head.props["href"] == "/notes?status=resolved"


async def test_the_sidebar_counts_open_parked_rows_under_their_own_head(
        user: User, repo, tmp_path):
    """The owner, 2026-10-02: "parked items should be included in the
    sidebar (ideas are invisible today)". Under "parked", not "open": a
    parked row stays out of the open views."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "idea", "target": {"type": "project", "name": None},
                  "body": "someday"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    (side,) = user.find(marker="open-idea").elements
    assert side.props["href"] == "/notes?kind=idea&status=open"
    assert side.default_slot.children[-1].text == "1"
    assert side.parent_slot.children[0].text == "parked"


def test_the_viewer_reads_only_markdown_inside_the_checkout(repo, tmp_path):
    """A target name is typed text: the viewer must not read through it."""
    from symbion.gui.notes import md_path
    ctx = api.resolve(str(tmp_path))
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("# A")
    (repo / "notes.txt").write_text("x")
    (tmp_path / "out.md").write_text("outside")
    (repo / "link.md").symlink_to(tmp_path / "out.md")
    assert md_path(ctx, "docs/a.md") == (repo / "docs" / "a.md").resolve()
    for name in ("../out.md", str(tmp_path / "out.md"), "link.md", "notes.txt",
                 "gone.md", "docs", None):
        assert md_path(ctx, name) is None, name


async def test_a_markdown_target_opens_in_a_dialog(user: User, repo, tmp_path):
    """The owner, 2026-10-02: ".md files should be viewable in a modal
    markdown viewer". From the card's target and refs, and the file's page."""
    ctx = api.resolve(str(tmp_path))
    (repo / "README.md").write_text("# Read me\n\nthe *whole* file")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "README.md"},
                  "body": "about it"}, author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "other.md"},
                  "body": "no such file"}, author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": [{"type": "item", "name": "README.md"}], "body": "cites it"},
            author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    assert len(user.find(marker="md-open").elements) == 2    # a target, a ref; not other.md
    user.find(marker="md-open").click()
    await user.should_see(marker="md-view")
    (view,) = user.find(marker="md-view").elements
    assert "the *whole* file" in view.content
    await user.open("/object?type=item&name=README.md")
    labels = [b.props.get("label") for b in user.find(marker="md-open").elements]
    assert sorted(labels, key=str) == ["", "read the file"]   # the page's own, the ref's


async def test_the_sidebar_links_the_source(user: User, ctx_with_notes):
    await user.open("/")
    (link,) = user.find(marker="source").elements
    assert link.props["href"] == "https://github.com/phreakocious/symbion"
    assert link.props["target"] == "_blank"


async def test_a_card_carries_its_whole_id_for_the_page_filter(user: User, ctx_with_notes):
    """The filter reads the card's text, which prints only the id's tail."""
    await user.open("/notes")
    ids = {e.props.get("data-id") for e in user.find(marker="note-row").elements}
    assert ids == {n.id for n in store.heads(store.load(ctx_with_notes.store_dir))}


async def test_typing_in_the_search_box_does_not_leave_the_page(user: User, ctx_with_notes):
    """Typing filters the page in the browser; only Enter searches the store."""
    await user.open("/tags")
    user.find(marker="search").type("beta")
    await user.should_not_see(marker="result-count")
    user.find(marker="search").trigger("keydown.enter")
    await user.should_see(marker="result-count")
    assert _count(user).startswith("1 of 2 notes match")


async def test_the_sidebar_links_the_other_stores_a_serve_runs_on(user: User, repo, tmp_path,
                                                                    monkeypatch):
    """The owner, 2026-10-01: switch stores in the GUI. One `serve` per store
    keeps each store's author and repo; each records itself while it runs,
    and the sidebar links the others. A dead serve's record is dropped, and
    a record that does not parse is skipped."""
    import json
    import os
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    records = tmp_path / "cache" / "symbion" / "serve"
    records.mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    for pid, url, store_dir, name in (
            (os.getpid(), "http://127.0.0.1:1111", tmp_path / "elsewhere-notes", "elsewhere"),
            (os.getpid(), "http://127.0.0.1:2222", tmp_path, "this one"),
            (dead.pid, "http://127.0.0.1:3333", tmp_path / "gone-notes", "gone")):
        (records / f"{name}.json").write_text(json.dumps(
            {"pid": pid, "url": url, "store": str(store_dir.resolve())}))
    (records / "torn.json").write_text('{"pid": ')
    ctx = api.resolve(str(tmp_path))
    build_page(ctx, author="ada")
    await user.open("/")
    (link,) = user.find(marker="store-item").elements
    assert link.props["href"] == "http://127.0.0.1:1111"
    assert "elsewhere" in [getattr(c, "text", "") for c in link.default_slot.children]
    assert not (records / "gone.json").exists()


async def test_a_page_of_check_badges_reads_head_once(user: User, repo, tmp_path, monkeypatch):
    """`/` took 1.7 s, three quarters of it four git calls per check badge,
    HEAD read again by each (2026-10-01). Every page that shows a badge
    reads HEAD once, and rows stamped at one commit share one answer."""
    ctx = api.resolve(str(tmp_path))
    ids = [api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "widget"},
                         "checked": f"run {i}", "result": "ok"}, author="ada").id
           for i in range(3)]
    aid = api.create_arc(ctx, "A", "", "mixed", author="ada").id
    api.add(ctx, {"kind": "check", "target": {"type": "arc", "name": aid},
                  "checked": "about the arc", "result": "ok"}, author="ada")
    api.commit(ctx, "c")                     # each edit its own row (store.rewritable)
    last = api.supersede(ctx, ids[0], author="ada", result="still ok").id
    build_page(ctx, author="ada")
    calls = []
    real = gitref._git
    monkeypatch.setattr(gitref, "_git", lambda c, *a: calls.append(a) or real(c, *a))
    for page in ("/", "/notes", "/object?type=item&name=widget", f"/arc?id={aid}",
                 f"/notes?id={last}"):
        calls.clear()
        await user.open(page)
        if "?id=2" in page:
            user.find(marker="history").click()   # the earlier version's badge
        await user.should_see("current")
        heads = [a for a in calls if a[:4] == ("rev-parse", "--verify", "-q", "HEAD")]
        walks = [a for a in calls if a[:2] == ("rev-list", "--left-right")]
        assert len(heads) == 1 and len(walks) <= 1, (page, calls)


async def test_targets_page_boards_each_target_open_work_first(user: User, repo, tmp_path):
    """One board per target, as the notebook has one per kind: its open rows
    as cards, its title a link to the object, and the aside counting what
    the object page holds, refs included, so the two agree. A target only
    referenced gets no board: 165 bare commit shas filled a real store's page.
    A commit's title is its subject, as `list` prints it."""
    ctx = api.resolve(str(tmp_path))
    add = lambda kind, name, body, **kw: api.add(   # noqa: E731
        ctx, {"kind": kind, "target": {"type": "item", "name": name}, "body": body, **kw},
        author="ada")
    add("task", "widget", "done body", status="resolved")
    add("bug", "widget", "open body")
    add("task", "other", "ref body", refs=[{"type": "item", "name": "widget"},
                                            {"type": "item", "name": "elsewhere"}])
    sha = api.add(ctx, {"kind": "note", "target": {"type": "commit", "name": "HEAD"}},
                  author="ada").target.name
    add("note", "quiet", "quiet body")     # newest, yet after the open ones
    build_page(ctx, author="ada")
    await user.open("/targets")
    # `find` returns a set; element ids rise in the order the page made them
    titles = [e.text for e in sorted(user.find(marker="board-title").elements,
                                     key=lambda e: e.id)]
    assert titles == ["item: other", "item: widget", "item: quiet",
                      f"commit: {sha[:7]} c0"], titles
    widget = next(e for e in user.find(marker="board-title").elements if e.text == "item: widget")
    assert widget.props["href"] == "/object?type=item&name=widget"
    asides = {e.props["href"]: e.text for e in user.find(marker="board-aside").elements}
    assert asides["/object?type=item&name=widget"] == "· 3 notes", asides
    # Two cards, each on its own target: the ref's open task is counted on
    # widget's board, not carded there. Cards, not text: the sidebar's closed
    # list shows the resolved row's body too.
    assert len(user.find(marker="note-row").elements) == 2
    await user.should_see("open body")
    await user.should_see("ref body")
    assert user.find(marker="place-targets").elements
