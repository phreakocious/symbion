from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest

pytest.importorskip("nicegui")

from nicegui import ElementFilter, ui   # noqa: E402
from nicegui.testing import User        # noqa: E402
from nicegui.testing.user_interaction import UserInteraction  # noqa: E402

from symbion import api, gitref, store  # noqa: E402
from symbion.gui.pages import build_page  # noqa: E402

from _cards import cards, drawn, part, parts, see, not_see, template   # noqa: E402

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


def _nav_n(item) -> str:
    """A sidebar kind's count, found by its class: its tooltip comes after it."""
    (n,) = [c for c in item.default_slot.children if "sb-nav-n" in c.classes]
    return n.text


async def test_the_menu_button_takes_the_theme_colour(user: User, ctx_with_notes):
    """Its `text-body` is `!important` but unlayered, so the `text-primary` a
    NiceGUI button gets by default, `!important` in NiceGUI's last layer,
    won: the menu button was accent (2026-10-02). It carries no colour."""
    await user.open("/")
    (b,) = user.find(marker="menu").elements
    assert b.props.get("color") is None, b.props


async def test_a_page_carries_the_stores_kind_colours_after_the_theme(user: User, repo,
                                                                      tmp_path):
    """A kind's `color` is a rule on its class, and only a rule after the
    theme's beats `sb-kind-own` and a default kind's colour. `_client` is
    nicegui's: its test User has no public way to the page's head."""
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nanomaly = { status = true, color = "flamingo" }\nnote = {}\n')
    build_page(api.resolve(str(tmp_path)), author="ada")
    await user.open("/")
    head = user._client.head_html
    assert head.index(".sb-kind-own {") < head.index(".sb-kind-anomaly { --kind: #f2cdcd; }")


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
    await user.should_see(words, retries=40)    # the handler is async: it lands after click()
    assert not user.notify.contains("nothing to commit")


async def test_the_commit_button_sits_left_of_new_note(user: User, ctx_with_notes):
    """To its right, New note moved whenever the commit button came or went
    (the user, 2026-10-02). Element ids rise in the order they are built."""
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
    api.commit(ctx, "notes", everyone=True)


async def test_the_push_button_shows_unpushed_commits_and_pushes_them(
        user: User, ctx_with_notes, tmp_path, tmp_path_factory):
    """After the GUI committed, pushing took a terminal (the user, 2026-10-01)."""
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
    """A commit at a terminal left the commit button on the page (the user,
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
    api.add(ctx_with_notes, {"kind": "note", "target": {"type": "project", "name": None},
                             "body": "delta"}, author="ada")
    await user.should_see(marker="commit-button", retries=40)
    (button,) = user.find(marker="commit-button").elements
    assert "1" in [e.text for e in button.default_slot.children], "sam's is not ada's"


async def _until(cond, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    assert cond()


async def test_a_page_has_one_h1_its_boards_h2_and_a_skip_link_first(user: User,
                                                                      ctx_with_notes):
    """A screen reader moves through a page by its headings, and every title
    was a div; dozens of stops came before the first card (the GUI review,
    2026-11-07). The look stays the classes'."""
    for path, title in (("/", "Summary"), ("/notes?kind=note", "Notes"), ("/docs", "Docs")):
        await user.open(path)
        with user:
            heads = [e.tag for e in ElementFilter(kind=ui.element) if e.tag in ("h1", "h2")]
        (h1,) = user.find(marker="page-title").elements
        assert (h1.tag, h1.text, heads.count("h1")) == ("h1", title, 1), (path, heads)
    await user.open("/")
    boards = user.find(marker="board-title").elements
    assert boards, "the Summary of ctx_with_notes has boards"
    for b in boards:
        assert "h2" in (b.tag, b.parent_slot.parent.tag), (b.tag, b.text)
    (skip,) = user.find(marker="skip-link").elements
    (target,) = user.find(marker="skip-target").elements
    assert (skip.props["href"], target.props["id"]) == ("#sb-start", "sb-start")


async def test_a_hidden_tab_polls_each_minute_and_reads_on_its_return(user: User, ctx_with_notes,
                                                                      monkeypatch):
    """A hidden tab read the store's git every 5 s, as a shown one did (the
    GUI review, 2026-11-07). It reads each minute instead, not never: the
    installed app's badge is set from this tick. Shown again, it reads now."""
    from symbion.gui import chrome
    await user.open("/")
    (timer,) = user.find(marker="git-poll").elements
    assert timer.interval == chrome._GIT_EVERY
    reads, real = [], chrome.git_state
    monkeypatch.setattr(chrome, "git_state", lambda *a: reads.append(1) or real(*a))
    trigger = UserInteraction(user, {user.client.layout}, None).trigger
    trigger("sbHidden", True)                  # what the page's visibilitychange sends
    await _until(lambda: timer.interval == chrome._HIDDEN_EVERY == 60)
    assert reads == []
    trigger("sbHidden", False)
    await _until(lambda: timer.interval == chrome._GIT_EVERY and reads == [1])


async def test_a_write_elsewhere_shows_a_reload_button(user: User, ctx_with_notes, tmp_path,
                                                       monkeypatch):
    """A page drew the store once: an entry an agent wrote showed only after
    a reload, and nothing said one was due (the user's pick, 2026-10-16). The
    button lists those entries in the peek: a reload of a filtered page, as
    here, shows neither (the user, 2026-10-27)."""
    from symbion.gui import chrome
    monkeypatch.setattr(chrome, "_GIT_EVERY", 0.05)
    def words():                         # user.find raises on none
        with user:
            return [[e.text for e in b.default_slot.children]
                    for b in ElementFilter(marker="stale-button")]

    def said():                          # Reload's words for a screen reader
        with user:
            return [e.text for e in ElementFilter(marker="stale-said")]

    await user.open("/notes?tag=red")
    await asyncio.sleep(0.3)                                    # a few ticks
    assert (words(), said()) == ([], [""])
    api.add(ctx_with_notes, {"kind": "note", "target": {"type": "project", "name": None},
                             "body": "gamma"}, author="sam")      # as at a terminal
    await _until(lambda: words() == [["1", "new"]])
    assert said() == ["1 new entry since this page was drawn: reload"]
    beta = next(n for n in store.load(tmp_path) if n.body == "beta body")
    api.supersede(ctx_with_notes, beta.id, author="sam", body="beta revised")
    await _until(lambda: words() == [["2", "new or changed"]])
    # Its tip names each entry, who wrote it and what they did, newest first.
    gamma = next(n for n in store.load(tmp_path) if n.body == "gamma")
    assert await _hover(user, "stale-button") == "\n".join([
        "since this page was drawn:",
        f"sam · edited · bug {beta.id[-10:]} · beta revised",
        f"sam · new · note {gamma.id[-10:]} · gamma",
        "click to list them"])
    user.find(marker="stale-button").click()
    with user.scope(marker="peek"):
        await user.should_see("1 new entry and 1 changed entry since this page was drawn")
        await user.should_see("gamma")
        await user.should_see("beta revised")
        await user.should_not_see("alpha body")
    await user.should_see(marker="stale-button")                # the page is as it was
    user.find(marker="drift-reload").click()
    await asyncio.sleep(0.3)
    await user.should_not_see(marker="stale-button")


async def _arc_board(user, ctx, monkeypatch):
    """An arc of three boxes in two phases, its page on the board, polled fast."""
    from symbion.gui import chrome
    monkeypatch.setattr(chrome, "_GIT_EVERY", 0.05)
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.seed(ctx, act.id, "item", ["one", "two", "three"], author="ada")
    api.set_phases(ctx, act.id, ["todo", "doing"])
    ids = {n.target.name: n.id for n in store.arc_items(store.load(ctx.store_dir), act.id)}
    await user.open(f"/arc?id={act.id}")
    user.find(marker="arc-view-board").click()
    await user.should_see(marker="board-column")
    return act, ids


def _stale(user) -> list:
    with user:
        return [[e.text for e in b.default_slot.children]
                for b in ElementFilter(marker="stale-button")]


def _drop(user, **args) -> None:
    UserInteraction(user, {user.client.layout}, None).trigger("sbMove", args)


async def test_an_agents_move_offers_a_reload(user: User, ctx_with_notes, monkeypatch):
    """The poll stamped notes.jsonl only: a move, which writes arcs.jsonl, left an open
    board as it was and said nothing."""
    act, ids = await _arc_board(user, ctx_with_notes, monkeypatch)
    await asyncio.sleep(0.3)                                    # a few ticks
    assert _stale(user) == []
    store.move_box(ctx_with_notes.store_dir, ids["one"], phase="doing", author="sam")
    # The count is of entries: it read "1 arcs changed".
    await _until(lambda: _stale(user) == [["arcs changed"]])
    api.add(ctx_with_notes, {"kind": "note", "target": {"type": "project", "name": None},
                             "body": "gamma"}, author="sam")
    await _until(lambda: _stale(user) == [["1", "new · arcs"]])
    user.find(marker="stale-button").click()
    with user.scope(marker="peek"):
        await user.should_see("arcs changed since this page was drawn")


async def test_the_pages_own_moves_light_nothing(user: User, ctx_with_notes, monkeypatch):
    """A drop, a resolve into done, a reopen out of it and a phase menu each write
    from this page: none is news to it."""
    ctx = ctx_with_notes
    act, ids = await _arc_board(user, ctx, monkeypatch)
    _drop(user, id=ids["one"], phase="doing", before=None)      # arcs only
    _drop(user, id=ids["two"], phase="done", before=None)       # resolves: a note too
    await asyncio.sleep(0.1)
    assert store.arc_progress(store.load(ctx.store_dir), act.id)[0] == 1
    _drop(user, id=ids["two"], phase="todo", before=None)       # reopens and places
    await asyncio.sleep(0.4)
    assert store.arc_progress(store.load(ctx.store_dir), act.id)[0] == 0
    user.find(marker="arc-view-list").click()                   # the list's own writes
    await user.should_see(marker="checklist-row")
    (box,) = [c for c in cards(user) if c._props["data-id"] == ids["three"]]
    from _cards import _emit
    _emit(user, box, "move", "phase", "doing")                  # the chip menu
    part(user, "note-resolve", card=box).click()                # the tick
    user.find(marker="arc-edit").click()                        # the form opens
    user.find(marker="arc-phases").elements.pop().set_value("todo, doing, review")
    user.find(marker="arc-phases-save").click()                 # the phases editor
    await asyncio.sleep(0.4)
    assert store.load_arcs(ctx.store_dir)[0].phases == ("todo", "doing", "review")
    assert store.arc_progress(store.load(ctx.store_dir), act.id)[0] == 1
    assert _stale(user) == []


async def test_the_arc_pages_describe_and_star_light_nothing(user: User, ctx_with_notes,
                                                             monkeypatch):
    """Describe writes arcs.jsonl and a star writes a note, both from this page."""
    act, ids = await _arc_board(user, ctx_with_notes, monkeypatch)
    user.find(marker="arc-view-list").click()
    await user.should_see(marker="checklist-row")
    user.find(marker="arc-edit").click()
    user.find(marker="arc-description").elements.pop().set_value("a new line")
    user.find(marker="arc-describe").click()
    (box,) = [c for c in cards(user) if c._props["data-id"] == ids["one"]]
    from _cards import _emit
    _emit(user, box, "star")
    await asyncio.sleep(0.4)
    assert store.load_arcs(ctx_with_notes.store_dir)[0].description == "a new line"
    assert any("priority" in n.tags for n in store.load(ctx_with_notes.store_dir) if n.arc_id == act.id)
    assert _stale(user) == []


async def test_the_arc_pages_unarchive_and_seed_light_nothing(user: User, repo, tmp_path,
                                                              monkeypatch):
    from symbion.gui import chrome
    monkeypatch.setattr(chrome, "_GIT_EVERY", 0.05)
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text('[catalogs]\nthing = "echo a"\n')
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "thing", author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={act.id}")
    user.find(marker="arc-edit").click()
    user.find(marker="arc-seed").click()
    await user.should_see("seeded 1 new target(s)")
    await asyncio.sleep(0.4)
    assert _stale(user) == []
    api.archive_arc(ctx, act.id)
    await _until(lambda: _stale(user) != [])
    await user.open(f"/arc?id={act.id}")                        # a fresh page, nothing stale
    await asyncio.sleep(0.2)
    user.find(marker="arc-unarchive").click()
    await asyncio.sleep(0.4)
    assert not store.load_arcs(ctx.store_dir)[0].archived
    assert _stale(user) == []


async def test_the_pages_move_keeps_an_agents_unseen_move_offered(user: User, ctx_with_notes,
                                                                  monkeypatch):
    """The page's own drop must not take an agent's arc change with it: the poll
    had not yet seen it, and the board redraws one card only."""
    act, ids = await _arc_board(user, ctx_with_notes, monkeypatch)
    await asyncio.sleep(0.1)
    # both writes in one loop turn: no tick between them
    store.move_box(ctx_with_notes.store_dir, ids["one"], phase="doing", author="sam")
    _drop(user, id=ids["two"], phase="doing", before=None)
    await _until(lambda: _stale(user) == [["arcs changed"]])


async def test_resolving_a_pre_registration_in_the_dialog_lights_nothing(
        user: User, repo, tmp_path, monkeypatch):
    """The dialog's save is the page's own write, whether it came from a tick or a
    drop."""
    from symbion.gui import chrome
    monkeypatch.setattr(chrome, "_GIT_EVERY", 0.05)
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nprediction = { status = true, verdict = true }\n')
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "item", "name": "sweep"},
                      "arc_id": act.id}, author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={act.id}")
    user.find(marker="arc-view-board").click()
    await user.should_see(marker="board-column")
    _drop(user, id=p.id, phase="done", before=None)
    await user.should_see(marker="edit-result")
    user.find(marker="edit-result").elements.pop().set_value("FIRED")
    user.find(marker="edit-save").click()
    await asyncio.sleep(0.4)
    assert store.arc_progress(store.load(tmp_path), act.id) == (1, 1)
    assert _stale(user) == []


async def test_the_app_badge_counts_other_writers_entries_since_a_page_was_drawn(
        user: User, create_user, ctx_with_notes, monkeypatch):
    """The installed app's icon counts what others wrote since this writer
    last drew a page: the writer's own entries are no news, and drawing any
    page clears it. Every page of one writer sends the same count: the app
    has one icon, and its tabs would each set it to their own."""
    from nicegui.outbox import Outbox

    from symbion.gui import chrome
    monkeypatch.setattr(chrome, "_GIT_EVERY", 0.05)
    sent: dict[str, list[int]] = {}
    emit = Outbox._emit

    async def record(self, message):
        # A javascript_rule reads a `request_id`, which a call that awaits
        # no answer lacks.
        _, kind, data = message
        if kind == "run_javascript" and (m := re.fullmatch(r"sbBadge\((\d+)\)", data["code"])):
            sent.setdefault(self._client().id, []).append(int(m[1]))
        await emit(self, message)

    def last(u: User):
        return sent.get(u._client.id, [None])[-1]

    def note(body, author):
        api.add(ctx_with_notes, {"kind": "note", "target": {"type": "project", "name": None},
                                 "body": body}, author=author)

    monkeypatch.setattr(Outbox, "_emit", record)
    await user.open("/notes")
    await _until(lambda: last(user) == 0)
    note("gamma", "sam")
    await _until(lambda: last(user) == 1)
    note("delta", "ada")
    await asyncio.sleep(0.3)
    assert last(user) == 1, "ada's own entry is no news to ada"
    tab = create_user()
    await tab.open("/tags")
    await _until(lambda: last(tab) == 0 and last(user) == 0)
    note("epsilon", "sam")
    await _until(lambda: last(tab) == 1 and last(user) == 1)


async def test_the_commit_button_commits_only_its_authors_rows(user: User, ctx_with_notes,
                                                              tmp_path):
    """It took an agent's pending rows under the GUI's message (2026-10-09);
    the user: "serve should only be allowed to commit its own rows"."""
    await user.open("/notes")
    (button,) = user.find(marker="commit-button").elements
    assert "1" in [e.text for e in button.default_slot.children]
    user.find(marker="commit-button").click()
    await user.should_see("committed: entries ada 1", retries=40)
    assert [r["author"] for r in store.pending_rows(tmp_path)] == ["sam"]


async def _hover(user, marker: str) -> str:
    """The tip a hover asks the server for (chrome._tip_on_hover)."""
    (el,) = user.find(marker=marker).elements
    el._props.pop("data-tip", None)
    user.find(marker=marker).trigger("mouseenter")
    await _until(lambda: "data-tip" in el._props)
    return el._props["data-tip"]


async def test_the_commit_buttons_tip_names_each_entry_and_what_was_done(
        user: User, ctx_with_notes, tmp_path):
    """Its tip said whose entries it commits, not which (the user, 2026-11-07).
    An entry is new until a commit holds it, however often it was edited."""
    ctx = ctx_with_notes
    alpha = next(n for n in store.load(tmp_path) if n.body == "alpha body")
    await user.open("/notes")
    assert await _hover(user, "commit-button") == "\n".join([
        "commit your 1 entry:", f"new · note {alpha.id[-10:]} · alpha body",
        "left for their writers: 1 entry"])
    api.commit(ctx, "first", author="ada")
    bug = api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "w"},
                        "body": "broken"}, author="ada")
    api.supersede(ctx, bug.id, author="ada", body="broken twice")    # new, edited: new
    api.supersede(ctx, alpha.id, author="ada", tags=["red", "priority"])
    await user.open("/notes")
    assert await _hover(user, "commit-button") == "\n".join([
        "commit your 2 entries:", f"starred it · note {alpha.id[-10:]} · alpha body",
        f"new · bug {bug.id[-10:]} · broken twice", "left for their writers: 1 entry"])  # newest first
    api.commit(ctx, "second", author="ada")
    api.supersede(ctx, bug.id, author="ada", status="resolved")
    api.supersede(ctx, alpha.id, author="ada", body="alpha, rewritten")
    api.supersede(ctx, alpha.id, author="ada", body="alpha, rewritten again")
    await user.open("/notes")
    assert await _hover(user, "commit-button") == "\n".join([
        "commit your 2 entries:", f"edited · note {alpha.id[-10:]} · alpha, rewritten again",
        f"resolved · bug {bug.id[-10:]} · broken twice",
        "left for their writers: 1 entry"])


async def test_the_commit_tip_names_an_entry_by_its_id_under_anothers_edit(
        user: User, ctx_with_notes, tmp_path):
    """sam edited ada's pending entry: ada's commit takes her version, which
    the edit saved under a new id, and the tip named that id (seen
    2026-11-07)."""
    alpha = next(n for n in store.load(tmp_path) if n.body == "alpha body")
    api.supersede(ctx_with_notes, alpha.id, author="sam", body="alpha, by sam")
    await user.open("/notes")
    assert await _hover(user, "commit-button") == "\n".join([
        "commit your 1 entry:", f"new · note {alpha.id[-10:]} · alpha body",
        "left for their writers: 2 entries"])


def test_a_long_tip_names_how_many_it_left_out():
    from symbion.gui import chrome
    lines = [f"line {i}" for i in range(chrome._TIP_LINES + 3)]
    assert chrome._capped(lines) == [*lines[:chrome._TIP_LINES - 1], "+4 more"]
    assert chrome._capped(lines[:chrome._TIP_LINES]) == lines[:chrome._TIP_LINES]


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
    assert _count(user).startswith("1 of 2 entries match")


async def test_a_search_narrows_the_filters_in_view(user: User, ctx_with_notes):
    """`keep` carries the page's filters into the search, and a search
    carries into the chips: both directions narrow."""
    await user.open("/notes?q=body&tag=blue")
    await user.should_see("beta")          # the hit is marked: "beta <mark>body</mark>"
    await user.should_not_see("alpha")
    assert _count(user).startswith("1 of 1 entry match")


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
    kept = api.supersede(ctx_with_notes, old, author="ada", body="beta revised").supersedes
    await user.open(f"/notes?id={kept}")
    await user.should_see("beta revised")
    await user.should_see(f"{kept} was superseded")
    await user.open(f"/notes?q={kept.rsplit('-', 1)[1]}")
    await user.should_see("beta revised")
    assert _count(user).startswith("1 of 2 entries match")
    # and says why it came: the search showed the current row and nothing
    # of the id that brought it (the user, 2026-10-09)
    (said,) = user.find(marker="superseded-by").elements
    assert said.text == f"{kept} was superseded; this is its current entry"


async def test_shift_enter_adds_a_note_and_keeps_the_composer_open(
        user: User, ctx_with_notes, monkeypatch):
    """The user, 2026-10-01: shift+enter adds another note. ⌘Enter reloads
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
    # NiceGUI 3.19 sends a reload as `history.go(0)`, and the fixture sees it a loop turn later.
    await asyncio.sleep(0.1)
    assert dialog.value and not body.elements.pop().value and not reloads
    dialog.close()                                    # Esc: the page shows them
    for _ in range(100):
        if reloads:
            break
        await asyncio.sleep(0.02)
    assert reloads == [1]


async def test_a_cited_tail_opens_its_row(user: User, ctx_with_notes):
    """Agents cite a row by its tail, and `show` takes one; /notes?id=<tail>
    read "0 notes", so a link built from a cited tail opened an empty page."""
    old = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    await user.open(f"/notes?id={old.split('-', 2)[2]}")
    await user.should_see("beta body")
    assert user.find(marker="page-title").elements.pop().text == f"bug {old.split('-', 2)[2]}"
    kept = api.supersede(ctx_with_notes, old, author="ada", body="beta revised").supersedes
    await user.open(f"/notes?id={old.split('-', 2)[2]}")
    await user.should_see("beta revised")                # an edit keeps the row's id
    await user.open(f"/notes?id={kept.split('-', 2)[2]}")
    await user.should_see(f"{kept} was superseded")


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
    await user.should_see("2 entries end in -abc")


async def test_a_notes_own_view_lists_its_earlier_versions(user: User, ctx_with_notes):
    """Every version is kept; the GUI showed only the last one."""
    first = next(n.id for n in store.load(ctx_with_notes.store_dir) if n.body == "beta body")
    mid = api.supersede(ctx_with_notes, first, author="ada", body="beta two").id
    last = api.supersede(ctx_with_notes, mid, author="ada", body="beta three").id
    await user.open(f"/notes?id={last}")
    await user.should_see("earlier versions (2)")
    user.find(marker="history").click()           # an expansion renders its body lazily
    await user.should_see("beta two")
    await user.should_see("beta body")
    assert len(parts(user, "note-edit")) == 1     # the current row's only


async def test_earlier_versions_show_their_target(user: User, ctx_with_notes):
    """A rename is a new version whose only change is the target, and the
    earlier versions were drawn without one: the change was invisible."""
    ctx = ctx_with_notes
    nid = api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "old-name"}},
                  author="ada").id
    api.commit(ctx, "c", everyone=True)
    api.rename_target(ctx, "item", "old-name", "new-name", author="ada")
    head = next(n for n in store.heads(store.load(ctx.store_dir)) if n.id == nid)
    await user.open(f"/notes?id={head.id}")
    await user.should_see("new-name")
    user.find(marker="history").click()
    await user.should_see("old-name")


async def test_a_rows_page_marks_each_amendment_with_who_added_it(user: User, ctx_with_notes):
    """A seam, not a diff: most supersedes are appends, and a diff of one is
    the whole old body unchanged (the user, 2026-10-08). On the row's page
    only, for now."""
    ctx = ctx_with_notes
    first = next(n.id for n in store.load(ctx.store_dir) if n.body == "beta body")
    api.commit(ctx, "c", everyone=True)
    mid = api.supersede(ctx, first, author="sam", append_body="gamma line").id
    api.commit(ctx, "c", everyone=True)
    last = api.supersede(ctx, mid, author="kim", prepend_body="new lead").id
    await user.open(f"/notes?id={last}")
    # .elements is a set: summary.seams' tests hold the order
    assert {e.text for e in parts(user, "seam")} == {
        "kim added the above just now", "sam added just now"}
    for text in ("new lead", "beta body", "gamma line"):
        await user.should_see(text)
    user.find(marker="history").click()
    await user.should_see("earlier versions (2)")
    await not_see(user, "diff-del")
    await not_see(user, "diff-ins")
    await user.open("/notes")
    await not_see(user, "seam")


async def test_earlier_versions_show_an_edit_as_a_diff(user: User, ctx_with_notes):
    """A replacement (an old-style resolve's "Fixed in …") stays the old
    card whole: a diff of it is all red and all green."""
    ctx = ctx_with_notes
    nid = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                        "body": "a kept paragraph stays\n\nthe old wording"}, author="ada").id
    api.commit(ctx, "c", everyone=True)
    mid = api.supersede(ctx, nid, author="sam",
                        body="a kept paragraph stays\n\nthe new wording").id
    api.commit(ctx, "c", everyone=True)
    last = api.supersede(ctx, mid, author="kim", body="Fixed in 1234abc.").id
    await user.open(f"/notes?id={last}")
    user.find(marker="history").click()
    await user.should_see("sam edited this just now")
    (gone,) = parts(user, "diff-del")
    (came,) = parts(user, "diff-ins")
    assert "the old wording" in gone.content and "the new wording" in came.content


async def test_an_earlier_version_says_what_the_next_changed(user: User, ctx_with_notes):
    """A version that changed one path in a long `checked` and nothing else
    showed both versions whole, and nothing said what differed (the user,
    2026-10-09)."""
    ctx = ctx_with_notes
    nid = api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "w"},
                        "checked": "pytest tmp/a.py -k b", "result": "3 passed",
                        "measurements": {"passed": 3, "failed": 1}}, author="ada").id
    api.commit(ctx, "c", everyone=True)
    last = api.supersede(ctx, nid, author="sam", checked="pytest tests/a.py -k b",
                         add_measurements={"failed": 2}, tags=["t"]).id
    await user.open(f"/notes?id={last}")
    user.find(marker="history").click()
    (said,) = parts(user, "changes")
    assert said.text == "sam edited this just now: checked, failed 1 → 2, +#t"
    verdicts = [e.content for e in parts(user, "note-verdict") if e.content]
    assert verdicts == ["checked: pytest <del>tmp/a.py </del><ins>tests/a.py </ins>"
                        "-k b   →   result: 3 passed"]


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
    (snip,) = parts(user, "note-snippet")
    assert snip.content.startswith("…")
    assert "<mark>needle</mark>" in snip.content


async def test_the_header_search_keeps_the_filters_in_view(user: User, ctx_with_notes):
    await user.open("/notes?kind=bug")
    user.find(marker="search").type("widget").trigger("keydown.enter")
    await user.should_see("Bugs matching “widget”")
    assert _count(user).startswith("1 of 1 entry match")


async def test_notes_with_no_filters_shows_everything(user: User, ctx_with_notes):
    await user.open("/notes")
    await user.should_see("alpha body")
    await user.should_see("beta body")


async def test_a_stale_bookmark_renders_rather_than_erroring(user: User, ctx_with_notes):
    await user.open("/notes?tag=nonexistent&utm_source=x")
    await user.should_see(marker="result-count")
    assert _count(user) == "0 entries"


async def test_an_unknown_param_does_not_silently_widen_the_result(
        user: User, ctx_with_notes):
    """The other direction of the dropped-param rule. Dropping utm_source must
    not also drop the tag beside it -- that would render every note and read
    as success."""
    await user.open("/notes?tag=red&utm_source=x")
    await user.should_see(marker="result-count")
    assert _count(user) == "1 entry"
    await user.should_not_see("beta body")


async def test_tab_title_leads_with_the_project_name(user: User, ctx_with_notes):
    """Several stores open in several tabs; the tab strip truncates from the
    right, so the project name goes first and the page follows. In the
    installed app's tabbed window, one store's, the tab is the page alone: a
    search's text in it ends no script. `_client`: see the first test."""
    await user.open("/notes?q=a</script>b")
    assert user.client.resolve_title() == \
        f"{ctx_with_notes.cfg.project_root.name} · Entries matching “a</script>b” · symbion"
    assert 'if (matchMedia("(display-mode: tabbed)").matches) ' \
        'document.title = "Entries matching \\u201ca\\u003c/script>b\\u201d"</script>' in user._client.head_html


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
    /object exists rather than being another canned filter.

    The page names the object, so a row ON it leaves its target out; a row
    that only refers to it names its own. Without it, that row's "N commits
    since" read as the page's file (2026-10-05)."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "other"},
                  "body": "points at widget", "refs": [{"type": "item", "name": "widget"}]},
            author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "widget"},
                  "body": "on widget"}, author="ada")
    build_page(ctx, author="ada")

    await user.open("/object?type=item&name=widget")
    await user.should_see("points at widget")
    assert [e.text for e in parts(user, "note-target")] == ["item: other"]


async def test_object_view_handles_a_name_with_a_slash(user: User, repo, tmp_path):
    """render_note builds these hrefs for every file/item target, so the
    round trip through the query string is the common case, not an edge one."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "src/x.py"},
                  "body": "slashed target"}, author="ada")
    build_page(ctx, author="ada")

    await user.open("/object?type=item&name=src%2Fx.py")
    await user.should_see("slashed target")




async def test_a_recent_board_holds_young_rows_and_those_current_at_head(
        user: User, repo, tmp_path):
    """The 25 newest checks of any age filled the board, nearly all `behind`
    (the user, 2026-10-02). An old row still current at HEAD stays: in a
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


async def test_a_board_past_its_cap_keeps_the_newest_and_every_due_row(
        user: User, repo, tmp_path, monkeypatch):
    """`/` drew every open row (the user, 2026-10-09). A board now draws
    its newest few, and a row due soon or past due wherever it falls: a cut
    must not hide a reminder. On a target's board: on `/`, a row due soon
    has a board of its own."""
    from datetime import date
    monkeypatch.setitem(build_page.__globals__, "BOARD_ROWS", 2)   # why: the PAGE_ROWS test
    ctx = api.resolve(str(tmp_path))
    for body, due in (("oldest, due today", date.today().isoformat()), ("second", None),
                      ("third", None), ("newest", None)):
        api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "widget"}, "body": body,
                      **({"due": due} if due else {})}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/targets")
    for body in ("newest", "third", "oldest, due today"):
        await user.should_see(body)
    await user.should_not_see("second")
    (aside,) = user.find(marker="board-aside").elements
    assert (aside.text, aside.props["href"]) == ("· 1 more · 4 entries",
                                                 "/object?type=item&name=widget")
    (count,) = [e for e in user.find(marker="board-count").elements   # title: an h2, or its link
                if "item: widget" in {getattr(d, "text", None)
                                      for d in e.parent_slot.parent.default_slot.children[0]
                                      .descendants(include_self=True)}]
    assert count.text == "3"


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


def _board_ids(user: User) -> dict[str, list[str]]:
    """Each board's title -> the ids of its cards, in page order."""
    out = {}
    for t in user.find(marker="board-title").elements:
        section = t.parent_slot.parent.parent_slot.parent
        out[t.text] = [e.props["data-id"] for e in sorted(section.descendants(), key=lambda e: e.id)
                       if "note-row" in e._markers]
    return out


async def test_the_summary_leads_with_what_waits_on_the_person(user: User, ctx_with_notes):
    """`waits` counts every open row another writer wrote last, which for the
    person is every agent's own task and parked idea. The person's page asks
    less: a question someone else wrote last, or the person's own entry that
    someone else answered (the user's pick, 2026-10-16). Each shows once."""
    def add(kind, body, author, **extra):
        return api.add(ctx_with_notes, {"kind": kind, "target": {"type": "project", "name": None},
                                        "body": body, **extra}, author=author).id
    asked = add("question", "is it blue", "sam")
    answered = add("question", "is it red", "sam")
    api.supersede(ctx_with_notes, answered, author="ada", append_body="no")
    hers = add("task", "ada's ask", "ada")
    api.supersede(ctx_with_notes, hers, author="sam", append_body="done: look?")
    sams = add("task", "sam's own", "sam")
    bet = add("prediction", "it holds", "ada", checked="the run")      # asks nothing until due
    api.supersede(ctx_with_notes, bet, author="sam", append_body="a first run held")
    due = add("task", "due thing", "ada", due=(date.today() + timedelta(days=1)).isoformat())
    await user.open("/")
    boards = _board_ids(user)
    assert sorted(boards["waiting on you"]) == sorted([asked, hers]), boards
    assert boards["due soon"] == [due], boards
    assert boards["open question"] == [answered], boards
    assert boards["open task"] == [sams], boards


def _q(ctx, body, author="sam", kind="question", options=None, **extra):
    return api.add(ctx, {"kind": kind, "target": {"type": "project", "name": None},
                         "body": body, **({"options": options} if options else {}), **extra},
                   author=author).id


def _head(tmp_path, nid):
    return next(n for n in store.heads(store.load(tmp_path)) if n.id == nid)


async def test_a_question_draws_a_button_per_option_default_first(user: User, ctx_with_notes,
                                                                  tmp_path):
    """A question's answers are its own data, the default first (the user,
    2026-10-16): a button per option, in order, the first marked, the field
    beside them. A pick goes below the question as the person, and the
    question stays open for its asker to act on and resolve."""
    q = _q(ctx_with_notes, "Ship the fix?",
           options=["ship it", "hold for a Chrome check", "ask again"])
    await user.open("/")
    buttons = sorted(parts(user, "answer-option"), key=lambda e: e.id)
    assert [b.text for b in buttons] == ["ship it", "hold for a Chrome check", "ask again"]
    assert [b.props.get("icon") for b in buttons] == ["check", None, None]
    assert [b.props.get("color") for b in buttons] == [None, None, None]
    assert parts(user, "answer-field")               # the field stays beside the buttons
    part(user, "answer-option", content="hold for a Chrome check").click()
    head = _head(tmp_path, q)
    assert (head.body.rstrip().endswith("hold for a Chrome check"), head.author,
            store.read_status(head)) == (True, "ada", "open")
    await user.open("/")
    await not_see(user, "answer-row")       # ada wrote the newest word


async def test_a_question_without_options_takes_typed_words_only(user: User, ctx_with_notes,
                                                                 tmp_path):
    """Every question written under the old convention is one of these: its
    `Default: … Flip:` prose stays, no button is parsed out of it, and the
    person answers in text."""
    bare = _q(ctx_with_notes, 'Default: port 80. Flip: say "8080".')
    await user.open("/")
    await not_see(user, "options")
    assert parts(user, "answer-row") and not parts(user, "answer-option")
    part(user, "answer-field").type("8080").trigger("keydown.enter")
    assert _head(tmp_path, bare).body.rstrip().endswith("8080")
    assert _head(tmp_path, bare).author == "ada"


async def _reloaded(user: User, by=None) -> None:
    """A card's write reloads the page: `ui.navigate.reload()`, as the `user`
    fixture runs it, opens the page anew in the same tab. `by` is the write."""
    old = user.client
    (by or user.navigate.reload)()
    for _ in range(200):
        if user.client is not old:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the page did not reload")


def _composer_closed(user: User) -> None:
    (d,) = [d for d in user.find(ui.dialog).elements
            if any("sb-new-card" in c.classes for c in d.default_slot.children)]
    d.close()


def _the(user: User, marker: str):
    (e,) = user.find(marker=marker).elements
    return e


async def test_a_draft_closed_with_esc_outlives_a_reload(user: User, ctx_with_notes):
    """A star, resolve or answer on any card reloads the page, and the
    composer's draft came back empty (audit, 2026-10-28)."""
    await user.open("/")
    user.find(marker="new-note").click()
    _the(user, "note-body").value = "half a thought #wip"
    _the(user, "note-kind-select").value = "task"
    _the(user, "note-due").value = "2026-12-01"
    _composer_closed(user)
    await _reloaded(user)
    user.find(marker="new-note").click()
    assert _the(user, "note-body").value == "half a thought #wip"
    assert (_the(user, "note-kind-select").value, _the(user, "note-due").value) == \
        ("task", "2026-12-01")
    assert _the(user, "note-tags").value == "wip"


async def test_a_draft_added_does_not_come_back(user: User, ctx_with_notes):
    await user.open("/")
    user.find(marker="new-note").click()
    _the(user, "note-body").value = "kept, then added"
    _composer_closed(user)
    user.find(marker="new-note").click()
    await _reloaded(user, user.find(marker="note-add").click)
    assert "kept, then added" in [n.body for n in store.load(ctx_with_notes.store_dir)]
    user.find(marker="new-note").click()
    assert _the(user, "note-body").value == ""


async def test_an_answer_typed_outlives_a_reload(user: User, ctx_with_notes):
    _q(ctx_with_notes, "which port?")
    await user.open("/")
    part(user, "answer-field").type("8080")
    await _reloaded(user)
    await see(user, "answer-field")
    assert part(user, "answer-field").value == "8080"


async def test_an_answer_sent_is_not_typed_again_at_its_next_turn(user: User, ctx_with_notes):
    q = _q(ctx_with_notes, "which port?")
    await user.open("/")
    await _reloaded(user, lambda: part(user, "answer-field").type("8080")
                    .trigger("keydown.enter"))
    api.supersede(ctx_with_notes, q, author="sam", append_body="and the next one?")
    await _reloaded(user)
    assert part(user, "answer-field").value == ""


async def test_a_question_not_waiting_on_the_person_lists_its_options(user: User,
                                                                      ctx_with_notes):
    """Where no buttons draw (the person's own question, one they answered),
    the options show as a meta row, as measurements do: the answer below
    reads against its alternatives, and the card shows what `show` prints."""
    _q(ctx_with_notes, "Mine?", author="ada", options=["keep", "drop"])
    answered = _q(ctx_with_notes, "Theirs?", options=["yes", "no"])
    api.supersede(ctx_with_notes, answered, author="ada", append_body="no")
    await user.open("/notes")
    await user.should_see("Mine?")
    await not_see(user, "answer-row")
    rows = sorted(parts(user, "options"), key=lambda e: e.id)
    # One label with the CLI's separator: three phrases parted by gaps alone
    # read as one run of words in a browser shot (2026-10-17).
    assert len(rows) == 2
    assert sorted(r.text for r in rows) == ["options: keep | drop", "options: yes | no"]


async def test_only_a_question_someone_else_wrote_last_takes_answers(user: User,
                                                                     ctx_with_notes):
    _q(ctx_with_notes, "Default: yes. sam's task", kind="task")
    _q(ctx_with_notes, "Default: yes. ada asks herself", author="ada")
    closed = _q(ctx_with_notes, "Default: yes. settled")
    api.supersede(ctx_with_notes, closed, author="sam", status="resolved")
    await user.open("/notes")
    await user.should_see("settled")
    await user.should_not_see(marker="answer-row")


async def test_someone_elses_options_on_the_persons_question_draw_no_answers(user: User,
                                                                           ctx_with_notes):
    """An agent's options on the person's own question drew the person
    buttons to answer it, while `summary.asks` no longer counted it as
    waiting on them (2026-10-22). Text below it still does."""
    own = _q(ctx_with_notes, "Mine?", author="ada")
    api.supersede(ctx_with_notes, own, author="sam", options=["keep", "drop"])
    await user.open("/notes")
    await user.should_see("options: keep | drop")
    await not_see(user, "answer-row")
    api.supersede(ctx_with_notes, own, author="sam", append_body="keep, I think")
    await user.open("/notes")
    await see(user, "answer-row")


async def test_no_resolve_ring_beside_a_questions_answers(user: User, ctx_with_notes):
    """The ring beside the answer buttons closed the question with no answer
    below it, and the pick was lost (2026-10-24). The asker acts on the
    answer and resolves the question; the edit dialog still closes it."""
    q = _q(ctx_with_notes, "Ship the fix?", options=["ship it", "hold"])

    def rings_on(nid):
        (card,) = [e for e in cards(user) if e._props.get("data-id") == nid]
        return parts(user, "note-resolve", card=card)

    await user.open("/notes")
    await see(user, "answer-row")
    bug = next(n.id for n in store.heads(store.load(ctx_with_notes.store_dir)) if n.kind == "bug")
    assert (len(rings_on(q)), len(rings_on(bug))) == (0, 1)
    part(user, "answer-option", content="hold").click()
    await user.open("/notes")
    await not_see(user, "answer-row")
    assert len(rings_on(q)) == 1         # ada's word is in: the ring is back for the asker's view


def _peek(user: User, nid: str) -> None:
    """What the page's script sends for a plain click on a card's text, or j/k."""
    UserInteraction(user, {user.client.layout}, None).trigger("sbPeek", {"id": nid})


async def test_a_peek_shows_the_whole_entry_beside_the_page(user: User, ctx_with_notes):
    """A card's text led to the entry's own page, and back to the board took
    a Back and a scroll (the user's pick, 2026-10-16). The peek is that page's
    entry, beside the board: the whole body, and its earlier versions."""
    long = "start " + "word " * 80 + "the end"
    nid = _q(ctx_with_notes, long, kind="task")
    api.supersede(ctx_with_notes, nid, author="ada", append_body="and a reply")   # a version
    await user.open("/")
    await user.should_not_see("the end")                  # the board clips it
    _peek(user, nid)
    with user.scope(marker="peek"):
        await user.should_see("the end")
        await user.should_see("and a reply")
        await user.should_see(marker="history")


async def test_a_peek_of_an_earlier_version_shows_its_entry(user: User, ctx_with_notes):
    nid = _q(ctx_with_notes, "first words", kind="task")
    earlier = api.supersede(ctx_with_notes, nid, author="ada", body="second words").supersedes
    assert earlier                                  # ada's edit of sam's entry keeps one
    await user.open("/")
    _peek(user, earlier)
    with user.scope(marker="peek"):
        await user.should_see(marker="note-row")             # the event is handled async
        first = min(user.find(marker="note-row").elements, key=lambda e: e.id)   # then history
        assert first.props["data-id"] == nid
    _peek(user, "999999-fff")
    with user.scope(marker="peek"):
        await user.should_see("no entry 999999-fff in this store")


async def test_the_pick_is_lit_only_while_the_peek_is_open(user: User, ctx_with_notes):
    """The user, 2026-10-16: the peek's highlight stayed on its card after
    the panel was gone. The card keeps `sb-picked`, j/k's place; the layout
    carries `sb-peeking` while the peek shows, and the highlight's rule needs
    both. Every way to close the peek (its button, Esc, a click beside it)
    sets the drawer's value."""
    nid = _q(ctx_with_notes, "look at me", kind="task")
    await user.open("/")
    layout = user.client.layout
    assert "sb-peeking" not in layout.classes
    _peek(user, nid)
    await _until(lambda: "sb-peeking" in layout.classes)
    user.find(marker="peek-close").click()
    await _until(lambda: "sb-peeking" not in layout.classes)
    # the peek's keys read the same class: under scrollbar-gutter, the drawer's
    # left edge read a closed peek as open, and x resolved its hidden entry
    from symbion.gui import chrome
    assert ".q-layout.sb-peeking" in chrome._PEEK_JS


def _finds(roots, selector) -> bool:
    """Whether `selector`, descendant parts each a `.class` or the tag
    `input`, finds an element under `roots`: a ui.input draws its <input>
    inside it, a ui.textarea a <textarea>."""
    *outer, last = selector.split()
    for step in outer:
        roots = [e for r in roots for e in (r, *r.descendants())
                 if step[1:] in e.classes or step[1:] in drawn(e)]
    return any((isinstance(e, ui.input) and not isinstance(e, ui.textarea)) if last == "input"
               else (last[1:] in e.classes) for r in roots for e in (r, *r.descendants())) \
        or any(last.lstrip(".") in drawn(e) for r in roots for e in (r, *r.descendants()))


async def test_every_key_and_palette_action_presses_a_drawn_element(
        user: User, ctx_with_notes, tmp_path_factory):
    """The keys and the palette's actions click through CSS classes, and a
    renamed class turned its key off with no error: the script's
    querySelector finds nothing and returns (a review, 2026-10-17). Each
    selector is read from the code, so a new key or action is checked here
    with no edit. The peek's keys look inside the peek."""
    from symbion.gui import chrome, palette
    maps = re.findall(r"const (sb(?:Peek)?Keys) = \{(.*?)\};", chrome._KEYS_JS + chrome._PEEK_JS,
                      re.S)
    wanted = {(".sb-peek " if name == "sbPeekKeys" else "") + s
              for name, body in maps for s in re.findall(r"'(\.[^']+)'", body)}
    wanted |= {a[2] for a in palette._ACTIONS}
    assert len(wanted) >= 13, f"the scan read {sorted(wanted)}: the pattern broke"
    _commit_with_origin(ctx_with_notes, tmp_path_factory)            # push is live
    nid = _q(ctx_with_notes, "Keep it?", options=["keep it", "drop it"])   # sam asks ada
    _q(ctx_with_notes, "ada's own", author="ada", kind="note")       # commit and undo are live
    await user.open("/")
    _peek(user, nid)
    await _until(lambda: "sb-peeking" in user.client.layout.classes)
    with user.scope(marker="peek"):
        await see(user, "answer-field")
    missing = sorted(s for s in wanted if not _finds([user.client.layout], s))
    # A question waiting on ada draws no ring (2026-10-24): its key is checked on the bug.
    _peek(user, next(n.id for n in store.heads(store.load(ctx_with_notes.store_dir))
                     if n.kind == "bug"))
    with user.scope(marker="peek"):
        await user.should_see("beta body")
    missing = [s for s in missing if not _finds([user.client.layout], s)]
    assert not missing, missing


async def test_the_peek_lives_in_the_url(user: User, ctx_with_notes, monkeypatch):
    """The peek opened and closed beside history: a phone's Back left the
    page under it, a reload lost it, and its "open its page" was a button a
    middle click could not open (the GUI review, 2026-11-07). The history
    steps run in the client (checked in a browser); here, what the page asks."""
    from nicegui.outbox import Outbox
    sent, emit = [], Outbox._emit

    async def record(self, message):
        if message[1] == "run_javascript" and "sbPeek" in message[2]["code"]:
            sent.append(message[2]["code"])
        await emit(self, message)

    monkeypatch.setattr(Outbox, "_emit", record)
    bug = next(n.id for n in store.heads(store.load(ctx_with_notes.store_dir)) if n.kind == "bug")
    await user.open("/")
    await _until(lambda: "sbPeekFromHash()" in sent)     # on connect: a #peek= link opens it
    _peek(user, bug)
    await _until(lambda: f'sbPeekHash("{bug}")' in sent)  # the outbox sends it a turn later
    (page,) = user.find(marker="peek-page").elements
    assert page.props["href"] == f"/notes?id={bug}"       # a link: a middle click opens a tab
    user.find(marker="peek-close").click()
    await _until(lambda: "sbPeekUnhash()" in sent)


async def test_a_write_in_the_peek_reloads_the_page_and_peeks_again(user: User, ctx_with_notes,
                                                                    tmp_path):
    """The user, 2026-10-16: a question answered in the peek stayed on the
    board, since a write there redrew the peek alone. It reloads the page, as
    a card's write does, and the page opens the peek on the entry again."""
    nid = _q(ctx_with_notes, "Keep it?", options=["keep it", "drop it"])
    await user.open("/")
    assert _board_ids(user)["waiting on you"] == [nid]
    first = user.client
    _peek(user, nid)
    with user.scope(marker="peek"):
        await see(user, "answer-option")
        part(user, "answer-option", content="keep it").click()
    await _until(lambda: user.client is not first)
    assert _head(tmp_path, nid).body.rstrip().endswith("keep it")
    boards = _board_ids(user)
    assert "waiting on you" not in boards and boards["open question"] == [nid], boards
    await _until(lambda: "sb-peeking" in user.client.layout.classes)
    with user.scope(marker="peek"):
        await user.should_see("keep it")


async def test_the_summary_shows_the_last_wrap(user: User, ctx_with_notes):
    """The handoff the last session left is the first thing `summary` prints
    to an agent; the person's page showed none of it."""
    await user.open("/")
    assert "last wrap" not in _board_ids(user)
    api.commit(ctx_with_notes, "Wrapped the importer\n\nNext: the dry run.", everyone=True,
               wrap=True)
    await user.open("/")
    assert "last wrap" in _board_ids(user)
    await user.should_see("Wrapped the importer")
    await user.should_see("Next: the dry run.")


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
    user.find(marker="arc-edit").click()
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
    """A card part's links, or else a server element's."""
    return {e.props.get("href") for e in parts(user, marker) or user.find(marker=marker).elements}


async def test_a_chip_in_a_filtered_view_narrows_it(user: User, ctx_with_notes):
    """A chip dropped the current filters instead of adding one (href() got
    one param); the spec: clicking it from a filtered view narrows further.
    Off the filtered view a chip starts a filter."""
    await user.open("/notes?kind=note")
    assert _hrefs(user, "tag-red") == {"/notes?kind=note&tag=red"}
    await user.open("/")                       # the open bug sits on a board
    assert _hrefs(user, "tag-blue") == {"/notes?tag=blue"}


async def test_a_page_has_one_filled_button(user: User, ctx_with_notes):
    """The design system: one filled primary per surface. /arcs had two, New
    entry and Create arc (the GUI review, 2026-11-07)."""
    for path in ("/", "/notes", "/targets", "/tags", "/arcs", "/docs", "/map"):
        await user.open(path)
        with user:
            filled = [b.text for b in ElementFilter(kind=ui.button) if b.props.get("unelevated")]
        assert len(filled) == 1, (path, filled)


async def test_enter_that_ends_an_ime_word_acts_on_nothing(user: User, ctx_with_notes):
    """An IME's Enter ends a word, and the answer field and the search box
    acted on it: a half-typed answer was written (the GUI review, 2026-11-07).
    Each acts only on an Enter that ends no word; Safari sends that one after
    compositionend, as keyCode 229. The guards run in the client: a real IME
    is a person's check. The drop's name field: tests/test_gui_drop.py."""
    from symbion.gui.chrome import _ENTER
    assert _ENTER == "!e.isComposing && e.keyCode !== 229"
    t = template()
    assert '@keydown.enter="answer"' in t
    assert 'if (!e.isComposing && e.keyCode !== 229) this.act("answer", this.typed)' in t
    await user.open("/")
    (box,) = user.find(marker="search").elements
    assert [l.js_handler for l in box._event_listeners.values() if "keydown" in l.type.lower()] \
        == [f"(e) => {{ if ({_ENTER}) emit(e.target.value) }}"]


async def test_an_entrys_page_is_named_for_the_entry(user: User, ctx_with_notes):
    """Its title and tab read `id=…`, alike for every entry in tabs, history
    and bookmarks, over "1 entry" (the GUI review, 2026-11-07). The crumbs
    say where it sits, and the tab leads with its kind and first words."""
    bug = store.query(store.load(ctx_with_notes.store_dir), kind="bug")[0]
    await user.open(f"/notes?id={bug.id}")
    (h1,) = user.find(marker="page-title").elements
    assert h1.text == f"bug {bug.id.split('-', 2)[2]}"
    assert _crumbs(user) == ["All entries", "›", h1.text]
    # the separator is a mark, so no word a screen reader reads (the design critique, 2026-11-07)
    (row,) = user.find(marker="crumbs").elements
    assert row.default_slot.children[1].props["aria-hidden"] == "true"
    assert user.client.resolve_title() == \
        f"{ctx_with_notes.cfg.project_root.name} · bug · beta body · symbion"
    await user.should_not_see(marker="result-count")
    await user.open(f"/notes?id={bug.id[-3:]}")         # a tail, as rows cite one
    assert user.find(marker="page-title").elements.pop().text == h1.text
    await user.open("/notes?id=no-such")
    assert user.find(marker="page-title").elements.pop().text == "no entry no-such"
    await user.should_see("No entry has an id that is or ends in “no-such”.")
    await user.should_not_see(marker="result-count")
    # an id that is there, under a filter it fails, is no unknown id
    await user.open(f"/notes?id={bug.id}&kind=note")
    assert user.find(marker="page-title").elements.pop().text.startswith("id=")
    await user.should_not_see(marker="no-such-id")


async def test_a_chip_or_a_search_on_a_notes_own_page_starts_a_filter(
        user: User, ctx_with_notes):
    """One note's page is `?id=`: its chips and the search kept the id, so
    the kind chip opened the same one row under `id=… · kind=bug`."""
    bug = store.query(store.load(ctx_with_notes.store_dir), kind="bug")[0]
    await user.open(f"/notes?id={bug.id}")
    assert _hrefs(user, "note-kind") == {"/notes?kind=bug"}
    assert _hrefs(user, "tag-blue") == {"/notes?tag=blue"}
    user.find(marker="search").type("alpha").trigger("keydown.enter")
    await user.should_see("Entries matching “alpha”")   # the new page; then the whole title
    assert {e.text for e in user.find(marker="page-title").elements} == {"Entries matching “alpha”"}


async def test_each_filter_comes_off_by_its_own_chip(user: User, ctx_with_notes):
    """"clear filters" took every filter at once, and an active filter's chip
    on a card linked to the page it was on (the GUI review, 2026-11-07). Each
    filter is a chip whose × takes off that one; on a card, its chip is a
    label; the title says it in words."""
    await user.open("/notes?kind=bug&tag=blue&q=beta")
    chips = {e.default_slot.children[0].text: e.props["href"]
             for e in user.find(marker="filter-chip").elements}
    assert chips == {"kind bug": "/notes?tag=blue&q=beta", "tag blue": "/notes?kind=bug&q=beta",
                     "search beta": "/notes?kind=bug&tag=blue"}, chips
    (clear,) = user.find(marker="clear-filters").elements
    assert clear.props["href"] == "/notes"
    assert _hrefs(user, "note-kind") == {None}, "the view's own kind: a label"
    assert _hrefs(user, "tag-blue") == {None}
    (foot,) = [e._props["card"]["foot"] for e in cards(user)]
    assert next(c["h"] for c in foot if c["t"] == "open") == \
        "/notes?kind=bug&tag=blue&q=beta&status=open", "a filter it lacks still narrows"
    (h1,) = user.find(marker="page-title").elements
    assert h1.text == "Bugs tagged #blue matching “beta”"
    await user.open("/notes")
    await user.should_not_see(marker="filter-chips")


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
    texts = {e.text for e in parts(user, "note-ref")}
    assert len(texts) == 1 and len(texts.pop().split(":", 1)[1]) == 7, texts


async def test_a_new_arc_is_mixed_as_the_cli_makes_one(user: User, repo, tmp_path):
    """The form picked `item`, the CLI `mixed` (the design critique,
    2026-11-07). A scope only names what an arc's boxes are on."""
    ctx = api.resolve(str(tmp_path))
    build_page(ctx, author="ada")
    await user.open("/arcs")
    user.find(marker="new-arc-name").type("camp")
    user.find(marker="new-arc-create").click()
    (act,) = store.load_arcs(tmp_path)
    assert (act.name, act.target_scope) == ("camp", "mixed")


async def test_a_finished_arc_steps_back_on_the_index(user: User, repo, tmp_path):
    """A finished arc's count read as an active one's did (the design critique,
    2026-11-07): a finished one says done, in `good`, and steps back."""
    ctx = api.resolve(str(tmp_path))
    done = api.create_arc(ctx, "done", "", "item", author="ada")
    api.seed(ctx, done.id, "item", ["a"], author="ada")
    live = api.create_arc(ctx, "live", "", "item", author="ada")
    api.seed(ctx, live.id, "item", ["b"], author="ada")
    (box,) = store.arc_items(store.load(tmp_path), done.id)
    api.supersede(ctx, box.id, author="ada", status="resolved")
    build_page(ctx, author="ada")
    await user.open("/arcs")
    cards = sorted(user.find(marker="arc-card").elements, key=lambda e: e.id)
    counts = sorted(user.find(marker="arc-count").elements, key=lambda e: e.id)
    assert {(n.text, "sb-done" in c.classes) for c, n in zip(cards, counts)} == \
        {("1/1 done", True), ("0/1", False)}


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


async def test_an_arc_page_unarchives_and_describes_its_arc(user: User, repo, tmp_path):
    """The user, 2026-10-30: Unarchive where Archive sits, and a description
    field with Save beside rename."""
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "Old", "first", "mixed", author="ada").id
    api.archive_arc(ctx, aid)
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={aid}")
    user.find(marker="arc-unarchive").click()
    await user.should_see(marker="arc-archive")
    assert not store.load_arcs(tmp_path)[0].archived
    user.find(marker="arc-description").clear().type("second")
    user.find(marker="arc-describe").click()
    await user.should_see(marker="arc-archive")
    assert store.load_arcs(tmp_path)[0].description == "second"


async def test_an_arc_page_opens_on_its_boxes_with_its_form_behind_edit_arc(user: User, repo,
                                                                           tmp_path):
    """The design critique, 2026-11-07: rename, description, phases and
    Archive came before every box, Archive drawn in "look here". They wait
    behind Edit arc, and Archive carries no colour of its own."""
    ctx = api.resolve(str(tmp_path))
    aid = api.create_arc(ctx, "camp", "d", "mixed", author="ada").id
    build_page(ctx, author="ada")
    await user.open(f"/arc?id={aid}")
    await user.should_see(marker="arc-edit")
    await user.should_not_see(marker="arc-rename")
    user.find(marker="arc-edit").click()
    await user.should_see(marker="arc-rename")
    (archive,) = user.find(marker="arc-archive").elements
    assert "color" not in archive._props, archive._props


async def test_a_list_card_names_six_measurements_and_the_page_all(user: User, repo, tmp_path):
    """One card printed dozens of counters, most of them 0 (the design critique,
    2026-11-07): a list's card names the first six and how many more; the
    entry's page names every one."""
    ctx = api.resolve(str(tmp_path))
    nid = api.add(ctx, {"kind": "measurement", "target": {"type": "item", "name": "w"},
                        "checked": "a run", "result": "counted",
                        "measurements": {f"k{i}": i for i in range(9)}}, author="ada").id
    build_page(ctx, author="ada")
    await user.open("/notes")
    (meas,) = parts(user, "measurements")
    assert meas.d["items"] == [f"k{i}: {i}" for i in range(6)] + ["+3 more"]
    await user.open(f"/notes?id={nid}")
    (meas,) = parts(user, "measurements")
    assert meas.d["items"] == [f"k{i}: {i}" for i in range(9)]


async def test_the_summarys_run_boards_show_three_under_a_rule(user: User, repo, tmp_path):
    """Two "recent" boards took more than half the Summary (the design
    critique, 2026-11-07): each shows its three newest runs, under a rule
    that ends the boards to act on, and counts the rest."""
    ctx = api.resolve(str(tmp_path))
    ids = [api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "w"},
                         "checked": f"run {i}", "result": "ok"}, author="ada").id
           for i in range(5)]
    build_page(ctx, author="ada")
    await user.open("/")
    assert _board_ids(user)["recent check"] == ids[::-1][:3]
    await user.should_see(marker="runs-rule")
    await user.should_see("· 2 more")


async def test_a_read_only_summary_waits_on_the_owner(user: User, ctx_with_notes, monkeypatch):
    """A demo visitor read "waiting on you" about the store's owner (the
    design critique, 2026-11-07)."""
    nid = _q(ctx_with_notes, "Keep it?")
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")            # after the write: it refuses one
    await user.open("/")
    assert _board_ids(user)["waiting on the owner"] == [nid]


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
    for i, tags in enumerate((["flaky-test"], ["flaky-test"], ["flaky-tests"], ["other"])):
        api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": f"b{i}", "tags": tags}, author="ada")
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


async def test_a_read_only_page_that_fails_names_only_the_exceptions_type(
        user: User, ctx_with_notes, caplog, monkeypatch):
    """The public demo's visitors are not its owner: an exception's text can
    hold a path on the serving machine. The log keeps it."""
    from nicegui import ui
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")

    @ui.page("/boom")
    def boom():
        raise RuntimeError("/srv/private/store")

    r = await user.http_client.get("/boom")
    assert r.status_code == 500
    assert "RuntimeError" in r.text
    assert "/srv/private/store" not in r.text
    assert "/srv/private/store" in caplog.text
    caplog.clear()


async def test_a_read_only_refusal_names_no_path(user: User, ctx_with_notes, caplog,
                                                 monkeypatch):
    """A typed path outside the project is refused in words that name the
    resolved path and the project root, both on the serving machine (the
    security review, 2026-11-07). The demo's visitor sees neither; the log
    keeps them."""
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    root = str(ctx_with_notes.cfg.work_root)
    await user.open("/object?type=file&name=/etc/passwd")
    assert user.notify.contains("Read-only demo: refused"), user.notify.messages
    assert not any(root in str(m) for m in user.notify.messages)
    assert root in caplog.text
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
                      "body": f"entry {i}", "tags": ["t"]}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes?tag=t")
    await user.should_see("entry 2")
    await user.should_not_see("entry 0")                     # the oldest is the one cut
    assert _count(user) == "3 entries; newest 2 shown"       # named at the top too
    (more,) = user.find(marker="show-all").elements
    assert more.text == "+1 older not shown — show all"
    assert more.props["href"] == "/notes?tag=t&all=1"      # the filters survive it
    await user.open(more.props["href"])
    await user.should_see("entry 0")
    await user.should_not_see(marker="show-all")



async def test_targets_draws_a_page_of_cards_and_names_the_rest(user: User, repo, tmp_path,
                                                                 monkeypatch):
    """/targets capped each board and never the page: one board per file of
    a seeded arc, 1,500 cards in 7.8 s, on the event loop every visitor
    shares (audit 2026-10-28). A read-only serve ignores `all=1`, on /notes
    too."""
    monkeypatch.setitem(build_page.__globals__, "PAGE_ROWS", 3)
    ctx = api.resolve(str(tmp_path))
    for i in range(5):
        api.add(ctx, {"kind": "task", "target": {"type": "item", "name": f"t{i}"},
                      "body": f"entry {i}"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/targets")
    assert len(user.find(marker="board").elements) == 3
    (more,) = user.find(marker="show-all").elements
    assert (more.text, more.props["href"]) == ("+2 more targets not shown — show all",
                                               "/targets?all=1")
    await user.open("/targets?all=1")
    assert len(user.find(marker="board").elements) == 5
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open("/targets?all=1")
    assert len(user.find(marker="board").elements) == 3
    await user.should_not_see(marker="show-all")
    await user.open("/notes?all=1")
    assert len(user.find(marker="note-row").elements) == 3
    await user.should_not_see(marker="show-all")


async def test_an_objects_page_draws_a_page_of_entries_and_files_and_names_the_rest(
        user: User, repo, tmp_path, monkeypatch):
    """/object drew every entry, and a commit's page every file: 2,000 cards
    took 0.88 s, and a 2,000-file commit 14,161 elements, on the loop every
    visitor shares (audit 2026-11-02). Each draws a page, as /notes does,
    names the cut, and a read-only serve ignores `all=1`."""
    monkeypatch.setitem(build_page.__globals__, "PAGE_ROWS", 2)
    for i in range(3):
        (repo / f"f{i}.py").write_text("x\n")
    sha = _commit_all(repo, "three files")
    ctx = api.resolve(str(tmp_path))
    for i in range(3):
        api.add(ctx, {"kind": "note", "target": {"type": "commit", "name": sha},
                      "body": f"entry {i}"}, author="ada")
    build_page(ctx, author="ada")
    page = f"/object?type=commit&name={sha}"
    await user.open(page)
    assert len(user.find(marker="note-row").elements) == 2
    assert len(user.find(marker="commit-file").elements) == 2
    await user.should_see("3 files · +3 −0 · the entries on each; the first 2 shown")
    await user.should_see("3 entries on or referencing this object; 2 shown, open work first")
    assert {(m.text, m.props["href"]) for m in user.find(marker="show-all").elements} == {
        ("+1 more file not shown — show all", page + "&all=1"),
        ("+1 more entry not shown — show all", page + "&all=1")}
    await user.open(page + "&all=1")
    assert len(user.find(marker="note-row").elements) == 3
    assert len(user.find(marker="commit-file").elements) == 3
    await user.should_not_see(marker="show-all")
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open(page + "&all=1")
    assert len(user.find(marker="note-row").elements) == 2
    assert len(user.find(marker="commit-file").elements) == 2
    await user.should_not_see(marker="show-all")


async def test_an_arcs_page_draws_a_page_of_boxes_open_first_and_names_the_rest(
        user: User, repo, tmp_path, monkeypatch):
    """/arc drew every box: a seeded arc of 2,000 took 0.59 s on the loop
    every visitor shares (audit 2026-11-02). It draws a page, open boxes
    first; read-only, the rest are named and no link lifts the cap."""
    monkeypatch.setitem(build_page.__globals__, "PAGE_ROWS", 2)
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    first, *_ = api.seed(ctx, act.id, "item", ["alpha", "beta", "gamma"], author="ada")
    store.supersede(ctx.store_dir, first.id, status="resolved", author="ada")
    build_page(ctx, author="ada")
    page = f"/arc?id={act.id}"
    await user.open(page)
    assert len(user.find(marker="checklist-row").elements) == 2
    assert first.id not in {c._props["data-id"] for c in cards(user)}   # the done box is cut
    (more,) = user.find(marker="show-all").elements
    assert (more.text, more.props["href"]) == ("+1 more entry not shown — show all",
                                               page + "&all=1")
    await user.open(page + "&all=1")
    assert len(user.find(marker="checklist-row").elements) == 3
    await user.should_not_see(marker="show-all")
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open(page + "&all=1")
    assert len(user.find(marker="checklist-row").elements) == 2
    await user.should_not_see(marker="show-all")
    await user.should_see("+1 more entry not shown")


async def test_a_read_only_serve_draws_a_shorter_page(user: User, repo, tmp_path, monkeypatch):
    """A read-only serve renders a 100-row /notes in about twice the time
    of a 30-row one, and a visitor's tabs queue behind each other."""
    monkeypatch.setitem(build_page.__globals__, "DEMO_ROWS", 2)
    ctx = api.resolve(str(tmp_path))
    for i in range(3):
        api.add(ctx, {"kind": "task", "target": {"type": "item", "name": f"t{i}"},
                      "body": f"entry {i}"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    assert len(user.find(marker="note-row").elements) == 3
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open("/notes")
    assert len(user.find(marker="note-row").elements) == 2
    assert _count(user) == "3 entries; newest 2 shown"
    await user.open("/targets")
    assert len(user.find(marker="board").elements) == 2


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
    assert _nav_n(side) == "3"        # 1 on the board + 2


async def test_the_top_bar_names_the_writer_while_the_sidebar_hides(user: User, ctx_with_notes):
    """Below the drawer's breakpoint the sidebar and its badge sit behind the
    menu button, and the page still writes: the top bar names who, first."""
    await user.open("/")
    (name,) = user.find(marker="author-badge-top").elements
    assert name.text == "ada"
    assert "sb-narrow" in name.parent_slot.parent.classes


async def test_the_sidebar_opens_by_width_without_asking_the_browser(user: User, ctx_with_notes):
    """A drawer of value=None asks each new client whether it is open, and logs
    a traceback when no answer comes in 1 s: the visitor left, or the tab slept."""
    await user.open("/")
    (side,) = user.find(ui.left_drawer).elements
    assert side.value is False
    assert side.props["show-if-above"] is True


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


# ---- the user's GUI rows of 2026-10-01 ----

def _crumbs(user: User) -> list[str]:
    (row,) = user.find(marker="crumbs").elements
    return [e.text for e in row.default_slot.children]


async def test_the_top_bar_ends_in_the_pages_title(user: User, ctx_with_notes):
    """A title under the bar left a band of nothing above it. A "Notebook"
    crumb before each title was one level deep (the user, 2026-10-13)."""
    for path, title in (("/", "Summary"), ("/tags", "Tags"), ("/arcs", "Arcs"), ("/docs", "Docs"),
                        ("/map", "Map"),
                        ("/object?type=item&name=widget", "item: widget"),
                        ("/notes?kind=bug", "Bugs")):
        await user.open(path)
        (here,) = user.find(marker="page-title").elements
        assert here.text == title, path
        assert _crumbs(user) == [title], path
    (home,) = user.find(marker="place-home").elements
    assert [c.text for c in home.default_slot.children if isinstance(c, ui.label)] == ["Summary"]


async def test_docs_lists_the_markdown_files_entries_name(user: User, repo, tmp_path,
                                                         monkeypatch):
    """The user, 2026-10-16: "add Docs below Arcs in the sidebar and show all
    the documents we know about". Every markdown file an entry is on or
    cites: the repo's, then the store's docs/, the most named first, each with
    its entries and the open ones among them, and a read button where the
    viewer can show it; then those no entry names, but for an ignored one
    (the user, 2026-10-27). Past BOARD_ROWS, a link shows them all."""
    ctx = api.resolve(str(tmp_path))
    (repo / "README.md").write_text("# hi\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "Guide.MD").write_text("# guide\n")
    (repo / ".gitignore").write_text("secret.md\n")
    (repo / "secret.md").write_text("# not listed\n")
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "plan.md").write_text("# plan\n")
    (tmp_path / "docs" / "loose.md").write_text("# loose\n")
    for kind, typ, name, refs in (("task", "item", "README.md", []),
                                  ("note", "item", "x", [("item", "README.md")]),
                                  ("task", "item", "gone.md", []),
                                  ("note", "doc", "plan.md", []),
                                  ("task", "item", "x.py", [])):
        api.add(ctx, {"kind": kind, "target": {"type": typ, "name": name}, "body": "about it",
                      "refs": [{"type": t, "name": n} for t, n in refs]}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    await user.should_see(marker="place-docs")
    await user.open("/docs")
    await user.should_see("3 markdown files that entries name, the most named first; "
                          "then 2 that none do")
    got = {}
    for board in ("docs-repo", "docs-store"):
        with user.scope(marker=board):
            links = sorted(user.find(marker="doc-link").elements, key=lambda e: e.id)
            counts = sorted(user.find(marker="doc-count").elements, key=lambda e: e.id)
            got[board] = [(a.text, " ".join(c.text for c in n.default_slot.children))
                          for a, n in zip(links, counts)]
            # an ellipsis cuts a long name at 375 px: each name is its tip there
            assert all(a.props["data-tip-cut"] == a.text for a in links)
            got[board + " read"] = len(user.find(marker="file-open").elements)
    assert got == {"docs-repo": [("item: README.md", "2 · 1 open"), ("item: gone.md", "1 · 1 open"),
                                 ("sub/Guide.MD", "0 entries")],
                   "docs-repo read": 2,
                   "docs-store": [("plan.md", "1 entry"), ("loose.md", "0 entries")],
                   "docs-store read": 2}
    # Each icon is named for the file it reads: all were "read" (the GUI review, 2026-11-07).
    names = sorted(b.props.get("aria-label", "") for b in user.find(marker="file-open").elements)
    assert names and all(n.startswith("read ") and n != "read " for n in names), names
    assert any("plan.md" in n for n in names), names
    monkeypatch.setitem(build_page.__globals__, "BOARD_ROWS", 2)
    for i in range(2):
        api.add(ctx, {"kind": "note", "target": {"type": "doc", "name": f"d{i}.md"},
                      "body": "more"}, author="ada")
    # The rows past it are drawn hidden, for the search box's filter.
    past = lambda: sum("sb-past" in e.classes for e in user.find(marker="doc-row").elements)  # noqa: E731
    await user.open("/docs")
    with user.scope(marker="docs-store"):
        assert (len(user.find(marker="doc-link").elements), past()) == (4, 2)
        (more,) = user.find(marker="docs-more").elements
        assert (more.text, more.props["href"]) == ("+2 more", "/docs?all=1")
    await user.open("/docs?all=1")
    with user.scope(marker="docs-store"):
        assert (len(user.find(marker="doc-link").elements), past()) == (4, 0)
        await user.should_not_see(marker="docs-more")


async def test_the_filter_reaches_the_rows_of_docs_tags_and_arcs(user: User, repo, tmp_path):
    """The search box's filter (chrome.py, `sbFilter`) hides the cards that
    lack a word typed, and searches the store once it has hidden them all.
    /docs, /tags and /arcs drew no cards, so a first word there opened the
    store's search (the user, 2026-10-29): each marks its own rows."""
    ctx = api.resolve(str(tmp_path))
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "plan.md").write_text("# plan\n")
    api.add(ctx, {"kind": "note", "target": {"type": "doc", "name": "plan.md"}, "body": "x",
                  "tags": ["red"]}, author="ada")
    api.create_arc(ctx, "Now", "", "item", author="ada")
    build_page(ctx, author="ada")
    for path, marker in (("/docs", "doc-row"), ("/tags", "tag-chip"), ("/arcs", "arc-card")):
        await user.open(path)
        rows = user.find(marker=marker).elements
        assert rows and all("sb-filterable" in e.classes for e in rows), path


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
    """The user, 2026-10-01: the GUI put a note only on the project, or on
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
    assert sorted(typ.options) == ["commit", "doc", "item", "project", "thing"]   # an arc: its select
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
    assert _crumbs(user) == ["Arcs", "›", "Release"]        # the one page a level down
    user.find(marker="new-note").click()
    await user.should_see(marker="note-arc-select")
    assert user.find(marker="note-arc-select").elements.pop().value == aid


async def test_new_note_from_a_kind_view_starts_as_that_kind(user: User, ctx_with_notes):
    """The user, 2026-10-11: a note added from a kind= view should be that
    kind, and the button should say so. A kind this store lacks (a stale
    bookmark) leaves the default."""
    await user.open("/notes?kind=task")
    await user.should_see("New task")
    await user.should_not_see("New entry")
    user.find(marker="new-note").click()
    await user.should_see(marker="note-body")
    assert user.find(marker="note-kind-select").elements.pop().value == "task"
    await user.open("/notes?kind=nosuch")
    await user.should_see("New entry")
    user.find(marker="new-note").click()
    await user.should_see(marker="note-body")
    assert user.find(marker="note-kind-select").elements.pop().value == "note"


async def test_the_keys_list_names_the_shortcuts_and_the_kinds(user: User, ctx_with_notes):
    await user.open("/")
    user.find(marker="keys").click()
    await user.should_see(marker="keys-list")
    await user.should_see("!kind")
    await user.should_see("in an entry: sets its kind, one of note, decision, bug, task, "
                          "question, idea, check, measurement, prediction; its first letters do, while one kind "
                          "alone starts with them")


async def test_the_help_icon_opens_how_symbion_works(user: User, repo, tmp_path):
    """The user, 2026-10-15: every doc was written for agents at a CLI, and
    a person in the GUI needs the model. It opens from its own icon, under
    the keyboard's, in the markdown viewer, with this store's own kinds in
    place of the marker, each with its `when`: the words are the store's,
    written in symbion.toml for wherever they show (the user, 2026-10-17)."""
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nnote = {}\nchore = { status = true, when = "a small thing to tidy" }\n'
        'bug = { status = true, when = "a known-broken thing to return to; resolve when fixed" }\n')
    ctx = api.resolve(str(tmp_path))
    build_page(ctx, author="ada")
    await user.open("/")
    user.find(marker="help-open").click()
    await user.should_see(marker="file-view", retries=40)
    (view,) = user.find(marker="file-view").elements
    assert view.content.startswith("# How symbion works")
    assert "This store's kinds:\n\n- **note** (record)\n" in view.content
    assert "- **chore** (open until resolved): a small thing to tidy\n" in view.content
    # a default kind too: the file's own prose for it stands in the file alone
    assert "- **bug** (open until resolved): a known-broken thing to return to; resolve when fixed\n" \
        in view.content
    assert "<!-- kinds -->" not in view.content and "default kinds" not in view.content


async def test_a_kind_in_the_sidebar_says_what_it_is_for(user: User, repo, tmp_path):
    """The user, 2026-10-16: a kind's words are defined in symbion.toml and
    treated as its colour is. A sidebar kind's tooltip is its name and
    properties, then the `when` the store gives it, a default's by default;
    none for a kind with no `when`. The help's own prose for a default kind
    stays on the help page."""
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nnote = {}\nchore = { status = true, when = "a small thing to tidy" }\n'
        'bug = { status = true, when = "a known-broken thing to return to; resolve when fixed" }\n')
    ctx = api.resolve(str(tmp_path))
    build_page(ctx, author="ada")
    await user.open("/")
    tips = {}
    for mark in ("open-bug", "open-chore", "kind-note"):
        (item,) = user.find(marker=mark).elements
        tips[mark] = [[w.text for w in t.descendants() if isinstance(w, ui.label)]
                      for t in item.descendants() if isinstance(t, ui.tooltip)]
    assert tips == {"open-bug": [["bug · open until resolved",
                                  "A known-broken thing to return to; resolve when fixed"]],
                    "open-chore": [["chore · open until resolved", "A small thing to tidy"]],
                    "kind-note": []}


def test_the_help_file_holds_the_default_kinds():
    """The file is read on GitHub too, where the block stands as written: a
    line per default kind, in plain words, with that kind's properties. It
    drifts the day a default kind is added, removed or changes a property."""
    from symbion.gui.chrome import HELP, _KINDS_BLOCK, kind_line
    from symbion.kinds import DEFAULT_KINDS
    (block,) = _KINDS_BLOCK.findall(HELP.read_text(encoding="utf-8"))
    lines = [ln for ln in block.splitlines() if ln.startswith("- **")]
    assert [ln.split("**")[1] for ln in lines] == list(DEFAULT_KINDS)
    for ln, (label, k) in zip(lines, DEFAULT_KINDS.items()):
        assert ln.startswith(kind_line(label, k).split(": ")[0] + ": "), ln


async def test_the_sidebar_lists_the_newest_closed_rows(user: User, repo, tmp_path):
    """The user, 2026-10-01: "sidebar should show recently closed items"."""
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
    """The user, 2026-10-02: "parked items should be included in the
    sidebar (ideas are invisible today)". Under "parked", not "open": a
    parked row stays out of the open views."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "idea", "target": {"type": "project", "name": None},
                  "body": "someday"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    (side,) = user.find(marker="open-idea").elements
    assert side.props["href"] == "/notes?kind=idea&status=open"
    assert _nav_n(side) == "1"
    assert side.parent_slot.children[0].text == "parked"


async def test_the_sidebar_lists_the_kinds_with_no_status_under_records(
        user: User, repo, tmp_path):
    """The sidebar showed no kind without a status bit (2026-10-02). Such
    a kind has no open count: it shows its rows, and
    links to them."""
    ctx = api.resolve(str(tmp_path))
    for body in ("chose x", "chose y"):
        api.add(ctx, {"kind": "decision", "target": {"type": "project", "name": None},
                      "body": body}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    (side,) = user.find(marker="kind-decision").elements
    assert side.props["href"] == "/notes?kind=decision"
    assert _nav_n(side) == "2"
    assert side.parent_slot.children[0].text == "records"
    (note,) = user.find(marker="kind-note").elements
    assert _nav_n(note) == "0"


def test_the_viewer_reads_only_text_it_knows_inside_the_checkout(repo, tmp_path):
    """A target name is typed text: the viewer must not read through it. It
    shows markdown, and a file pygments names a language for."""
    from symbion.gui.notes import view_path
    ctx = api.resolve(str(tmp_path))
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("# A")
    (repo / "x.py").write_text("x = 1")
    (repo / "Makefile").write_text("all:")
    (repo / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path / "out.md").write_text("outside")
    for name in ("docs/a.md", "x.py", "Makefile"):
        assert view_path(ctx, name) == ((repo / name).resolve(), None), name
    for name in ("../out.md", str(tmp_path / "out.md"), "logo.png",
                 "gone.md", "docs", None):
        assert view_path(ctx, name) is None, name


def test_the_viewer_reads_a_doc_from_the_store_not_the_checkout(repo, tmp_path):
    """A `doc` name is a path within the store's docs/, and the checkout's
    file at the same path is another file."""
    from symbion.gui.notes import view_path
    ctx = api.resolve(str(tmp_path))
    for root in (repo, tmp_path):
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "a.md").write_text("# A")
    (tmp_path / "top.md").write_text("# the store's, not a doc")
    assert view_path(ctx, "a.md", "doc") == ((tmp_path / "docs" / "a.md").resolve(), None)
    assert view_path(ctx, "docs/a.md") == ((repo / "docs" / "a.md").resolve(), None)
    for name in ("../top.md", "../p/f"):
        assert view_path(ctx, name, "doc") is None, name


def test_the_viewer_refuses_symlinks_outside_the_checkout(repo, tmp_path, symlink):
    from symbion.gui.notes import view_path
    ctx = api.resolve(str(tmp_path))
    (tmp_path / "out.md").write_text("outside")
    symlink(repo / "link.md", tmp_path / "out.md")
    assert view_path(ctx, "link.md") is None


async def test_a_markdown_target_opens_in_a_dialog(user: User, repo, tmp_path):
    """The user, 2026-10-02: ".md files should be viewable in a modal
    markdown viewer". From the card's target and refs, and the file's page."""
    ctx = api.resolve(str(tmp_path))
    (repo / "README.md").write_text("# Read me\n\nthe *whole* file\n\n"
                                    "```mermaid\nflowchart LR\n  a --> b\n```\n")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "README.md"},
                  "body": "about it"}, author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "other.md"},
                  "body": "no such file"}, author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": [{"type": "item", "name": "README.md"}], "body": "cites it"},
            author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    assert len(parts(user, "file-open")) == 2    # a target, a ref; not other.md
    # with its chip, so a wrap keeps them together
    chips = [c for e in cards(user) for c in e._props["card"]["foot"] if c.get("view")]
    assert all(c["pair"] and "sb-chip" in c["c"] for c in chips), chips
    assert '<span v-if="c.pair" class="sb-pair">' in template()
    # Named for the file it reads: every read button on a Summary was named
    # "read", the file only in a tip no keyboard reached (the GUI review, 2026-11-07).
    assert all(c["view"]["tip"].startswith("read README.md") for c in chips), chips
    (read,) = re.findall(r'<q-btn v-if="c\.view".*?/>', template())    # `=>` holds a `>`
    assert ':aria-label="c.view.tip"' in read, read
    parts(user, "file-open")[0].click()
    await user.should_see(marker="file-view", retries=40)
    (view,) = user.find(marker="file-view").elements
    assert "the *whole* file" in view.content
    # a diagram, drawn in mermaid's dark theme (the user, 2026-10-27); the
    # fixture runs no JS, so the drawing itself was checked in a browser
    assert view.props["use-mermaid"]
    assert "```mermaid\n%%{init: {'theme': 'dark'}}%%\nflowchart LR" in view.content
    await user.open("/object?type=item&name=README.md")
    labels = [b.props.get("label") for b in user.find(marker="file-open").elements]
    assert labels == ["read the file"]                      # the page's own
    assert len(parts(user, "file-open")) == 1               # the ref's, on its card


async def test_a_file_only_the_default_branch_holds_opens_from_there(user: User, repo, tmp_path):
    """The user, 2026-10-15: a ref to a .tf file had no view button. The
    file was on main and not in the branch checked out, and the viewer read
    the worktree alone. It reads the default branch's copy, and says so."""
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",  # noqa: E731
                                     "-c", "user.email=t@t", *a], check=True,
                                    capture_output=True)
    base = git("rev-parse", "HEAD").stdout.decode().strip()
    (repo / "infra").mkdir()
    (repo / "infra" / "main.tf").write_text('resource "aws_instance" "web" {}\n')
    git("add", "-A")
    git("commit", "-q", "-m", "infra")
    git("checkout", "-q", "-b", "wip", base)
    assert not (repo / "infra" / "main.tf").exists()
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": [{"type": "item", "name": "infra/main.tf"}], "body": "see it"},
            author="ada")
    build_page(ctx, author="ada")
    await user.open("/notes")
    part(user, "file-open").click()
    await user.should_see(marker="file-view", retries=40)
    (view,) = user.find(marker="file-view").elements
    assert "aws_instance" in view.props["innerHTML"]
    await user.should_see("infra/main.tf, as on main")


def test_the_viewer_knows_terraform_files_pygments_leaves_unnamed():
    """pygments names a lexer for .tf and not for .tfvars or .tfstate, so
    those got no view button (the user, 2026-10-15)."""
    from symbion.gui.notes import _lexer
    assert _lexer("env/dev.tfvars").aliases[0] == "terraform"
    assert _lexer("terraform.tfstate").aliases[0] == "json"
    assert _lexer("main.tf").aliases[0] == "terraform"
    assert _lexer("notes.unknownext") is None


async def test_a_code_target_opens_coloured_in_a_dialog(user: User, repo, tmp_path):
    """The user, 2026-10-11: as markdown opens in a viewer, so should the
    other files we can read, code most of all. A fence in the file stays
    inside the one block."""
    ctx = api.resolve(str(tmp_path))
    (repo / "x.py").write_text('def f():\n    return """\n```\n"""\n')
    (repo / "blob.py").write_text("a\0b")
    for name in ("x.py", "blob.py"):
        api.add(ctx, {"kind": "note", "target": {"type": "item", "name": name},
                      "body": "about it"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/object?type=item&name=x.py")
    user.find(marker="file-open").click()
    await user.should_see(marker="file-view", retries=40)
    (view,) = user.find(marker="file-view").elements
    html = view.props["innerHTML"]
    assert html.count('<pre class="codehilite">') == 1, html
    assert "return" in html and "```" in html, html
    await user.open("/object?type=item&name=blob.py")
    user.find(marker="file-open").click()
    assert user.notify.contains("blob.py is not text")


async def test_the_viewer_shows_its_file_in_the_file_manager(user: User, repo, tmp_path,
                                                             monkeypatch):
    """The user, 2026-10-16: "is open in finder even a possibility?" The
    viewer's button runs the reveal command on the file it shows, and so
    does a shift-click on a card's read button, with no viewer."""
    import asyncio
    from pathlib import Path
    ctx = api.resolve(str(tmp_path))
    (repo / "x.py").write_text("x = 1\n")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "x.py"},
                  "body": "about it"}, author="ada")
    got = tmp_path / "revealed"
    notes = build_page.__globals__["view_button"].__globals__
    monkeypatch.setitem(notes, "_reveal_cmd", lambda p: [
        sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(sys.argv[2])",
        str(got), str(p)])

    async def revealed():
        for _ in range(100):
            if got.exists():
                break
            await asyncio.sleep(0.05)
        path = Path(got.read_text()).resolve()
        got.unlink()
        return path

    build_page(ctx, author="ada")
    await user.open("/notes")                # the card's button; the page has none
    part(user, "file-open").click()
    await user.should_see(marker="file-reveal")
    user.find(marker="file-reveal").click()
    assert await revealed() == (repo / "x.py").resolve()
    await user.open("/notes")
    part(user, "file-open").click(shift=True)
    assert await revealed() == (repo / "x.py").resolve()
    await user.should_not_see(marker="file-view")


async def test_the_viewer_card_takes_the_focus_so_page_keys_scroll_it(user: User, repo,
                                                                      tmp_path):
    """The user, 2026-10-16: "modal viewer - page up/down don't work". A
    key scrolls the focused element or a scroller around it. Quasar's dialog
    focused its own inner node, the parent of the card that scrolls, so the
    keys scrolled nothing. It focuses an `[autofocus][tabindex]` element
    first. Checked in Chrome: no client JS runs here."""
    ctx = api.resolve(str(tmp_path))
    (repo / "x.py").write_text("x = 1\n")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "x.py"},
                  "body": "about it"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/object?type=item&name=x.py")
    user.find(marker="file-open").click()
    await user.should_see(marker="file-view", retries=40)
    (view,) = user.find(marker="file-view").elements
    card = view.parent_slot.parent
    assert "sb-md-card" in card.classes
    assert card.props.get("autofocus") is True and card.props.get("tabindex") == "-1"


async def test_a_file_on_disk_gets_reveal_and_editor_buttons_on_its_page(user: User, repo,
                                                                         tmp_path, monkeypatch):
    """The user, 2026-10-16: show in Finder and open in the editor sit on
    the target's page, beside its read button, for a file the viewer cannot
    show too. The editor's link is VS Code's, its path quoted."""
    from pathlib import Path
    from urllib.parse import unquote
    ctx = api.resolve(str(tmp_path))
    (repo / "a b.py").write_text("x = 1\n")
    (repo / "pic.png").write_bytes(b"\x89PNG\0")
    for name in ("a b.py", "pic.png"):
        api.add(ctx, {"kind": "note", "target": {"type": "item", "name": name},
                      "body": "about it"}, author="ada")
    notes = build_page.__globals__["view_button"].__globals__
    monkeypatch.setitem(notes, "_reveal_cmd", lambda p: ["reveal", str(p)])
    build_page(ctx, author="ada")
    await user.open("/object?type=item&name=a%20b.py")
    await user.should_see(marker="file-reveal")
    (link,) = user.find(marker="file-editor").elements
    href = link.props["href"]
    assert href.startswith("vscode://file/") and href.endswith("/a%20b.py"), href
    rest = unquote(href.removeprefix("vscode://file"))
    assert Path(rest[1:] if sys.platform == "win32" else rest) == (repo / "a b.py").resolve()
    await user.open("/object?type=item&name=pic.png")
    await user.should_see(marker="file-editor")
    await user.should_not_see(marker="file-open")


def _pushed_commit(repo) -> str:
    """A commit with a body, by an author with an email, that origin's main holds."""
    (repo / "g.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--author=Ada Lovelace <ada@example.com>",
         "-m", "add g\n\nWhy g exists, said below the subject.")
    _git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return _git(repo, "rev-parse", "HEAD")


async def test_a_commit_shows_its_diff_and_links_its_forge(user: User, repo, tmp_path):
    """The user, 2026-10-30, on a commit: "the diff, then jump to it, or its
    MR/PR". The page and a card's chip open the diff under the subject, the
    author's name and the body, never the email; the page and the dialog
    link the commit and its pull requests on the forge origin names."""
    sha = _pushed_commit(repo)
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "commit", "name": sha},
                  "body": "about it"}, author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/object?type=commit&name={sha}")
    (page,) = user.find(marker="forge-commit").elements
    assert page.props["href"] == f"https://github.com/o/r/commit/{sha}"
    assert page.props["target"] == "_blank"
    (prs,) = user.find(marker="forge-prs").elements
    assert prs.props["href"] == f"https://github.com/o/r/pulls?q=is%3Apr+{sha}"
    user.find("read the diff").click()
    await user.should_see(marker="diff-view", retries=40)
    await user.should_see("add g")
    await user.should_see("Why g exists, said below the subject.")
    await user.should_see(f"{sha[:12]} · Ada Lovelace · ")
    await user.should_not_see("ada@example.com")
    assert len(user.find(marker="forge-commit").elements) == 2     # the page's, the dialog's
    await user.open("/notes")
    part(user, "file-open").click()
    await user.should_see("Why g exists, said below the subject.")


async def test_a_typed_name_reads_as_context_reads_it(user: User, repo, tmp_path):
    """A pasted /object URL with a short sha listed 0 entries, while `context
    --target` resolved it: rows store the full sha (2026-10-30). A name two
    catalog names hold says so and reads as typed."""
    sha = _pushed_commit(repo)
    (tmp_path / "symbion.toml").write_text("""[catalogs]\nthing = 'printf "a/x\\nb/x\\n"'\n""")
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "commit", "name": sha},
                  "body": "about it"}, author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/object?type=commit&name={sha[:7]}")
    await user.should_see("about it")
    # titled short, as Targets names it: the whole sha left the search 120px
    assert [e.text for e in user.find(marker="page-title").elements] == [f"commit: {sha[:7]}"]
    await user.open("/object?type=thing&name=x")
    await user.should_see("matches 2")
    await user.should_see("thing: x")


async def test_a_read_only_commit_page_names_no_remote(user: User, repo, tmp_path,
                                                       monkeypatch):
    """Read-only, the public demo: a forge button answers with a notice, and
    no link spells out the serving repo's remote, which may be private."""
    sha = _pushed_commit(repo)
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "commit", "name": sha},
                  "body": "about it"}, author="ada")
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    build_page(ctx, author="ada")
    await user.open(f"/object?type=commit&name={sha}")
    (page,) = user.find(marker="forge-commit").elements
    assert "href" not in page.props
    user.find(marker="forge-commit").click()
    await user.should_see("Read-only demo: in your own store, this opens the commit on github.com.")


def _commit_all(repo, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _newest(user, marker):
    return max(user.find(marker=marker).elements, key=lambda e: e.id)


async def test_a_diff_reads_unified_or_side_by_side_and_keeps_the_pick(user: User, repo,
                                                                       tmp_path):
    """The user, 2026-10-30: "side-by-side diff is far easier to reason about
    for me, but I realize this is divisive". A first diff reads unified, as
    git prints one; the toggle's pick holds for the next diff on the page."""
    sha = _pushed_commit(repo)
    build_page(api.resolve(str(tmp_path)), author="ada")
    await user.open(f"/object?type=commit&name={sha}")
    user.find("read the diff").click()
    await user.should_see(marker="diff-view", retries=40)
    view = _newest(user, "diff-view")
    assert "sb-diff-unified" in view.content and "sb-diff-split" not in view.content
    assert "y = 2" in view.content
    _newest(user, "diff-mode").set_value("split")
    await asyncio.sleep(0.1)                        # the render runs on a worker thread
    assert "sb-diff-split" in view.content
    assert "sb-diff-narrow" in view.content       # unified too, for a narrow window
    user.find("read the diff").click()
    await asyncio.sleep(0.1)
    assert "sb-diff-split" in _newest(user, "diff-view").content


async def test_a_diff_parses_and_renders_off_the_event_loop(user: User, repo, tmp_path,
                                                            monkeypatch):
    """A diff click parsed and rendered in its handler, on the event loop: a
    large diff held every visitor for its render, and a side-by-side pick
    renders twice (audit 2026-11-02). Each runs on a worker thread now."""
    import threading
    from symbion.gui import diffview
    sha = _pushed_commit(repo)
    build_page(api.resolve(str(tmp_path)), author="ada")
    ran = []
    for name in ("parse", "render"):
        real = getattr(diffview, name)
        monkeypatch.setattr(diffview, name, lambda *a, real=real, name=name:
                            ran.append((name, threading.get_ident())) or real(*a))
    await user.open(f"/object?type=commit&name={sha}")
    user.find("read the diff").click()
    await user.should_see(marker="diff-view", retries=40)
    _newest(user, "diff-mode").set_value("split")
    await asyncio.sleep(0.1)
    assert [n for n, _ in ran] == ["parse", "render", "render", "render"]
    assert threading.get_ident() not in {t for _, t in ran}


async def test_a_merge_reads_as_what_it_brought_onto_its_branch(user: User, repo, tmp_path):
    """git shows a clean merge's patch as a combined diff, which is empty:
    the viewer showed a merge's stat and no change at all (2026-10-30)."""
    (repo / "m.txt").write_text("one\n")
    _commit_all(repo, "base")
    _git(repo, "checkout", "-qb", "side")
    (repo / "m.txt").write_text("one\ntwo from side\n")
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "o.txt").write_text("o\n")
    _commit_all(repo, "main moves")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    sha = _git(repo, "rev-parse", "HEAD")
    build_page(api.resolve(str(tmp_path)), author="ada")
    await user.open(f"/object?type=commit&name={sha}")
    await user.should_see("1 file · +1 −0 · the entries on each")
    user.find("read the diff").click()
    await user.should_see(marker="diff-view", retries=40)
    await user.should_see(marker="commit-merge")
    assert "two from side" in _newest(user, "diff-view").content


async def test_a_commits_page_leads_with_its_message_and_the_entries_on_each_file(
        user: User, repo, tmp_path):
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    sha = _pushed_commit(repo)
    ctx = api.resolve(str(tmp_path))
    for kind, body in (("bug", "open on g"), ("note", "a note on g")):
        api.add(ctx, {"kind": kind, "target": {"type": "file", "name": "g.py"}, "body": body},
                author="ada")
    build_page(ctx, author="ada")
    await user.open(f"/object?type=commit&name={sha}")
    await user.should_see(marker="commit-card")
    await user.should_see("add g")
    await user.should_see("Why g exists, said below the subject.")
    await user.should_see("1 file · +1 −0 · the entries on each")
    assert len(user.find(marker="commit-file").elements) == 1
    with user.scope(marker="commit-file"):
        await user.should_see("g.py")
        await user.should_see("1 open")
    await user.open("/object?type=commit&name=abcdef1234")
    await user.should_see("not a commit in this checkout")
    await user.should_not_see("read the diff")


def _since_repo(repo, tmp_path):
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    (repo / "f").write_text("one\nvalue = two\n")
    _commit_all(repo, "f in two lines")
    return api.resolve(str(tmp_path))


async def test_a_since_chip_opens_the_commits_and_the_files_diff_since_the_entry(
        user: User, repo, tmp_path):
    ctx = _since_repo(repo, tmp_path)
    api.add(ctx, {"kind": "bug", "target": {"type": "file", "name": "f"}, "body": "on f"},
            author="ada")
    (repo / "f").write_text("one\nvalue = TWO\n")
    _commit_all(repo, "shout two")
    (repo / "f").write_text("one\nvalue = TWO\nthree\n")
    _commit_all(repo, "add three")
    build_page(ctx, author="ada")
    await user.open("/object?type=file&name=f")
    await user.should_see("2 commits since")
    part(user, "note-since").click()
    await user.should_see(marker="commit-list", retries=40)
    assert len(user.find(marker="commit-row").elements) == 2
    await user.should_see("shout two")
    await user.should_see("f, as this entry saw it, against the file now")
    view = _newest(user, "diff-view")
    assert '<span class="sb-dx">two</span>' in view.content and "three" in view.content
    user.find(marker="since-was").click()
    await user.should_see(marker="file-view", retries=40)
    was = _newest(user, "file-view").content
    assert "value = two" in was and "TWO" not in was


async def test_the_git_behind_a_diff_runs_off_the_event_loop(user: User, repo, tmp_path,
                                                             monkeypatch):
    """The 2026-11-02 audit moved a diff's parse to a worker thread, and the
    git that made its text stayed in the click's handler: a large `git show`
    held every page's loop (the GUI review, 2026-11-07). A commit's diff, a
    file's since-diff and its earlier text all read git on a worker now."""
    import threading
    ctx = _since_repo(repo, tmp_path)
    api.add(ctx, {"kind": "bug", "target": {"type": "file", "name": "f"}, "body": "on f"},
            author="ada")
    (repo / "f").write_text("one\nvalue = TWO\n")
    _commit_all(repo, "shout two")
    sha = _git(repo, "rev-parse", "HEAD")
    build_page(ctx, author="ada")
    ran = []
    for name in ("commit_show", "diff_text", "changed_since", "since_base", "commit_rows",
                 "blob_text"):
        real = getattr(gitref, name)
        monkeypatch.setattr(gitref, name, lambda *a, real=real, name=name, **k:
                            ran.append((name, threading.get_ident())) or real(*a, **k))
    await user.open(f"/object?type=commit&name={sha}")
    ran.clear()                                      # the page's own build reads git too
    user.find("read the diff").click()
    await user.should_see(marker="diff-view", retries=40)
    clicked = list(ran)
    await user.open("/object?type=file&name=f")
    await user.should_see("1 commit since")
    ran.clear()
    part(user, "note-since").click()
    await user.should_see(marker="commit-list", retries=40)
    user.find(marker="since-was").click()
    await user.should_see(marker="file-view", retries=40)
    clicked += ran
    assert {n for n, _ in clicked} == {"commit_show", "diff_text", "changed_since",
                                       "since_base", "commit_rows", "blob_text"}, clicked
    assert threading.get_ident() not in {t for _, t in clicked}, \
        [n for n, t in clicked if t == threading.get_ident()]


@pytest.mark.skipif(sys.platform == "win32", reason="a file name cannot hold a double quote")
async def test_a_quoted_file_name_is_one_prop_in_the_since_dialog(user: User, repo, tmp_path):
    """`.props(f'aria-label="read {path} …"')` let a file name close the
    quote and add a `:`-prop, which nicegui.js runs as JavaScript (audit
    2026-11-02)."""
    ctx = _since_repo(repo, tmp_path)
    evil = 'a" :title="alert(1)" x="'
    (repo / evil).write_text("one\n")
    _commit_all(repo, "evil")
    api.add(ctx, {"kind": "bug", "target": {"type": "file", "name": evil}, "body": "on it"},
            author="ada")
    (repo / evil).write_text("two\n")
    _commit_all(repo, "change it")
    build_page(ctx, author="ada")
    await user.open("/object?" + urlencode({"type": "file", "name": evil}))
    part(user, "note-since").click()
    await user.should_see(marker="since-was", retries=40)
    (b,) = user.find(marker="since-was").elements
    assert b._props["aria-label"] == f"read {evil} as this entry saw it" \
        and ":title" not in b._props


async def test_a_since_diff_from_edits_no_commit_holds_reads_the_nearest_commit(
        user: User, repo, tmp_path, monkeypatch):
    ctx = _since_repo(repo, tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("one\nvalue = draft\n")        # never committed
    api.add(ctx, {"kind": "bug", "target": {"type": "file", "name": "f"}, "body": "on f"},
            author="ada")
    (repo / "f").write_text("one\nvalue = final\n")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-04T00:00:00+00:00")   # after the row
    _commit_all(repo, "final")
    build_page(ctx, author="ada")
    await user.open("/object?type=file&name=f")
    part(user, "note-since").click()
    await user.should_see(f"f, as of {before[:7]}, the nearest commit: the entry saw edits "
                          f"no commit holds, against the file now", retries=40)


async def test_a_checks_behind_chip_opens_the_commits_and_what_they_changed(
        user: User, repo, tmp_path, monkeypatch):
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "w"},
                  "checked": "run", "result": "ok"}, author="ada")
    api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "v"},
                  "checked": "run", "result": "ok"}, author="ada")
    (repo / "f").write_text("changed after the run\n")
    _commit_all(repo, "after the run")
    api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "v"},
                  "checked": "run again", "result": "ok"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/object?type=item&name=v")
    current = part(user, "check-state", content="current")
    with pytest.raises(AssertionError, match="takes no click"):
        current.click()
    await user.open("/object?type=item&name=w")
    import threading
    ran = []                     # its git on a worker thread (the GUI review, 2026-11-07)
    for name in ("stamp_cfg", "commit_rows", "diff_text"):
        real = getattr(gitref, name)
        monkeypatch.setattr(gitref, name, lambda *a, real=real, name=name, **k:
                            ran.append((name, threading.get_ident())) or real(*a, **k))
    part(user, "check-state", content="behind 1").click()
    await user.should_see(marker="commit-list", retries=40)
    await user.should_see("after the run")
    assert "changed after the run" in _newest(user, "diff-view").content
    assert {n for n, _ in ran} == {"stamp_cfg", "commit_rows", "diff_text"}, ran
    assert threading.get_ident() not in {t for _, t in ran}, ran


def test_the_reveal_command_selects_the_file_and_never_opens_it(monkeypatch, tmp_path):
    """`open -R` and `/select,` select the file; a headless Linux gets no
    button, and a desktop one opens the folder."""
    from symbion.gui.notes import _reveal_cmd
    f = tmp_path / "a b.py"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _reveal_cmd(f) == ["open", "-R", str(f)]
    monkeypatch.setattr(sys, "platform", "win32")
    assert _reveal_cmd(f) == ["explorer", f"/select,{f}"]
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert _reveal_cmd(f) is None
    monkeypatch.setenv("DISPLAY", ":0")
    assert _reveal_cmd(f) == ["xdg-open", str(tmp_path)]


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
    assert _count(user).startswith("1 of 2 entries match")


async def test_a_filter_that_hides_every_card_searches_the_store(user: User, ctx_with_notes,
                                                                 monkeypatch):
    """The page's script sends the text after a pause once its filter hides
    every card: a superseded id matched no card and read as a row that was
    gone (the user, 2026-10-05). The page that shows that search stays.
    A page with no entry cards does not (the user, 2026-10-29): its rows
    are not entries, and Enter still searches."""
    from nicegui import ui
    for path in ("/tags", "/docs", "/arcs", "/map"):
        await user.open(path)
        with monkeypatch.context() as m:
            went = []
            m.setattr(ui.navigate, "to", went.append)
            user.find(marker="search").trigger("update:value", "beta")
            assert went == [], path
    await user.open("/")
    user.find(marker="search").trigger("update:value", "beta")
    await user.should_see(marker="result-count")
    assert _count(user).startswith("1 of 2 entries match")
    went = []
    monkeypatch.setattr(ui.navigate, "to", went.append)
    user.find(marker="search").trigger("update:value", " beta ")
    assert went == []
    user.find(marker="search").trigger("update:value", "beta gamma")
    assert len(went) == 1 and "gamma" in went[0], went


async def test_the_sidebar_links_the_other_stores_a_serve_runs_on(user: User, repo, tmp_path,
                                                                    monkeypatch):
    """The user, 2026-10-01: switch stores in the GUI. One `serve` per store
    keeps each store's author and repo; each records itself while it runs,
    and the sidebar links the others. A dead serve's record is dropped, and
    a record that does not parse is skipped."""
    import json
    import os
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    records = tmp_path / "cache" / "symbion" / "serve"
    records.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", ""])
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
    assert asides["/object?type=item&name=widget"] == "· 3 entries", asides
    # Two cards, each on its own target: the ref's open task is counted on
    # widget's board, not carded there. Cards, not text: the sidebar's closed
    # list shows the resolved row's body too.
    assert len(user.find(marker="note-row").elements) == 2
    await user.should_see("open body")
    await user.should_see("ref body")
    assert user.find(marker="place-targets").elements


async def test_a_card_on_a_file_says_how_many_commits_changed_it_since(user: User, repo,
                                                                         tmp_path):
    """Each page of cards counts once for its rows, as it reads HEAD once."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    ctx = api.resolve(str(tmp_path))
    arc = api.create_arc(ctx, "a", "", "mixed", author="ada")
    api.add(ctx, {"kind": "bug", "target": {"type": "file", "name": "f"}, "body": "on f",
                  "refs": [{"type": "arc", "name": arc.id}]}, author="ada")
    for i in range(2):
        (repo / "f").write_text(f"v{i}")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam",
                        f"edit {i}"], check=True, cwd=repo)
    build_page(ctx, author="ada")
    for url in ("/", "/notes", "/object?type=file&name=f", "/targets", f"/arc?id={arc.id}"):
        await user.open(url)
        await user.should_see("2 commits since")


async def test_a_page_of_boards_counts_commits_since_once(user: User, repo, tmp_path,
                                                          monkeypatch):
    """A count per board took /targets from 0.37 s to 0.62 s (2026-10-05)."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    for name in ("g", "h"):
        (repo / name).write_text(name)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
                   check=True, cwd=repo)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                    "g h"], check=True, cwd=repo)
    ctx = api.resolve(str(tmp_path))
    for name, kind in (("f", "bug"), ("g", "task"), ("h", "question")):    # three boards
        api.add(ctx, {"kind": kind, "target": {"type": "file", "name": name},
                      "body": f"on {name}"}, author="ada")
    build_page(ctx, author="ada")
    calls = []
    real = gitref._git
    monkeypatch.setattr(gitref, "_git", lambda cfg, *a, **k: calls.append(a[0]) or real(cfg, *a, **k))
    for url in ("/", "/targets"):
        calls.clear()
        await user.open(url)
        assert calls.count("hash-object") == 1, (url, calls)


async def test_the_undo_button_asks_with_what_it_takes_back(user: User, repo, tmp_path,
                                                           monkeypatch):
    """A misclick on resolve or the star takes two clicks to take back (the
    user, 2026-10-01), and so does a note added to the wrong store, which
    took a `git restore` (2026-10-13). An undo icon in the top bar while the
    store's last write is yours and uncommitted; its dialog says what the
    undo changes. A timed notice set a deadline the store does not have, and
    a click on it as it faded did nothing."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "b"}, author="ada")
    api.commit(ctx, "c", author="ada")
    build_page(ctx, author="ada")
    head = lambda: store.chain_tip(store.load(tmp_path), n.id)
    await user.open("/notes")
    await user.should_not_see(marker="undo-button")       # a commit ends the level
    part(user, "note-resolve").click()
    await user.should_see(marker="undo-button")
    user.find(marker="undo-button").click()
    await user.should_see("resolved → open")
    assert store.read_status(head()) == "resolved", "asking writes nothing"
    user.find(marker="undo-confirm").click()
    await user.should_see(f"undone: the edit of {n.id} by ada")
    assert store.read_status(head()) == "open"
    await user.should_not_see(marker="undo-button")       # one level

    user.find(marker="new-note").click()
    user.find(marker="note-body").type("wrong store").trigger("keydown.meta.enter")
    await user.should_see(marker="undo-button")
    nid = next(r.id for r in store.load(tmp_path) if r.body == "wrong store")
    user.find(marker="undo-button").click()
    await user.should_see(f"removes {nid[-10:]}")
    user.find(marker="undo-confirm").click()
    await user.should_see(f"undone: the add of {nid} by ada")
    assert "wrong store" not in {r.body for r in store.load(tmp_path)}

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")    # an agent's write
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "theirs"}, author="claude")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    await user.open("/notes")
    await user.should_not_see(marker="undo-button")


async def test_an_undo_another_write_overtook_says_why(user: User, repo, tmp_path, monkeypatch):
    """The user looked for the undo icon to take back a test entry, and it
    was gone: an agent had written since, and the one level is the newest
    write's (2026-10-16). While the person has an uncommitted write,
    the icon stays, dimmed, and a click names the write that holds the level."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "mine"}, author="ada")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")    # an agent's write
    theirs = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                           "body": "theirs"}, author="claude")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    build_page(ctx, author="ada")
    await user.open("/notes")
    await user.should_not_see(marker="undo-button")
    user.find(marker="undo-gone").click()
    await user.should_see(f"the last write is not yours: it was the add of {theirs.id} by claude")
    api.commit(ctx, "c", author="ada")
    await user.open("/notes")
    await user.should_not_see(marker="undo-gone")          # nothing of hers is left to undo


async def test_a_store_with_repos_names_the_store_not_the_repo_the_serve_stands_in(
        user: User, tmp_path, monkeypatch):
    """The serve is the store's (one per store, on the store's port), so a
    store over several repos takes the store's name, as the CLI's repos line
    does, whichever repo the serve started in (the user, 2026-10-14)."""
    r = tmp_path / "net"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / ".symbion").write_text("../work-notes\n")
    store.ensure_store(tmp_path / "work-notes")
    (tmp_path / "work-notes" / "symbion.toml").write_text('[repos]\nnet = "../net"\n')
    monkeypatch.chdir(r)
    build_page(api.resolve(), author="ada")
    await user.open("/")
    assert user.client.resolve_title() == "work-notes · Summary · symbion"
    from symbion.gui import chrome
    assert f'<meta property="og:description" content="{chrome.ABOUT}">' in user._client.head_html
    (brand,) = user.find(marker="brand").elements
    assert brand.text == "work-notes"


async def test_a_read_only_page_says_so_and_links_the_readme(user: User, ctx_with_notes,
                                                            monkeypatch):
    """A visitor who lands on the public demo from a link learns what they
    see and where to get it, and no badge says they write (the spec,
    2026-10-22)."""
    await user.open("/")
    await user.should_not_see(marker="demo-intro")       # a serve of your own: no intro
    assert 'property="og:title"' not in user._client.head_html
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open("/")
    # A shared demo link previews as the demo, with the repo's banner.
    head = user._client.head_html
    assert '<meta property="og:title" content="symbion: live demo">' in head
    assert 'property="og:image" content="https://raw.githubusercontent.com/' in head
    await user.should_see("read-only demo")
    # A visitor from a social post lands on the demo before the README, and most
    # left from the summary alone: it opens with what symbion is and what this
    # store is (the user, 2026-11-05).
    await user.should_see("symbion is a notebook and ticket registry")
    name = build_page.__globals__["store_name"](ctx_with_notes)
    await user.should_see(f"This is {name}'s store")
    # Each link to the source goes by /go/github, which 302s to the readme: the
    # demo's access log then counts clicks out, and names the link (the user,
    # 2026-11-03). /go/github itself is in test_gui_serve.py.
    hrefs = {m: next(iter(user.find(marker=m).elements)).props["href"]
             for m in ("demo-link", "demo-intro-link", "source")}
    assert hrefs == {"demo-link": "/go/github?from=banner",
                     "demo-intro-link": "/go/github?from=intro",
                     "source": "/go/github?from=sidebar"}
    assert next(iter(user.find(marker="demo-link").elements)).text == "symbion on GitHub"
    assert 'href="/go/github?from=noscript"' in user._client.body_html
    await user.should_see("viewing as ada")
    await user.should_not_see("writing as ada")


async def test_a_refusal_reaches_the_visitor_as_a_notice(user: User, ctx_with_notes, tmp_path,
                                                        monkeypatch, caplog):
    """A control with no notice of its own still answers a click: the refusal
    below it becomes the fallback notice, after an await too, and nothing
    changes. Every real control names its own (the seam test's
    test_every_gui_write_names_what_it_does_when_read_only); this is the net
    under one that does not. NiceGUI logs the refusal, so such a control
    shows in the serve's log."""
    from nicegui import run
    nid = store.heads(store.load(tmp_path))[0].id
    before = (tmp_path / "notes.jsonl").read_bytes()
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")

    async def push():
        await run.io_bound(api.push, ctx_with_notes, capture=True)

    @ui.page("/t")
    def page():
        ui.button("star", on_click=lambda: api.retag(ctx_with_notes, nid, add=("priority",),
                                                     author="ada")).mark("bare-star")
        ui.button("push", on_click=push).mark("bare-push")

    await user.open("/t")
    user.find(marker="bare-star").click()
    await user.should_see("Read-only demo: nothing here writes.")
    user.notify.messages.clear()
    user.find(marker="bare-push").click()
    await user.should_see("Read-only demo: nothing here writes.", retries=40)
    assert (tmp_path / "notes.jsonl").read_bytes() == before
    assert "read-only: nothing here writes" in caplog.text
    caplog.clear()


async def test_each_control_names_what_it_does_when_read_only(user: User, ctx_with_notes,
                                                             tmp_path, monkeypatch):
    """The public demo's controls teach instead of acting: each says what it
    does in a real store, and the fallback notice never shows (the user,
    2026-10-22)."""
    _q(ctx_with_notes, "Ship it?", options=["ship it", "hold"])
    before = (tmp_path / "notes.jsonl").read_bytes()
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open("/notes")
    for marker, does in (("note-star", "stars the entry"),
                         ("note-resolve", "resolves the entry"),
                         ("answer-option", "adds your answer below the question"),
                         ("commit-button", "commits the store"),
                         ("undo-button", "takes back the last write")):
        (parts(user, marker)[0] if parts(user, marker) else user.find(marker=marker)).click()
        await user.should_see(f"Read-only demo: in your own store, this {does}.")
    assert not user.notify.contains("nothing here writes"), user.notify.messages
    assert (tmp_path / "notes.jsonl").read_bytes() == before


async def test_the_stopped_clock_keeps_a_check_on_the_recent_board(user: User, repo, tmp_path,
                                                                  monkeypatch):
    """The demo serves a snapshot for months: under SYMBION_NOW a check from
    five days before the clock stays on `/`'s recent board, as on its day."""
    import subprocess
    ctx = api.resolve(str(tmp_path))
    monkeypatch.setenv("SYMBION_NOW", "2026-01-05T12:00:00Z")    # far from any real today
    api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "rig"},
                  "checked": "the rig run", "result": "ok"}, author="ada")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "later"], cwd=repo, check=True)
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:00:00Z")
    build_page(ctx, author="ada")
    await user.open("/")
    await user.should_see("the rig run")


async def test_a_read_only_page_polls_no_git_and_names_no_path(user: User, ctx_with_notes,
                                                              monkeypatch):
    """Nothing changes under read-only, so a tab's git poll is load from
    anonymous visitors on a shared box; and `/` names the store, not its
    path on the serving machine (a reviewer, 2026-10-22)."""
    await user.open("/")
    assert user.find(marker="git-poll").elements
    await user.should_see(str(ctx_with_notes.store_dir))
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    await user.open("/")
    await user.should_not_see(marker="git-poll")
    await user.should_not_see(str(ctx_with_notes.store_dir))


def _side(store_dir, name):
    subprocess.run(["git", "-C", str(store_dir), "config", "user.name", name], check=True)


@pytest.fixture
def shared(repo, tmp_path):
    """A shared store read from side A: `_side` switches the side a write is made as."""
    (tmp_path / "symbion.toml").write_text("shared = true\n")
    _side(tmp_path, "A")
    ctx = api.resolve(str(tmp_path))
    build_page(ctx, author="ada")
    return ctx


def _both_sides_ask(ctx, store_dir):
    """(mine, theirs): an open question from each side, by an agent called sam."""
    mine = _q(ctx, "is it blue")
    _side(store_dir, "B")
    theirs = _q(ctx, "is it red")
    _side(store_dir, "A")
    return mine, theirs


async def test_home_leaves_out_the_other_sides_questions(user: User, shared, tmp_path):
    """The person's page is their session start: what waits on them, and what
    is due, are their side's. The boards below it are the store, both sides."""
    mine, theirs = _both_sides_ask(shared, tmp_path)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    due_mine = _q(shared, "ours", kind="task", due=tomorrow)
    _side(tmp_path, "B")
    due_theirs = _q(shared, "theirs", kind="task", due=tomorrow)
    _side(tmp_path, "A")
    await user.open("/")
    boards = _board_ids(user)
    assert boards["waiting on you"] == [mine], boards
    assert boards["open question"] == [theirs], boards
    assert boards["due soon"] == [due_mine], boards
    assert boards["open task"] == [due_theirs], boards


async def test_home_shows_the_readers_sides_wrap(user: User, shared, tmp_path):
    _q(shared, "one")
    api.commit(shared, "A's handoff", everyone=True, wrap=True)
    _side(tmp_path, "B")
    _q(shared, "two")
    api.commit(shared, "B's handoff", everyone=True, wrap=True)
    _side(tmp_path, "A")
    await user.open("/")
    await user.should_see("A's handoff")
    await user.should_not_see("B's handoff")


async def test_notes_name_the_other_sides_author(user: User, shared, tmp_path):
    _both_sides_ask(shared, tmp_path)
    await user.open("/notes")
    await user.should_see("sam (B)")


async def test_the_arc_page_names_the_other_sides_author(user: User, shared, tmp_path):
    act = api.create_arc(shared, "camp", "d", "item", author="ada")
    _side(tmp_path, "B")
    (box,) = api.seed(shared, act.id, "item", ["widget"], author="sam")
    api.supersede(shared, box.id, author="sam", status="resolved")
    _side(tmp_path, "A")
    await user.open(f"/arc?id={act.id}")
    await user.should_see("sam (B)")


async def test_the_other_sides_question_has_no_answer_box(user: User, shared, tmp_path):
    """It waits on the other side's person, not on this one."""
    mine, theirs = _both_sides_ask(shared, tmp_path)
    await user.open(f"/notes?id={mine}")
    await see(user, "answer-field")
    await user.open(f"/notes?id={theirs}")
    await not_see(user, "answer-field")


async def test_the_commit_button_shows_a_missing_user_name(user: User, shared, tmp_path):
    """A shared store's write names the clone's missing user.name; the button raised it."""
    _q(shared, "pending", author="ada")
    subprocess.run(["git", "-C", str(tmp_path), "config", "--unset", "user.name"], check=True)
    await user.open("/notes")
    user.find(marker="commit-button").click()
    await user.should_see("config user.name NAME", retries=40)


def _git(d, *args) -> str:
    return subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def synced(shared, tmp_path, tmp_path_factory):
    """`shared` pushed to an origin, and B's clone of it, which writes there."""
    origin = tmp_path_factory.mktemp("origin") / "store.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "shared")
    _git(tmp_path, "remote", "add", "origin", str(origin))
    _git(tmp_path, "push", "-q", "-u", "origin", "HEAD")
    b = tmp_path_factory.mktemp("b") / "store"
    subprocess.run(["git", "clone", "-q", str(origin), str(b)], check=True, capture_output=True)
    _side(b, "B")
    return shared, b, origin


def _publish(b, body) -> str:
    """A row B writes, commits and pushes."""
    rid = _q(api.resolve(str(b)), body)
    api.commit(api.resolve(str(b)), "B's", everyone=True)
    _git(b, "push", "-q")
    return rid


async def test_a_shared_stores_commit_button_pulls_then_pushes(user: User, synced, tmp_path):
    """As `commit` does at a terminal; the GUI's commit stayed on this disk."""
    ctx, b, origin = synced
    theirs, mine = _publish(b, "from B"), _q(ctx, "from A", author="ada")
    await user.open("/notes")
    user.find(marker="commit-button").click()
    await user.should_see("pushed 2 commits to origin", retries=40)
    held = _git(origin, "show", f"{_git(tmp_path, 'branch', '--show-current')}:notes.jsonl")
    assert theirs in held and mine in held and gitref.unpushed(tmp_path) == 0


async def test_a_shared_stores_push_button_pulls_first(user: User, synced, tmp_path):
    """Once origin moved, a bare push was refused with git's own `git pull` hint."""
    ctx, b, origin = synced
    _q(ctx, "from A", author="ada")
    api.commit(ctx, "A's", everyone=True)           # as at a terminal, before origin moved
    theirs = _publish(b, "from B")
    await user.open("/notes")
    user.find(marker="push-button").click()
    await user.should_see("pulled 1 commit from origin/", retries=40)
    await user.should_see("pushed 2 commits to origin", retries=40)
    assert theirs in (tmp_path / "notes.jsonl").read_text() and gitref.unpushed(tmp_path) == 0


@pytest.mark.parametrize("s, last", [
    (api.Synced(api.Pulled(conflicts=["symbion.toml"])),
     "not pushed: git cannot merge symbion.toml; `symbion pull` at a terminal names the "
     "commands that finish the merge"),
    (api.Synced(api.Pulled(), timed_out=True), "not pushed: the push ran too long and was stopped"),
    (api.Synced(api.Pulled(), pushed=subprocess.CompletedProcess([], 1, "", "! [rejected] x\n")),
     "push failed: ! [rejected] x")])
def test_a_shared_stores_sync_says_each_way_its_push_waits(ctx_with_notes, s, last):
    from symbion.gui import chrome
    said, failed = chrome._synced(ctx_with_notes, s)
    assert failed and said[-1] == last, said


async def test_a_shared_stores_commit_says_why_its_push_waits(user: User, synced, tmp_path):
    ctx, b, origin = synced
    _git(tmp_path, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    _q(ctx, "from A", author="ada")
    await user.open("/notes")
    user.find(marker="commit-button").click()
    await user.should_see("not pushed: the pull failed: git fetch origin failed", retries=40)
    assert gitref.unpushed(tmp_path) == 1 and not store.pending_rows(tmp_path)


@pytest.mark.parametrize("page", ["/targets", "/object?type=item&name=widget", "/arc?id=camp"])
async def test_every_page_that_prints_rows_names_the_other_sides_author(user: User, shared,
                                                                        tmp_path, page):
    api.create_arc(shared, "camp", "d", "item", author="ada")
    _side(tmp_path, "B")
    for typ, name in (("item", "widget"), ("arc", "camp")):
        api.add(shared, {"kind": "task", "target": {"type": typ, "name": name},
                         "body": f"on the {name}"}, author="sam")
    _side(tmp_path, "A")
    await user.open(page)
    await user.should_see("sam (B)")


async def test_the_peek_names_the_other_sides_author(user: User, shared, tmp_path):
    mine, theirs = _both_sides_ask(shared, tmp_path)
    await user.open("/")
    _peek(user, theirs)
    with user.scope(marker="peek"):
        await user.should_see("sam (B)")


def _no_user_name(store_dir):
    subprocess.run(["git", "-C", str(store_dir), "config", "--unset", "user.name"], check=True)


@pytest.mark.parametrize("click", ["note-resolve", "note-star", "checklist-toggle", "new-arc-create"])
async def test_a_write_a_clone_with_no_user_name_cannot_make_is_told(user: User, shared,
                                                                    tmp_path, click):
    """The click raised into NiceGUI's log and did nothing."""
    _q(shared, "a task", kind="task")
    act = api.create_arc(shared, "camp", "d", "item", author="ada")
    api.seed(shared, act.id, "item", ["widget"], author="ada")
    _no_user_name(tmp_path)
    before = len(store.load(tmp_path)), len(store.load_arcs(tmp_path))
    await user.open({"checklist-toggle": f"/arc?id={act.id}", "new-arc-create": "/arcs"}
                    .get(click, "/notes?kind=task"))
    if click == "new-arc-create":
        user.find(marker="new-arc-name").type("other")
    # the first card's: the task and the arc's box are both on the page; a box
    # is a card, its tick the card's resolve
    mark = {"checklist-toggle": "note-resolve"}.get(click, click)
    (parts(user, mark)[0] if click != "new-arc-create" else user.find(marker=click)).click()
    await user.should_see("config user.name NAME")
    assert (len(store.load(tmp_path)), len(store.load_arcs(tmp_path))) == before


@pytest.mark.parametrize("click, write", [("arc-rename", "rename_arc"), ("arc-archive", "archive_arc"),
                                          ("arc-describe", "describe_arc")])
async def test_an_arc_write_the_store_refuses_is_told(user: User, shared, monkeypatch, click, write):
    def refuse(*a, **k):
        raise ValueError("the store refuses this")
    act = api.create_arc(shared, "camp", "d", "item", author="ada")
    monkeypatch.setattr(api, write, refuse)
    await user.open(f"/arc?id={act.id}")
    user.find(marker="arc-edit").click()
    user.find(marker=click).click()
    await user.should_see("the store refuses this")


@pytest.mark.parametrize("is_shared", [True, False], ids=["shared", "unshared"])
@pytest.mark.parametrize("page", ["/", "/notes", "/targets", "/object?type=item&name=widget",
                                  "/arc?id=camp"])
async def test_a_page_reads_the_clones_side_once_however_many_questions_it_shows(
        user: User, repo, tmp_path, monkeypatch, is_shared, page):
    """Each open question another wrote asked the store for its side and its
    rows again: a fork of `git config` and a load a card."""
    if is_shared:
        (tmp_path / "symbion.toml").write_text("shared = true\n")
        _side(tmp_path, "A")
    ctx = api.resolve(str(tmp_path))
    api.create_arc(ctx, "camp", "d", "item", author="ada")
    for target in ({"type": "item", "name": "widget"}, {"type": "arc", "name": "camp"}):
        for i in range(3):
            api.add(ctx, {"kind": "question", "target": target, "body": f"is it {i}"},
                    author="sam")
    build_page(ctx, author="ada")
    calls = []
    real = store.shared_side
    monkeypatch.setattr(store, "shared_side", lambda s: calls.append(s) or real(s))
    await user.open(page)
    await user.should_see("is it 2")
    assert len(calls) == 1, calls


async def test_a_row_id_is_one_prop_not_a_script(user: User, ctx_with_notes):
    """`.props(f"data-id={id}")` let an id like `x :title=1` add a `:`-prop,
    which nicegui.js runs as JavaScript; a pushed row or a hand edit sets the
    id, as store.load never checks its shape (audit 2026-10-28)."""
    bug = next(n.id for n in store.heads(store.load(ctx_with_notes.store_dir)) if n.kind == "bug")
    evil = f"{bug} :title=1"
    p = ctx_with_notes.store_dir / "notes.jsonl"
    p.write_text(p.read_text().replace(bug, evil))
    await user.open("/notes")
    (card,) = [e for e in user.find(marker="note-row").elements if bug in e._props.get("data-id", "")]
    assert card._props["data-id"] == evil and ":title" not in card._props


async def test_a_read_only_sidebar_names_no_other_store(user: User, repo, tmp_path, monkeypatch):
    """Its tooltip gave another store's absolute path to a read-only
    serve's visitors (audit 2026-10-28)."""
    import json
    import os
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    records = tmp_path / "cache" / "symbion" / "serve"
    records.mkdir(parents=True)
    (records / "elsewhere.json").write_text(json.dumps(
        {"pid": os.getpid(), "url": "http://127.0.0.1:1111",
         "store": str((tmp_path / "elsewhere-notes").resolve())}))
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    build_page(api.resolve(str(tmp_path)), author="ada")
    await user.open("/")
    await user.should_not_see(marker="store-item")


async def test_a_cut_board_with_no_page_of_its_own_links_nowhere(user: User, repo, tmp_path,
                                                                   monkeypatch):
    """The "waiting on you" board's `· N more` linked to "" and reloaded the
    page; an `arc:` ref chip went to /object, not the arc's page (audit
    2026-10-28)."""
    monkeypatch.setitem(build_page.__globals__, "BOARD_ROWS", 2)
    ctx = api.resolve(str(tmp_path))
    for i in range(3):
        api.add(ctx, {"kind": "question", "target": {"type": "project", "name": None},
                      "body": f"ask {i}"}, author="sam")
    arc = api.create_arc(ctx, "camp", "d", "item", author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}, "body": "x",
                  "refs": [{"type": "arc", "name": arc.id}]}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    asides = [e for e in user.find(marker="board-aside").elements if "more" in e.text]
    assert asides and all(e.props.get("href") for e in asides if "href" in e.props), \
        [(e.text, e.props.get("href")) for e in asides]
    await user.open("/notes")
    (ref,) = parts(user, "note-ref")
    assert ref.props["href"] == f"/arc?id={arc.id}"


def _map_store(tmp_path):
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    return api.resolve(str(tmp_path))


def _map_nodes(user: User) -> dict:
    (chart,) = user.find(marker="map-chart").elements
    todo, out = list(chart.props["options"]["series"][0]["data"]), {}
    while todo:
        d = todo.pop()
        out[d["name"]] = d
        todo.extend(d.get("children", []))
    return out


async def test_the_map_draws_the_store_and_says_what_it_counts_twice(user: User, repo, tmp_path):
    """The spec, 2026-10-29: the store's entries as a treemap, area by count,
    colour by open work. A target axis counts an entry on each object it
    cites, so the page says so."""
    ctx = _map_store(tmp_path)
    api.add(ctx, {"kind": "note", "target": {"type": "file", "name": "a.py"}, "body": "on a",
                  "tags": ["gui"]}, author="ada")
    api.add(ctx, {"kind": "bug", "target": {"type": "item", "name": "x"}, "body": "on x"},
            author="ada")
    build_page(ctx, author="ada")
    await user.open("/")
    await user.should_see(marker="place-map")
    await user.open("/map")
    assert set(_map_nodes(user)) == {"file/a.py", "item/x"}     # each type holds one child
    await user.should_see(marker="map-multi")
    await user.open("/map?by=kind")
    assert set(_map_nodes(user)) == {"note", "bug"}
    await user.should_not_see(marker="map-multi")


async def test_a_map_control_navigates_to_its_view(user: User, repo, tmp_path, monkeypatch):
    ctx = _map_store(tmp_path)
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "x"}, "body": "b"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/map")
    went = []
    monkeypatch.setattr(user.navigate, "to", lambda target, new_tab=False: went.append(target))
    (second,) = user.find(marker="map-by-2").elements
    second.value = "kind"
    (colour,) = user.find(marker="map-color").elements
    colour.value = "kind"
    assert went == ["/map?by=target%2Ckind", "/map?color=kind"]


async def test_a_map_click_lists_the_tiles_entries_and_a_link_to_all(user: User, repo, tmp_path,
                                                                     monkeypatch):
    """The chart sends a tile's id, never its entries; the panel lists them,
    open first, and past PANEL_ROWS links to a page with the same entries."""
    monkeypatch.setitem(build_page.__globals__, "PANEL_ROWS", 2)
    ctx = _map_store(tmp_path)
    ids = [api.add(ctx, {"kind": "task", "target": {"type": "file", "name": "a.py"},
                         "body": f"task {i}"}, author="ada").id for i in range(3)]
    for i in ids[1:]:
        api.supersede(ctx, i, author="ada", status="resolved")
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "y"}, "body": "elsewhere"},
            author="ada")                   # a second tile, for an id that picks the wrong one
    build_page(ctx, author="ada")
    await user.open("/map")
    tid = _map_nodes(user)["file/a.py"]["id"]
    chart = user.find(marker="map-chart")
    chart.trigger("componentClick", tid)
    rows = lambda: sorted(user.find(marker="map-row").elements, key=lambda e: e.id)  # noqa: E731
    await user.should_see(marker="map-row")
    first = [r.props["href"] for r in rows()]
    assert len(first) == 2 and first[0] == f"/notes?id={ids[0]}"
    (more,) = user.find(marker="map-all").elements
    assert more.props["href"] == "/object?type=file&name=a.py"
    # A tile's id only: the fixture's trigger sends {} for None, as no
    # browser does (the breadcrumb sends null); check that one in a browser.
    for stale in (-1, -2, 9999, "x", {}):
        chart.trigger("componentClick", stale)
        await asyncio.sleep(0.1)          # a refresh lands after trigger returns
        assert [r.props["href"] for r in rows()] == first, stale


async def test_back_or_a_tile_button_lists_the_tile_it_names(user: User, repo, tmp_path):
    """Back to a focus, or a press in the tile list, sends its tile's id
    (`_MAP_JS`), and the panel lists that tile's entries, as a click did; the
    whole map sends null, and the panel says what a click does. The history
    and the list's buttons run only in a browser; the list is there for them."""
    ctx = _map_store(tmp_path)
    api.add(ctx, {"kind": "task", "target": {"type": "file", "name": "a.py"}, "body": "on a"},
            author="ada")
    build_page(ctx, author="ada")
    await user.open("/map")
    (tiles,) = user.find(marker="map-tiles").elements
    assert (tiles.props["role"], tiles.props["aria-label"]) == ("toolbar", "map tiles")
    back = lambda arg: UserInteraction(user, {user.client.layout}, None).trigger("sbMapTile", arg)  # noqa: E731
    back(_map_nodes(user)["file/a.py"]["id"])
    await user.should_see(marker="map-row")
    for whole in (None, 9999, "x"):
        back(whole)
        await user.should_not_see(marker="map-row")
        await user.should_see("A click on a tile lists its entries here")
        back(_map_nodes(user)["file/a.py"]["id"])
        await user.should_see(marker="map-row")


async def test_a_tile_no_page_lists_says_how_to_narrow_it(user: User, repo, tmp_path,
                                                         monkeypatch):
    """A directory's entries are on several targets, and no page lists
    exactly them: past PANEL_ROWS the panel says how to narrow them, and links
    nowhere. A group narrows to a tile in it; a leaf, which a click leaves in
    place, to a search or another axis."""
    monkeypatch.setitem(build_page.__globals__, "PANEL_ROWS", 1)
    ctx = _map_store(tmp_path)
    for name in ("src/a.py", "src/b.py"):
        api.add(ctx, {"kind": "note", "target": {"type": "file", "name": name}, "body": "b"},
                author="ada")
    build_page(ctx, author="ada")
    await user.open("/map")
    user.find(marker="map-chart").trigger("componentClick", _map_nodes(user)["file/src"]["id"])
    await user.should_see("+1 more: click a tile in it to narrow it")
    await user.should_not_see(marker="map-all")
    await user.open("/map?by=week")
    (week,) = _map_nodes(user).values()
    user.find(marker="map-chart").trigger("componentClick", week["id"])
    await user.should_see("+1 more: search or pick another axis to narrow it")
    await user.should_not_see(marker="map-all")


async def test_a_repos_type_nests_by_directory_on_the_map(user: User, tmp_path, monkeypatch):
    """With [repos], each repo is a type of its own, and its names are paths."""
    r = tmp_path / "net"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / ".symbion").write_text("../work-notes\n")
    store.ensure_store(tmp_path / "work-notes")
    (tmp_path / "work-notes" / "symbion.toml").write_text('[repos]\nnet = "../net"\n')
    monkeypatch.chdir(r)
    ctx = api.resolve()
    for name in ("roles/web/tasks.yml", "roles/db/tasks.yml"):
        api.add(ctx, {"kind": "note", "target": {"type": "net", "name": name}, "body": "b"},
                author="ada")
    build_page(ctx, author="ada")
    await user.open("/map")
    assert set(_map_nodes(user)) == {"net/roles", "web/tasks.yml", "db/tasks.yml"}


async def test_a_search_on_the_map_maps_what_it_finds(user: User, repo, tmp_path, monkeypatch):
    """The map has no cards to filter: its box's Enter maps only the entries
    the search finds, the view kept (the user, 2026-10-29). The crumb before
    the search is the whole map, and a tile's link to all carries the words."""
    monkeypatch.setitem(build_page.__globals__, "PANEL_ROWS", 1)
    ctx = _map_store(tmp_path)
    for body, tag in (("treemap colours", "gui"), ("treemap tooltips", "gui"),
                      ("other work", "gui")):
        api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "x"}, "body": body,
                      "tags": [tag]}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/map?by=tag")
    went = []
    monkeypatch.setattr(user.navigate, "to", lambda target, new_tab=False: went.append(target))
    user.find(marker="search").trigger("keydown.enter", "  treemap ")
    assert went == ["/map?by=tag&q=treemap"]
    await user.open("/map?by=tag&q=treemap")
    assert _map_nodes(user)["#gui"]["value"] == 2
    (crumbs,) = user.find(marker="crumbs").elements
    assert [e.props["href"] for e in crumbs.descendants() if "href" in e.props] == ["/map?by=tag"]
    await user.should_see("search: treemap")
    user.find(marker="map-chart").trigger("componentClick", _map_nodes(user)["#gui"]["id"])
    await user.should_see(marker="map-all")
    (more,) = user.find(marker="map-all").elements
    assert more.props["href"] == "/notes?tag=gui&q=treemap"
    await user.open("/map?q=nothing+here")
    await user.should_see(marker="map-empty")


async def test_a_superseded_id_on_the_map_maps_its_current_entry(user: User, repo, tmp_path):
    """As /notes: an id other rows cite may be an earlier version's."""
    ctx = _map_store(tmp_path)
    t = api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "z"}, "body": "before"},
                author="ada")
    api.supersede(ctx, t.id, author="ada", body="after")
    rows = store.load(tmp_path)
    (saved,) = [n.id for n in rows if n.id != t.id and store.chain_tip(rows, n.id).id == t.id]
    build_page(ctx, author="ada")
    await user.open(f"/map?q={saved}")
    assert set(_map_nodes(user)) == {"item/z"}


async def test_a_map_of_nothing_says_so(user: User, repo, tmp_path):
    ctx = _map_store(tmp_path)
    api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "x"}, "body": "b"}, author="ada")
    build_page(ctx, author="ada")
    await user.open("/map?show=open")
    await user.should_see(marker="map-empty")
    await user.should_not_see(marker="map-chart")


async def test_the_map_names_the_other_sides_author(user: User, shared, tmp_path):
    """As every other page reads a shared store: another side's agent is
    someone else, so its tile is its own and its link lists the same rows."""
    _both_sides_ask(shared, tmp_path)
    await user.open("/map?by=author")
    assert "sam (B)" in _map_nodes(user)
