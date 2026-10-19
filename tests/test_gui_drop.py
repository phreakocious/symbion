"""A file dropped on a page (the drop design, 2026-10-15). The `user`
fixture runs no client JS, so these fire the event `_DROP_JS` sends."""
from __future__ import annotations

import pytest

pytest.importorskip("nicegui")

from nicegui import ui                                          # noqa: E402
from nicegui.testing import User                                # noqa: E402
from nicegui.testing.user_interaction import UserInteraction    # noqa: E402

from symbion import api, config, store                          # noqa: E402
from symbion.gui.pages import build_page                        # noqa: E402

pytestmark = pytest.mark.usefixtures("tmp_store")


@pytest.fixture
def ctx(repo, tmp_path):
    """A store `init` made lists no repo file until its toml declares one."""
    (tmp_path / "symbion.toml").write_text(f'[catalogs]\nfile = "{config.FILE_CATALOG}"\n')
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print(1)\n", newline="\n")    # as a drop reads it, LF
    c = api.resolve(str(tmp_path))
    build_page(c, author="ada")
    return c


def _drop(user: User, name: str = "", text: str = "", cursor=None, refused=None) -> None:
    args = {"refused": refused} if refused else {"name": name, "text": text, "cursor": cursor}
    UserInteraction(user, {user.client.layout}, None).trigger("sbDrop", args)


def _one(user: User, marker: str):
    (e,) = user.find(marker=marker).elements
    return e


def _composer_dialog(user: User):
    """The New entry dialog: a page has others (the keys list)."""
    (d,) = [d for d in user.find(ui.dialog).elements
            if any("sb-new-card" in c.classes for c in d.default_slot.children)]
    return d


async def test_a_known_file_opens_the_composer_on_it(user: User, ctx):
    await user.open("/")
    _drop(user, "app.py", "print(1)\n")
    assert _one(user, "note-on-type").value == "file"
    assert _one(user, "note-on-name").value == "src/app.py"
    assert _one(user, "note-body").value == ""


async def test_short_text_becomes_the_body(user: User, ctx):
    await user.open("/")
    _drop(user, "scratch.md", "the station times out")
    assert _one(user, "note-body").value == "the station times out"
    assert _one(user, "note-on-type").value == "project"


async def test_with_the_composer_open_a_known_file_joins_refs(user: User, ctx):
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "draft"
    _drop(user, "app.py", "print(1)\n")
    assert _one(user, "note-refs").value == ["file:src/app.py"]
    assert _one(user, "note-on-type").value == "project"
    assert _one(user, "note-body").value == "draft"


async def test_with_the_composer_open_text_goes_in_at_the_cursor(user: User, ctx):
    """`_DROP_JS` counts the cursor in code points, as Python does: in UTF-16
    units, a cursor after an emoji lands one character late."""
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "🙂 ab"
    _drop(user, "x.md", "XY", cursor=[3, 3])
    assert _one(user, "note-body").value == "🙂 aXYb"


async def test_a_draft_in_a_closed_composer_is_kept(user: User, ctx):
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "draft"
    _composer_dialog(user).close()
    _drop(user, "x.md", "more")
    assert _composer_dialog(user).value
    assert _one(user, "note-body").value == "draft\n\nmore"


async def test_a_known_file_on_a_closed_draft_joins_refs(user: User, ctx):
    """A draft counts as open: the dropped file must not take its target."""
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "draft"
    _composer_dialog(user).close()
    _drop(user, "app.py", "print(1)\n")
    assert _one(user, "note-refs").value == ["file:src/app.py"]
    assert _one(user, "note-on-type").value == "project"

async def test_several_matching_files_get_a_notice_and_count_as_text(user: User, repo, ctx):
    for d in ("a", "b"):
        (repo / d).mkdir()
        (repo / d / "same.md").write_text("s")
    await user.open("/")
    _drop(user, "same.md", "s")
    assert user.notify.contains("matches 2 files")
    assert _one(user, "note-body").value == "s"


async def test_a_refusal_from_the_page_is_a_notice(user: User, ctx):
    await user.open("/")
    _drop(user, refused="drop one file at a time")
    assert user.notify.contains("drop one file at a time")
    await user.should_not_see(marker="note-body")


LONG = "x" * 2001


async def test_long_text_is_offered_to_docs_and_saved_as_a_ref(user: User, ctx, tmp_path):
    await user.open("/")
    _drop(user, "notes.md", LONG)
    assert _one(user, "drop-doc-name").value == "notes.md"
    user.find(marker="drop-save").click()
    assert (tmp_path / "docs" / "notes.md").read_text() == LONG
    assert _one(user, "note-refs").value == ["doc:notes.md"]
    assert _one(user, "note-body").value == ""


async def test_long_text_can_go_in_as_the_body(user: User, ctx, tmp_path):
    await user.open("/")
    _drop(user, "long.md", LONG)
    user.find(marker="drop-body").click()
    assert _one(user, "note-body").value == LONG
    assert not (tmp_path / "docs" / "long.md").exists()


async def test_the_offer_names_a_taken_doc_anew(user: User, ctx, tmp_path):
    (tmp_path / "docs" / "notes.md").write_text("other")
    await user.open("/")
    _drop(user, "notes.md", LONG)
    assert _one(user, "drop-doc-name").value == "notes-2.md"


async def test_the_same_long_text_dropped_twice_is_one_doc(user: User, ctx, tmp_path):
    await user.open("/")
    _drop(user, "notes.md", LONG)
    user.find(marker="drop-save").click()
    _drop(user, "notes.md", LONG)
    assert _one(user, "note-refs").value == ["doc:notes.md"]
    assert sorted(p.name for p in (tmp_path / "docs").iterdir()) == ["notes.md"]


async def test_a_refused_save_says_why_and_keeps_the_offer(user: User, ctx, tmp_path):
    await user.open("/")
    _drop(user, "notes.md", LONG)
    _one(user, "drop-doc-name").value = "../out.md"
    user.find(marker="drop-save").click()
    assert user.notify.contains("cannot save a doc")
    assert _one(user, "drop-doc-name").value == "../out.md"


async def test_a_dropped_code_file_keeps_its_text(user: User, ctx, tmp_path):
    """The composer reads `#word` as a tag and `!kind` as the kind, and takes
    them out of the body: a dropped CSS file lost `#main` and `#fff`."""
    await user.open("/")
    _drop(user, "style.css", "#main { color: #fff; }")
    user.find(marker="note-add").click()
    (row,) = store.pending_rows(tmp_path)
    assert "#main { color: #fff; }" in row["body"]
    assert not row.get("tags")


async def test_code_dropped_at_the_cursor_is_a_block_on_lines_of_its_own(user: User, ctx):
    """A fence opens a code block only at the start of a line."""
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "ab"
    _drop(user, "x.py", "print(1)\n", cursor=[1, 1])
    assert _one(user, "note-body").value == "a\n```python\nprint(1)\n```\nb"


async def test_crlf_text_goes_in_as_lf(user: User, ctx):
    """A textarea holds LF alone: a CR kept in the body put the next drop's
    cursor, counted in the browser, a character off for each."""
    await user.open("/")
    user.find(marker="new-note").click()
    _drop(user, "x.md", "one\r\ntwo\rthree")
    assert _one(user, "note-body").value == "one\ntwo\nthree"


async def test_a_long_text_saved_beside_an_open_draft_keeps_the_draft(user: User, ctx, tmp_path):
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "draft"
    _drop(user, "notes.md", LONG)
    user.find(marker="drop-save").click()
    assert _one(user, "note-refs").value == ["doc:notes.md"]
    assert _one(user, "note-body").value == "draft"


async def test_a_long_text_used_as_body_goes_in_at_the_cursor(user: User, ctx):
    await user.open("/")
    user.find(marker="new-note").click()
    _one(user, "note-body").value = "ab"
    _drop(user, "long.md", LONG, cursor=[1, 1])
    user.find(marker="drop-body").click()
    assert _one(user, "note-body").value == "a" + LONG + "b"


async def test_a_name_the_disk_refuses_says_why_and_enter_saves(user: User, ctx, tmp_path):
    """save_doc let an OSError out, so a too-long name's save showed nothing;
    and Enter in the name field did nothing."""
    await user.open("/")
    _drop(user, "notes.md", LONG)
    _one(user, "drop-doc-name").value = "n" * 300 + ".md"
    user.find(marker="drop-save").click()
    assert user.notify.contains("cannot save docs/"), user.notify.messages
    _one(user, "drop-doc-name").value = "short.md"
    user.find(marker="drop-doc-name").trigger("keydown.enter")
    assert (tmp_path / "docs" / "short.md").read_text() == LONG
