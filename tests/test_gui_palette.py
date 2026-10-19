"""The ⌘K palette's rows (`palette.rows`): what `/palette` answers as the
person types. The box itself is chrome's `_PALETTE_JS`, which the `user`
fixture never runs: check it in a browser."""
from __future__ import annotations

import json
import os
import time

import pytest

pytest.importorskip("nicegui")

from symbion import api, store  # noqa: E402
from symbion.gui import palette, servers  # noqa: E402

pytestmark = pytest.mark.usefixtures("tmp_store")


def _add(ctx, kind, body, target=("project", None), **more):
    row = {"kind": kind, "target": {"type": target[0], "name": target[1]}, "body": body, **more}
    return api.add(ctx, row, author="ada").id


def _groups(ctx, q):
    return {g["head"]: g for g in palette.rows(ctx, "ada", q)}


def _serving(store_dir, url, pid=None):
    """A serve's record. Its pid must be alive: the parent's is, and ours."""
    pid = pid or os.getppid()
    d = servers._dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "url": url, "store": str(store_dir.resolve()), "started": time.time()}))


def test_at_rest_the_actions_are_the_ones_the_top_bar_would_show(repo, tmp_path):
    """No remote, nothing pending: no push and no commit. A pending entry
    of the writer's brings commit, counted, and undo."""
    ctx = api.resolve(str(tmp_path))
    g = _groups(ctx, "")
    assert [r["text"] for r in g["go to"]["rows"]] == \
        ["Summary", "All entries", "Targets", "Tags", "Arcs", "Docs"]
    assert [(r["text"], r["click"]) for r in g["do"]["rows"]] == \
        [("New entry", ".sb-new"), ("Keyboard shortcuts", ".sb-keys"),
         ("How symbion works", ".sb-help")]
    assert "other stores" not in g
    _add(ctx, "note", "alpha")
    acts = {r["text"]: r for r in _groups(ctx, "")["do"]["rows"]}
    assert acts["Commit your entries"]["meta"] == "1 entry"
    assert acts["Undo your last write"]["click"] == ".sb-undo"
    assert "Push the store" not in acts


def test_a_word_finds_places_then_entries_open_first_then_the_whole_search(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    arc = api.create_arc(ctx, "Open-source release", "", "item", author="ada")
    bug = _add(ctx, "bug", "cut the release", target=("item", "release recipe"))
    note = _add(ctx, "note", "how a release is cut", tags=["release"])
    done = _add(ctx, "bug", "the release broke")
    api.supersede(ctx, done, author="ada", status="resolved")
    _add(ctx, "note", "nothing to see")
    g = _groups(ctx, "release")
    goto = g["go to"]["rows"]
    assert [r["href"] for r in goto] == \
        [f"/arc?id={arc.id}", "/object?type=item&name=release%20recipe", "/notes?tag=release"]
    assert goto[0]["html"] == "Open-source <mark>release</mark>"
    rows = g["entries here"]["rows"]
    # open, then no status, then closed; each one peeks, here
    assert [r["peek"] for r in rows[:-1]] == [bug, note, done]
    assert rows[2]["done"] and not rows[0]["done"]
    assert (rows[-1]["text"], rows[-1]["href"]) == \
        ("All 3 entries here that say “release”", "/notes?q=release")
    assert g["entries here"]["aside"] == "open first"


def test_an_entrys_words_are_escaped_and_only_the_words_marked(repo, tmp_path):
    """The box sets this html as is: a body is anyone's text."""
    ctx = api.resolve(str(tmp_path))
    _add(ctx, "note", "<img src=x onerror=alert(1)> release")
    (row, _all) = _groups(ctx, "release")["entries here"]["rows"]
    assert row["html"] == "&lt;img src=x onerror=alert(1)&gt; <mark>release</mark>", row


def test_an_ids_tail_finds_its_entry_here_to_peek_and_another_stores_to_open(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    here = _add(ctx, "task", "build it")
    far = tmp_path / "far-notes"
    store.ensure_store(far)
    there = _add(api.resolve(str(far)), "task", "elsewhere")
    _serving(far, "http://127.0.0.1:20001/")
    (row,) = _groups(ctx, here[-3:])["entries by id"]["rows"]
    assert (row["peek"], row["href"]) == (here, f"/notes?id={here}")
    assert row["id"] == f"{here[-10:-3]}<mark>{here[-3:]}</mark>"
    g = _groups(ctx, there[-3:])
    (row,) = g["entries by id"]["rows"]
    assert "peek" not in row
    assert (row["store"], row["href"]) == ("far", f"http://127.0.0.1:20001/notes?id={there}")
    assert "other stores" not in g, "none of them say it, under an id found in one"


def test_other_running_stores_are_counted_and_link_to_their_own_search(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    _add(ctx, "note", "release close by")
    far = tmp_path / "far-notes"
    store.ensure_store(far)
    _add(api.resolve(str(far)), "note", "release far away")
    _serving(far, "http://127.0.0.1:20001/")
    _serving(tmp_path, "http://127.0.0.1:20002", pid=os.getpid())     # this store's own
    g = _groups(ctx, "release")
    (row,) = g["other stores"]["rows"]
    assert (row["text"], row["meta"], row["href"]) == \
        ("far", "1 entry", "http://127.0.0.1:20001/notes?q=release")
    assert g["other stores"]["aside"] == "1 of 1 say it"
    assert _groups(ctx, "close")["other stores"]["aside"] == "none of 1 say it"
    # nothing anywhere: no groups, and the box says so
    assert _groups(ctx, "nowhere") == {}
    assert [(r["text"], r["meta"]) for r in _groups(ctx, "")["other stores"]["rows"]] == \
        [("far", "1 entry")]
