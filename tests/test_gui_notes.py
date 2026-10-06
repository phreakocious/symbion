from __future__ import annotations

import pytest

pytest.importorskip("nicegui")

from nicegui import ui                      # noqa: E402
from nicegui.testing import User            # noqa: E402

from symbion import api, store              # noqa: E402
from symbion.gui.notes import render_note   # noqa: E402

pytestmark = pytest.mark.usefixtures("tmp_store")


async def test_renders_body_kind_and_tags(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "check", "target": {"type": "project", "name": None},
                      "checked": "the suite", "result": "412 passed",
                      "body": "ran on a clean tree", "tags": ["release"]},
                author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("ran on a clean tree")   # markdown body
    await user.should_see("check")                 # kind chip
    await user.should_see("412 passed")            # check result
    await user.should_see("#release")              # tag chip
    await user.should_see("ada")                   # author


async def test_check_badge_says_why_it_is_unverifiable(user: User, repo, tmp_path):
    """check_state collapses three causes into 'unverifiable'; the badge must
    say which, or a dirty stamp reads like a squashed sha."""
    ctx = api.resolve(str(tmp_path))
    n = store.add(tmp_path, kind="check", target={"type": "project", "name": None},
                  checked="x", result="y", author="t",
                  provenance={"sha": "0" * 40, "dirty": True})

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("unverifiable")
    await user.should_see("dirty tree")


async def test_a_row_with_no_body_leads_with_its_target(user: User, repo, tmp_path):
    """A seeded box has no text: its target is what it says, and the card
    led with an empty line and put the target in a chip below. Both
    directions: a row with a body keeps its target chip and no headline."""
    ctx = api.resolve(str(tmp_path))
    bare = api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "alpha"}},
                   author="ada")
    said = api.add(ctx, {"kind": "task", "target": {"type": "item", "name": "beta"},
                         "body": "words"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, bare, lambda: None, author="ada", show_target=True)

    @ui.page("/u")
    def page_u():
        render_note(ctx, said, lambda: None, author="ada", show_target=True)

    await user.open("/t")
    await user.should_see("alpha")
    [lead] = user.find(marker="note-headline").elements
    assert lead.props["href"] == "/object?type=item&name=alpha"
    assert len(user.find(marker="note-target").elements) == 1, "the headline, not a chip too"
    await user.open("/u")
    await user.should_see("words")
    await user.should_not_see(marker="note-headline")
    assert len(user.find(marker="note-target").elements) == 1


async def test_a_row_with_no_body_on_its_own_page_leads_with_its_arc(user: User, repo, tmp_path):
    """On the object's own page the target is the page: the arc is what is
    left to say, by its name, and the arc chip would say it twice."""
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "Audit for accuracy", "d", "item", author="ada")
    [n] = api.seed(ctx, act.id, "item", ["alpha"], author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada", show_target=False)

    await user.open("/t")
    await user.should_see("Audit for accuracy")
    [lead] = user.find(marker="note-headline").elements
    assert lead.props["href"] == f"/arc?id={act.id}"
    await user.should_not_see(marker="note-arc")


async def test_a_check_with_no_stamp_reads_unstamped(user: User, repo, tmp_path):
    from datetime import datetime, timedelta
    ctx = api.resolve(str(tmp_path))
    five = (datetime.now().astimezone() - timedelta(days=5, hours=1)).isoformat(timespec="seconds")
    n = store.add(tmp_path, kind="check", target={"type": "project", "name": None},
                  checked="x", result="y", author="t", created_at=five)

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("unstamped — 5d ago")
    await user.should_not_see("unverifiable")


async def test_an_external_check_badge_reads_external(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "check", "target": {"type": "item", "name": "dns"},
                      "checked": "dig", "result": "ok", "external": True}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("external — <1d ago")
    await user.should_not_see("current")


async def test_tag_chip_is_a_link_into_the_filtered_view(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b", "tags": ["priority"]}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    # By marker, not by text: the star button's tooltip also says "#priority",
    # and find() returns a SET -- popping it is a coin flip between the two.
    link = user.find(marker="tag-priority").elements.pop()
    assert link.props["href"] == "/notes?tag=priority"


async def test_a_clean_check_badge_reads_current(user: User, repo, tmp_path):
    """The other direction of the badge. Only ever asserting 'unverifiable'
    would pass on a render_note that can report nothing else."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "check", "target": {"type": "commit", "name": "HEAD"},
                      "checked": "x", "result": "y"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("current")
    await user.should_not_see("unverifiable")


async def test_an_open_pre_registration_badge_reads_pending(user: User, repo, tmp_path):
    """Stamped at registration, before the run: `current` read as run."""
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nprediction = { status = true, verdict = true }\n')
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "prediction", "target": {"type": "item", "name": "p"},
                      "checked": "the sweep"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("pending")
    await user.should_not_see("current")


async def test_a_behind_badge_lists_the_commits_since_its_stamp(user: User, repo, tmp_path):
    """The badge lit up on hover and a click did nothing (the owner's bug).
    Its tooltip now names the stamp and the commits `behind N` counts."""
    import subprocess
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "check", "target": {"type": "commit", "name": "HEAD"},
                      "checked": "x", "result": "y"}, author="ada")
    for i in range(10):
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", f"later {i}"],
                       check=True, cwd=repo)
    from symbion import gitref
    from symbion.gui.notes import TIP_COMMITS, check_tip
    state, distance = gitref.check_state(ctx.cfg, n.provenance)
    assert (state, distance) == ("behind", 10)
    tip = check_tip(ctx.cfg, n.provenance, state, distance).splitlines()
    assert tip[0] == f"stamped at {n.provenance['sha'][:7]}; HEAD is 10 commits past it:"
    assert [t.split(" ", 1)[1] for t in tip[1:-1]] == \
        [f"later {i}" for i in range(9, 9 - TIP_COMMITS, -1)]      # newest first
    assert tip[-1] == f"+{10 - TIP_COMMITS} more"

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    await user.should_see("behind 10")
    await user.should_not_see(f"stamped at {n.provenance['sha'][:7]}")   # not yet: lazy
    user.find(marker="check-state").trigger("mouseenter")
    await user.should_see(f"stamped at {n.provenance['sha'][:7]}")       # the tooltip


def test_a_tip_names_the_stamp_or_nothing():
    from symbion.gui.notes import check_tip
    sha = "0123456789abcdef"
    assert check_tip(None, {"sha": sha}, "current", 0) == "stamped at 0123456, this checkout's HEAD"
    assert check_tip(None, {"sha": sha}, "diverged", None).startswith("stamped at 0123456, on another")
    assert check_tip(None, None, "unverifiable", None) == ""
    assert check_tip(None, {"external": True, "at": "x"}, "external", None) == ""


async def test_an_id_cited_in_a_body_links_to_that_note(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    cited = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                          "body": "the cited row"}, author="ada")
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": f"settled by {cited.id}; the code `{cited.id}` stays text"},
                author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (md,) = user.find(ui.markdown).elements
    html = md.props["innerHTML"]                  # rendered; .content is the source
    assert html.count(f'href="/notes?id={cited.id}"') == 1, html
    assert f"<code>{cited.id}</code>" in html


async def test_a_cited_tail_of_a_row_links_to_it(user: User, repo, tmp_path):
    """The owner, 2026-10-01: rows cite the 10-character tail, and only a
    full id linked. A tail links only to a row this store has: rows cite
    other stores' rows too."""
    ctx = api.resolve(str(tmp_path))
    cited = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                          "body": "the cited row"}, author="ada")
    tail = cited.id[-10:]
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": f"see {tail} and 999999-fff; the code `{tail}` stays text"},
                author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (md,) = user.find(ui.markdown).elements
    html = md.props["innerHTML"]
    assert html.count(f'href="/notes?id={cited.id}"') == 1, html
    assert "999999-fff" in html and "id=999999-fff" not in html, html
    assert f"<code>{tail}</code>" in html, html

    # A row written while the server runs: its tail links on the next render.
    later = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                          "body": "written later"}, author="ada")
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": f"see {later.id[-10:]}"}, author="ada")
    await user.open("/t")
    (md,) = user.find(ui.markdown).elements
    assert f'href="/notes?id={later.id}"' in md.props["innerHTML"]


async def test_a_tag_in_a_body_prints_as_text(user: User, repo, tmp_path):
    """A body is read at a terminal too, where `<pre>` is five characters.
    markdown2 passes raw tags through and DOMPurify keeps the legal ones: a
    `<pre>` in prose set the rest of a row in one unwrapped monospace line,
    and `<td>` and a `<repo>` placeholder vanished."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "a <td> cell; ../<repo>-notes; wrap it in <pre> tags; "
                              "the code `<pre>`; <https://example.org>"},
                author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (md,) = user.find(ui.markdown).elements
    html = md.props["innerHTML"]
    assert "&lt;td&gt; cell" in html and "../&lt;repo&gt;-notes" in html, html
    assert "&lt;pre&gt; tags" in html, html
    assert "<pre" not in html and "<td" not in html, html
    assert "<code>&lt;pre&gt;</code>" in html, html          # escaped once, not twice
    assert '<a href="https://example.org">' in html, html


async def test_a_body_reads_as_it_does_at_a_terminal(user: User, repo, tmp_path):
    """The GUI read bodies with markdown2 and the terminal with CommonMark:
    a list right under a line of prose was a list on a tty and one run-on
    paragraph here, and `~~x~~` kept its tildes. One parser now. `_` still
    makes no emphasis, as markdown2's code-friendly kept it, and a fenced
    block in a language pygments knows is still coloured."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "Steps:\n1. a sample\n2. a test\n\n"
                              "~~gone~~ and __init__.py\n\n```python\nx = 1\n```\n\n"
                              "```nosuchlang\n<y>\n```"},
                author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (md,) = user.find(ui.markdown).elements
    html = md.props["innerHTML"]
    assert "<ol>" in html and "<li>a sample</li>" in html, html
    assert "<s>gone</s>" in html, html
    assert "__init__.py" in html and "<strong>" not in html, html
    assert '<pre class="codehilite"><code><span class="n">x</span>' in html, html
    assert '<code class="language-nosuchlang">&lt;y&gt;\n</code>' in html, html


async def test_a_compact_row_clips_a_long_verdict_and_a_full_one_does_not(
        user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    long = "pytest " + "--flag " * 40 + "END"
    n = api.add(ctx, {"kind": "check", "target": {"type": "project", "name": None},
                      "checked": long, "result": "ok"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada", compact=True)
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    short, full = sorted((e.text for e in user.find(marker="note-verdict").elements), key=len)
    assert "END" not in short and "…" in short
    assert full == f"checked: {long}   →   result: ok"
    # A check's verdict is its content, not metadata: one with no body
    # showed only this line, dim, beside bodies in Text (owner, 2026-10-01).
    assert all("sb-verdict" in e.classes for e in user.find(marker="note-verdict").elements)


async def test_icon_buttons_have_names_a_screen_reader_can_read(user: User, repo, tmp_path):
    """An icon-only button reads as its icon's ligature ("star_outline")."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "b"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    for marker, name in (("note-star", "toggle priority"), ("note-resolve", "resolve"),
                         ("note-edit", "edit")):
        (b,) = user.find(marker=marker).elements
        assert b.props.get("aria-label") == name, (marker, b.props)


async def test_a_cards_icon_buttons_take_the_theme_colour(user: User, repo, tmp_path):
    """A NiceGUI button is Quasar's primary unless told otherwise, and that
    `text-primary` is `!important` in NiceGUI's last layer: the star, edit
    and copy-id buttons rendered accent over theme.py's body and muted
    (measured in serve, 2026-10-02). They carry no colour of their own; a
    starred row's star is `warning`."""
    ctx = api.resolve(str(tmp_path))
    for tags in ([], ["priority"]):
        api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "b", "tags": tags}, author="ada")
    rows = store.heads(store.load(tmp_path))

    @ui.page("/t")
    def page():
        for n in rows:
            render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    for marker in ("note-edit", "note-copy-id"):
        colours = [b.props.get("color") for b in user.find(marker=marker).elements]
        assert colours == [None, None], (marker, colours)
    stars = sorted(str(b.props.get("color")) for b in user.find(marker="note-star").elements)
    assert stars == ["None", "warning"]


async def test_the_id_links_to_the_note_alone(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (link,) = user.find(marker="note-id").elements
    assert link.props["href"] == f"/notes?id={n.id}"


async def test_a_non_project_target_links_to_the_object_view(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "item", "name": "src/x.py"},
                      "body": "b"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada", show_target=True)

    await user.open("/t")
    link = user.find(marker="note-target").elements.pop()
    assert link.props["href"] == "/object?type=item&name=src%2Fx.py"


# ---- write surfaces ----
async def test_star_button_toggles_priority_through_retag(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b", "tags": ["keep"]}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-star").click()

    head = store.heads(store.load(tmp_path))[0]
    assert set(head.tags) == {"keep", "priority"}
    assert head.author == "ada", "a GUI write is the human's"


async def test_star_button_unstars_an_already_starred_note(user: User, repo, tmp_path):
    """The other direction. A star asserted only on the way in passes on a
    button that can only ever add the tag."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b", "tags": ["keep", "priority"]}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-star").click()

    assert set(store.heads(store.load(tmp_path))[0].tags) == {"keep"}


async def test_resolve_button_only_on_open_stateful_kinds(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    bug = api.add(ctx, {"kind": "bug",
                            "target": {"type": "project", "name": None},
                            "body": "broken"}, author="ada")
    decision = api.add(ctx, {"kind": "decision",
                             "target": {"type": "project", "name": None},
                             "body": "chose x"}, author="ada")

    @ui.page("/a")
    def page_a():
        render_note(ctx, bug, lambda: None, author="ada")

    @ui.page("/d")
    def page_d():
        render_note(ctx, decision, lambda: None, author="ada")

    await user.open("/a")
    assert user.find(marker="note-resolve").elements, "an open bug resolves"

    await user.open("/d")
    with pytest.raises(AssertionError):
        user.find(marker="note-resolve")     # a decision carries no status


async def test_add_form_writes_through_api_with_provenance(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    from symbion.gui.notes import add_form

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-body").type("written in the gui")
    user.find(marker="note-add").click()

    rows = store.load(tmp_path)
    assert len(rows) == 1
    assert rows[0].author == "ada" and rows[0].body == "written in the gui"


async def test_a_check_added_in_the_gui_carries_provenance(user: User, repo, tmp_path):
    """api.add stamps provenance by kind. Without it a GUI check is a claim
    about a tree with no record of which tree."""
    ctx = api.resolve(str(tmp_path))
    from symbion.gui.notes import add_form

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-kind-select").click()
    user.find("check").click()
    user.find(marker="note-checked").type("the suite")
    user.find(marker="note-result").type("254 passed")
    user.find(marker="note-add").click()

    rows = store.load(tmp_path)
    assert len(rows) == 1, f"expected one row, got {[r.kind for r in rows]}"
    assert rows[0].kind == "check"
    assert rows[0].provenance and rows[0].provenance.get("sha")


# ---- tags and arc on the write surfaces ----
def test_split_tags_accepts_every_way_a_person_types_a_list():
    from symbion.gui.notes import split_tags
    assert split_tags("gui, ux") == ["gui", "ux"]
    assert split_tags("#gui #ux") == ["gui", "ux"]
    assert split_tags("gui,ux  #seed") == ["gui", "ux", "seed"]
    assert split_tags("") == [] and split_tags(None) == []
    assert split_tags("  ,  # ") == [], "separators alone are not a tag"


async def test_a_hashtag_in_the_body_becomes_a_tag(user: User, repo, tmp_path):
    """The bug: '#gui' typed in the body was stored as prose and the
    note came back with tags=[]."""
    ctx = api.resolve(str(tmp_path))
    from symbion.gui.notes import add_form

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-body").type("#gui replace the nicegui art")
    user.find(marker="note-add").click()

    row = store.load(tmp_path)[0]
    assert row.tags == ("gui",)
    assert row.body == "replace the nicegui art", "the tag is a column, not prose"


async def test_a_body_with_no_hashtag_still_stores_its_prose(user: User, repo, tmp_path):
    """The other direction: the harvest must not eat ordinary bodies."""
    ctx = api.resolve(str(tmp_path))
    from symbion.gui.notes import add_form

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-body").type("no hashes here at all")
    user.find(marker="note-add").click()

    row = store.load(tmp_path)[0]
    assert row.body == "no hashes here at all"
    assert row.tags == ()


async def test_a_note_can_be_filed_under_an_arc_on_creation(
        user: User, repo, tmp_path):
    """The gap: nothing in the GUI set arc_id."""
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "theming", "d", "project", author="ada")
    from symbion.gui.notes import arc_options, add_form

    assert act.id in arc_options(ctx)

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-arc-select").click()
    user.find("theming").click()
    user.find(marker="note-body").type("filed under the arc")
    user.find(marker="note-add").click()

    row = store.load(tmp_path)[0]
    assert row.arc_id == act.id


def test_an_archived_arc_is_not_offered(repo, tmp_path):
    """Offering a closed ticket invites reopening one by accident."""
    ctx = api.resolve(str(tmp_path))
    from symbion.gui.notes import arc_options
    act = api.create_arc(ctx, "old", "d", "project", author="ada")
    assert act.id in arc_options(ctx)
    api.archive_arc(ctx, act.id)
    assert act.id not in arc_options(ctx)


def test_supersede_can_attach_an_existing_note_to_an_arc(repo, tmp_path):
    """What the edit dialog does. supersede refuses target and provenance;
    arc_id and tags are neither, so an already-written note is filable."""
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "theming", "d", "project", author="ada")
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "written before the arc existed"}, author="ada")
    assert n.arc_id is None

    out = api.supersede(ctx, n.id, author="ada", arc_id=act.id, tags=["gui"])
    assert out.arc_id == act.id
    assert out.tags == ("gui",)
    assert out.target == n.target, "target is still inherited"


async def test_a_project_target_links_to_the_note_itself(user: User, repo, tmp_path):
    """The other direction of the target link. `project` has no object page --
    /object redirects when name is empty -- so before /notes?id= this row was a
    dead label, and a project note filed under an arc rendered on the
    arc page with nothing to click."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada", show_target=True)

    await user.open("/t")
    link = user.find(marker="note-target").elements.pop()
    assert link.props["href"] == f"/notes?id={n.id}"


async def test_edit_dialog_corrects_a_check_verdict(user: User, repo, tmp_path):
    """The GUI half of the same gap: the edit dialog offered body, tags and
    arc, so the two fields that make a check queryable were unreachable from
    either surface."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "check", "target": {"type": "project", "name": None},
                      "checked": "the suit", "result": "412 pased",
                      "body": "ran it"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-edit").click()
    await user.should_see(marker="edit-checked")
    user.find(marker="edit-checked").elements.pop().set_value("the suite")
    user.find(marker="edit-result").elements.pop().set_value("412 passed")
    user.find(marker="edit-save").click()

    head = store.heads(store.load(tmp_path))[0]
    assert (head.checked, head.result) == ("the suite", "412 passed")
    assert head.body == "ran it"


async def test_edit_dialog_hides_the_verdict_fields_on_a_non_check(
        user: User, repo, tmp_path):
    """The other direction. Rendered unconditionally, every note would offer
    two fields that mean nothing on it -- and a save would then write
    checked=None onto rows that never had the field."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "prose"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-edit").click()
    await user.should_see(marker="edit-body")
    await user.should_not_see(marker="edit-checked")


PREREG = ('[kinds]\nprediction = { status = true, verdict = true }\n'
          'bug = { status = true }\nnote = {}\n')


def _declare(store_dir, text):
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(text)


def test_kind_class_is_by_label_as_in_the_terminal():
    """A kind the store declares has no colour of its own: it takes the one
    every declared kind shares, as a `list` row prints it."""
    from symbion.gui.notes import kind_class
    assert kind_class("bug") == "sb-kind-bug"
    assert kind_class("decision") == "sb-kind-decision"
    assert kind_class("measurement") == "sb-kind-own"


async def test_add_form_lists_exactly_the_declared_kinds(user: User, repo, tmp_path):
    from symbion.gui.notes import add_form
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\nfact = {}\n')
    ctx = api.resolve(str(tmp_path))

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    sel = user.find(marker="note-kind-select").elements.pop()
    assert sel.options == ["anomaly", "fact"]
    assert sel.value == "anomaly", "no `note` declared: the first label is the default"


async def test_add_form_shows_the_verdict_fields_on_exactly_the_verdict_kinds(
        user: User, repo, tmp_path):
    from symbion.gui.notes import add_form
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    sel = user.find(marker="note-kind-select").elements.pop()
    # note-checked starts hidden (default kind is "note", no verdict bit), and
    # User.find()'s only_visible=True default can never hand back a hidden
    # element -- so the handle is grabbed AFTER the field is made visible, not
    # before. The other direction still exercises the live handle, not find().
    sel.set_value("prediction")
    checked = user.find(marker="note-checked").elements.pop()
    assert checked.visible
    sel.set_value("bug")
    assert not checked.visible


async def test_resolve_button_on_a_prediction_opens_the_dialog_instead_of_resolving(
        user: User, repo, tmp_path):
    """Both directions: the click does not resolve; saving with a result does."""
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "project", "name": None},
                      "body": "falsified if"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, p, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-resolve").click()
    assert store.read_status(store.heads(store.load(tmp_path))[0]) == "open"
    user.find(marker="edit-save").click()                     # empty result: refused
    await user.should_see("result")
    assert store.read_status(store.heads(store.load(tmp_path))[0]) == "open"
    user.find(marker="edit-result").elements.pop().set_value("HELD")
    user.find(marker="edit-save").click()
    head = store.heads(store.load(tmp_path))[0]
    assert (store.read_status(head), head.result, head.author) == ("resolved", "HELD", "ada")


async def test_resolve_button_on_a_status_only_kind_still_resolves_in_one_click(
        user: User, repo, tmp_path):
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))
    b = api.add(ctx, {"kind": "bug", "target": {"type": "project", "name": None},
                      "body": "broken"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, b, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-resolve").click()
    assert store.read_status(store.heads(store.load(tmp_path))[0]) == "resolved"


# ---- due ----
async def test_an_open_row_past_due_shows_it_and_a_resolved_one_does_not(
        user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    late = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                         "body": "late", "due": "2000-01-01"}, author="ada")
    done = store.supersede(tmp_path, late.id, status="resolved")

    @ui.page("/late")
    def page_late():
        render_note(ctx, late, lambda: None, author="ada")

    @ui.page("/done")
    def page_done():
        render_note(ctx, done, lambda: None, author="ada")

    await user.open("/late")
    chip = user.find(marker="note-due").elements.pop()
    assert chip.text.startswith("overdue ") and "sb-chip-bad" in chip.classes
    await user.open("/done")
    chip = user.find(marker="note-due").elements.pop()
    assert chip.text == "due 2000-01-01" and "sb-chip-bad" not in chip.classes


async def test_edit_dialog_keeps_an_untouched_datetime_due(user: User, repo, tmp_path):
    """A native date input cannot hold a datetime: it would render empty, and
    a save that only fixed a typo in the body would clear the due date."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "typo", "due": "2026-10-01T22:30Z"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-edit").click()
    await user.should_see(marker="edit-due")
    user.find(marker="edit-body").elements.pop().set_value("fixed")
    user.find(marker="edit-save").click()
    head = store.heads(store.load(tmp_path))[0]
    assert (head.body, head.due) == ("fixed", "2026-10-01T22:30:00+00:00")


async def _append_in_dialog(user: User, ctx, n, text: str, *, body: str | None = None,
                            meanwhile=None, keys: bool = False):
    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-edit").click()
    await user.should_see(marker="edit-append")
    if body is not None:
        user.find(marker="edit-body").elements.pop().set_value(body)
    user.find(marker="edit-append").type(text)
    if meanwhile:
        meanwhile()
    if keys:
        user.find(marker="edit-append").trigger("keydown.meta.enter")
    else:
        user.find(marker="edit-save").click()
    return store.heads(store.load(ctx.store_dir))[0]


async def test_the_edit_dialog_adds_text_after_the_body(user: User, repo, tmp_path):
    """Most edits add to a row, and the body's end was a scroll away (the
    owner, 2026-10-01). A #tag in the added text is a tag, as in the body.
    ⌘ Enter saves, as it adds a note in the composer."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "first"}, author="ada")
    head = await _append_in_dialog(user, ctx, n, "more #ux", keys=True)
    assert (head.body, list(head.tags)) == ("first\n\nmore", ["ux"])


async def test_the_added_text_lands_after_a_revision_made_meanwhile(user: User, repo,
                                                                     tmp_path):
    """The text goes after the current row's body, read under the lock, as
    `supersede --append` does: a body written from this dialog's copy lost
    a revision made since it opened."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "first"}, author="ada")
    head = await _append_in_dialog(
        user, ctx, n, "more",
        meanwhile=lambda: api.supersede(ctx, n.id, author="sam", body="second"))
    assert head.body == "second\n\nmore"


async def test_an_edited_body_takes_the_added_text_after_it(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "frist"}, author="ada")
    head = await _append_in_dialog(user, ctx, n, "more", body="first")
    assert head.body == "first\n\nmore"


async def test_the_edit_dialog_stars_a_row_in_the_same_write(user: User, repo, tmp_path):
    """The owner, 2026-10-02: "supersede should include a priority toggle
    for a single op". The switch holds `priority`, the tags field the rest."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "first", "tags": ["ux"]}, author="ada")
    api.commit(ctx, "notes")                  # an edit after a commit adds a row
    flip = lambda: user.find(marker="edit-priority").click()     # noqa: E731
    head = await _append_in_dialog(user, ctx, n, "more", meanwhile=flip)
    assert (head.body, list(head.tags)) == ("first\n\nmore", ["ux", "priority"])
    assert len(store.load(tmp_path)) == 2     # one write: the row and its edit


async def test_the_edit_dialog_unstars_a_row(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "first", "tags": ["priority", "ux"]}, author="ada")

    def flip():
        assert user.find(marker="edit-tags").elements.pop().value == "ux"
        user.find(marker="edit-priority").click()

    head = await _append_in_dialog(user, ctx, n, "more", meanwhile=flip)
    assert list(head.tags) == ["ux"]


async def test_edit_dialog_sets_and_clears_due(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": {"type": "project", "name": None},
                      "body": "b"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, store.heads(store.load(tmp_path))[0], lambda: None, author="ada")

    for value, want in (("2026-10-01", "2026-10-01"), ("", None)):
        await user.open("/t")
        user.find(marker="note-edit").click()
        await user.should_see(marker="edit-due")
        user.find(marker="edit-due").elements.pop().set_value(value)
        user.find(marker="edit-save").click()
        assert store.heads(store.load(tmp_path))[0].due == want


async def test_edit_dialog_offers_due_only_on_a_status_kind(user: User, repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "prose"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-edit").click()
    await user.should_see(marker="edit-body")
    await user.should_not_see(marker="edit-due")


async def test_add_form_takes_due_on_a_status_kind(user: User, repo, tmp_path):
    from symbion.gui.notes import add_form
    ctx = api.resolve(str(tmp_path))

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    sel = user.find(marker="note-kind-select").elements.pop()
    sel.set_value("task")
    due = user.find(marker="note-due").elements.pop()      # visible only now
    due.set_value("2026-10-01")
    user.find(marker="note-body").type("with a date")
    user.find(marker="note-add").click()
    assert store.load(tmp_path)[0].due == "2026-10-01"
    sel.set_value("note")
    assert not due.visible


# ---- the card layout (2026-10-01): age, short id ----

def test_a_rows_age_is_one_unit():
    """A row's stamp to the second, with its zone, was the widest thing on
    its line; the full stamp is the tooltip now."""
    from datetime import datetime, timedelta, timezone
    from symbion.gui.notes import ago
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    at = lambda **kw: (now - timedelta(**kw)).isoformat()   # noqa: E731
    assert [ago(at(seconds=5), now), ago(at(minutes=17), now), ago(at(hours=4), now),
            ago(at(days=3), now)] == ["now", "17m", "4h", "3d"]
    assert ago(at(days=90), now) == "2026-07-03"
    assert ago("not a stamp", now) == "not a stamp"
    # a stamp with no offset reads as local, as `store.written_at` reads it;
    # stores written before stamps carried one hold thousands of them
    naive = (now - timedelta(hours=4)).astimezone().replace(tzinfo=None).isoformat()
    assert ago(naive, now) == "4h"


def test_a_rows_id_shows_its_last_two_parts():
    """As rows cite one another: the date and time are the row's age."""
    from symbion.gui.notes import short_id
    assert short_id("20260102-030405-735180-a9c") == "735180-a9c"


# ---- the owner's GUI rows of 2026-10-01 ----

async def test_the_resolve_box_comes_after_the_text(user: User, repo, tmp_path):
    """On the left it pushed an open row's text in past a closed row's."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "bug", "target": {"type": "project", "name": None},
                      "body": "broken"}, author="ada")

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (row,) = user.find(marker="note-row").elements
    kids = list(row.default_slot.children)
    (box,) = user.find(marker="note-resolve").elements
    assert "sb-resolvable" in row.classes
    assert "sb-note-main" in kids[0].classes and kids.index(box) > 0


async def test_the_copy_button_puts_the_whole_id_on_the_clipboard(user: User, repo, tmp_path,
                                                                 monkeypatch):
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "cite me"}, author="ada")
    copied = []
    monkeypatch.setattr(ui.clipboard, "write", copied.append)

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-copy-id").click()
    await user.should_see(f"copied {n.id}")
    assert copied == [n.id]


async def test_a_tags_field_drops_its_hashes_when_left(user: User, repo, tmp_path):
    from symbion.gui.notes import add_form
    ctx = api.resolve(str(tmp_path))

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    tags = user.find(marker="note-tags")
    (field,) = tags.elements
    field.set_value("#ux, ##gui")           # type() takes NiceGUI's own inputs only
    tags.trigger("blur")
    assert field.value == "ux, gui"


async def test_the_tags_fields_offer_the_stores_tags_most_used_first(user: User, repo,
                                                                     tmp_path):
    """Tags should offer the ones in use as you type (the owner, 2026-10-01).
    `tokens`: the menu completes the field's last word, not the whole text."""
    from symbion.gui.notes import add_form
    ctx = api.resolve(str(tmp_path))
    for tags in (["ux"], ["gui", "ux"], ["perf"]):
        n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                          "body": "b", "tags": tags}, author="ada")

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")
        render_note(ctx, n, lambda: None, author="ada")

    await user.open("/t")
    (tags,) = user.find(marker="note-tags").elements
    assert (tags.props["options"], tags.props["tokens"]) == (["ux", "gui", "perf"], True)
    user.find(marker="note-edit").click()
    await user.should_see(marker="edit-tags")
    (tags,) = user.find(marker="edit-tags").elements
    assert (tags.props["options"], tags.props["tokens"]) == (["ux", "gui", "perf"], True)


async def test_a_bang_in_the_body_picks_the_kind_and_leaves_the_body(user: User, repo, tmp_path):
    from symbion.gui.notes import add_form
    ctx = api.resolve(str(tmp_path))

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    (add,) = user.find(marker="note-add").elements
    assert add.text == "Add note"
    user.find(marker="note-body").type("!task fix the header #gui")
    assert user.find(marker="note-kind-select").elements.pop().value == "task"   # as typed
    # The button names the kind, so a request is not filed as a note unseen
    # (the owner's row, 2026-10-02): a `note` reaches no open view.
    assert add.text == "Add task"
    user.find(marker="note-add").click()
    (row,) = store.heads(store.load(tmp_path))
    assert (row.kind, row.body, list(row.tags)) == ("task", "fix the header", ["gui"])


async def test_a_bang_alone_is_an_empty_note(user: User, repo, tmp_path):
    from symbion.gui.notes import add_form
    ctx = api.resolve(str(tmp_path))

    @ui.page("/t")
    def page():
        add_form(ctx, "project", None, lambda: None, author="ada")

    await user.open("/t")
    user.find(marker="note-body").type("!task")
    user.find(marker="note-add").click()
    await user.should_see("empty note")
    assert not store.heads(store.load(tmp_path))


async def test_the_edit_dialog_says_whether_the_row_itself_changes(user: User, repo, tmp_path):
    """It said "the old row is kept" over an edit that rewrites the row: one
    by its own author before `symbion commit` (store.rewritable)."""
    ctx = api.resolve(str(tmp_path))
    rows = {who: api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                               "body": f"by {who}"}, author=who) for who in ("ada", "sam")}

    @ui.page("/t/{who}")
    def page(who: str):
        render_note(ctx, rows[who], lambda: None, author="ada")

    for who, words in (("ada", "not yet committed, so the row itself changes"),
                       ("sam", "the old row is kept")):
        await user.open(f"/t/{who}")
        user.find(marker="note-edit").click()
        await user.should_see(words)


async def test_a_since_chip_lists_the_commits_it_counts(user: User, repo, tmp_path):
    """"2 commits since" asks the reader to look at them, as `behind N` does,
    so the tip lists them, newest first, filled on hover."""
    import subprocess
    from symbion import gitref
    from symbion.gui.notes import since_tip
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "file", "name": "f"}, "body": "b"},
                author="ada")
    shas = []
    for i in range(2):
        (repo / "f").write_text(f"v{i}")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam",
                        f"edit {i}"], check=True, cwd=repo)
        shas.append(gitref.head_sha(ctx.cfg))
    assert since_tip(ctx.cfg, n).splitlines() == [
        "f changed in 2 commits since this row:",
        f"{shas[1][:7]} edit 1", f"{shas[0][:7]} edit 0"]

    @ui.page("/t")
    def page():
        render_note(ctx, n, lambda: None, author="ada",
                    since=gitref.commits_since(ctx.cfg, [n])[n.id])

    await user.open("/t")
    await user.should_see("2 commits since")
    await user.should_not_see("edit 1")                     # not yet: lazy
    user.find(marker="note-since").trigger("mouseenter")
    await user.should_see("edit 1")
