"""The ⌘K palette's rows (`palette.rows`): what `/palette` answers as the
person types. The box itself is chrome's `_PALETTE_JS`, which the `user`
fixture never runs: check it in a browser."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

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


_STORES: dict = {}                  # a recorded serve's url -> its store
_REAL_ASK = palette._ask            # before `_asks_in_process` stands in for it


def _serving(store_dir, url, pid=None):
    """A serve's record. Its pid must be alive: the parent's is, and ours.
    Nothing listens at `url`: `_asks_in_process` answers for it."""
    pid = pid or os.getppid()
    d = servers._dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "url": url, "store": str(store_dir.resolve()), "started": time.time()}))
    _STORES[url.rstrip("/")] = store_dir


@pytest.fixture(autouse=True)
def _asks_in_process(monkeypatch):
    """Another serve's answer, made here as that serve makes it (`answer`):
    tests/test_gui_serve.py asks two real serves."""
    _STORES.clear()
    monkeypatch.setattr(palette, "_ask", lambda url, q: palette.answer(
        api.resolve(str(_STORES[url])), q) if url in _STORES else None)


def test_at_rest_the_actions_are_the_ones_the_top_bar_would_show(repo, tmp_path):
    """No remote, nothing pending: no push and no commit. A pending entry
    of the writer's brings commit, counted, and undo."""
    ctx = api.resolve(str(tmp_path))
    g = _groups(ctx, "")
    assert [r["text"] for r in g["go to"]["rows"]] == \
        ["Summary", "All entries", "Targets", "Tags", "Arcs", "Docs", "Map"]
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


def test_a_type_and_a_colon_lists_that_types_targets_by_the_words_after_it(repo, tmp_path):
    """Every item's `item:NAME` holds `item:`: the two most named were all
    it showed."""
    ctx = api.resolve(str(tmp_path))
    for name in ("thing one", "thing two", "thing three", "other"):
        _add(ctx, "note", "x", target=("item", name))
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "thing.md").write_text("x\n")
    _add(ctx, "note", "x", target=("doc", "thing.md"))

    def names(q):
        return [r["href"].split("name=")[1] for r in _groups(ctx, q)["go to"]["rows"]]
    # the most named first; a tie, the newest first
    assert names("item:") == ["other", "thing%20three", "thing%20two", "thing%20one"]
    assert names("Item:THING o") == ["thing%20two", "thing%20one"]
    assert names("doc:thing") == ["thing.md"]
    # with no type, two of every type as before
    assert names("thing") == ["thing.md", "thing%20three"]
    assert "go to" not in _groups(ctx, "nope:")


def test_arc_and_a_colon_lists_the_arcs_active_first(repo, tmp_path):
    """No entry has an arc as its target, so `arc:` named no type the
    palette counted, and listed nothing."""
    ctx = api.resolve(str(tmp_path))
    old, one, two = (api.create_arc(ctx, name, "", "item", author="ada").id
                     for name in ("ship zero", "ship one", "other two"))
    api.archive_arc(ctx, old, author="user")

    def ids(q):
        return [r["href"].split("id=")[1] for r in _groups(ctx, q)["go to"]["rows"]]
    assert ids("arc:") == [one, two, old]
    assert ids("Arc:ship") == [one, old]


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
    # The tail may also occur in the id's digits (414769-414): each is marked.
    assert row["id"] == here[-10:].replace(here[-3:], f"<mark>{here[-3:]}</mark>")
    g = _groups(ctx, there[-3:])
    (row,) = g["entries by id"]["rows"]
    assert "peek" not in row
    assert (row["store"], row["href"]) == ("far", f"http://127.0.0.1:20001/notes?id={there}")
    assert "other stores" not in g, "none of them say it, under an id found in one"


def test_another_serve_is_asked_past_any_proxy_and_its_rows_read_as_text(repo, tmp_path,
                                                                         monkeypatch):
    """An HTTP proxy the environment named took what the person typed, and its
    answer reached the page as sent: a row's words as HTML, its link, and a
    button its `click` pressed on Enter (audit, 2026-11-08). The ask goes
    direct, and a row another serve sends is rebuilt from named fields."""
    import http.server
    import threading
    seen = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.requestline)
            body = json.dumps({"entries": 1}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{srv.server_port}")
    for k in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    try:
        assert _REAL_ASK("http://127.0.0.1:9", "what i typed") is None   # nothing listens at 9
    finally:
        srv.shutdown()
    assert seen == []

    ctx = api.resolve(str(tmp_path))
    _serving(tmp_path / "far-notes", "http://127.0.0.1:20001/")
    sent = {"kind": 'x" onclick="y', "kind_name": "<b>note</b>", "id": "<b>1234567abc</b>",
            "html": "<img src=x onerror=alert(1)>a &lt;word&gt;", "href": "/notes?id=abc",
            "click": ".sb-discard", "peek": "abc", "done": "yes"}
    monkeypatch.setattr(palette, "_ask", lambda url, q: {
        "entries": 1, "say": "2", "by_id": [sent, {**sent, "href": "@evil.example/"}, "row"]})
    g = _groups(ctx, "abc")
    assert g["entries by id"]["rows"] == [{
        "kind": "", "kind_name": "note", "html": "a &lt;word&gt;", "id": "1234567<mark>abc</mark>",
        "done": False, "href": "http://127.0.0.1:20001/notes?id=abc", "store": "far"}]
    assert "other stores" not in g, "a count that is no number is none"


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
    g = _groups(ctx, "far")                 # only elsewhere: this store's 0 is printed
    assert g["entries here"] == {"head": "entries here", "aside": "none say “far”", "rows": []}
    assert list(g) == ["entries here", "other stores"]
    assert _groups(ctx, "close")["other stores"]["aside"] == "none of 1 say it"
    # nothing anywhere: no groups, and the box says so
    assert _groups(ctx, "nowhere") == {}
    assert [(r["text"], r["meta"]) for r in _groups(ctx, "")["other stores"]["rows"]] == \
        [("far", "1 entry")]


def test_the_palette_reads_no_store_but_its_own(repo, tmp_path, monkeypatch):
    """Every serve whose palette opened parsed every running store and kept
    it for its life, so one palette grew its serve by every store's size (the
    GUI review, 2026-11-07). Each other store's serve answers for it."""
    ctx = api.resolve(str(tmp_path))
    _add(ctx, "note", "release close by")
    far = tmp_path / "far-notes"
    store.ensure_store(far)
    _add(api.resolve(str(far)), "note", "release far away")
    _serving(far, "http://127.0.0.1:20001/")
    asked, loaded, load = [], [], store.load
    monkeypatch.setattr(palette, "_ask", lambda url, q: asked.append((url, q)) or
                        {"entries": 7, "say": 2, "by_id": []})
    monkeypatch.setattr(store, "load", lambda s: loaded.append(Path(s).resolve()) or load(s))
    palette._HEADS.clear()
    g = _groups(ctx, "release")
    assert set(loaded) == {Path(ctx.store_dir).resolve()}, loaded
    assert asked == [("http://127.0.0.1:20001", "release")]
    assert [(r["text"], r["meta"]) for r in g["other stores"]["rows"]] == [("far", "2 entries")]
    assert [r["meta"] for r in _groups(ctx, "")["other stores"]["rows"]] == ["7 entries"]


def test_a_serve_that_gives_no_answer_is_said_not_counted_as_none(repo, tmp_path,
                                                                  monkeypatch):
    """A serve stopped, busy, or older than `/palette/here` answers nothing
    in palette.ASK_SECONDS: the box says so, and counts it with no store."""
    assert _REAL_ASK("http://127.0.0.1:9", "x") is None, "a real ask of a port no one holds"
    ctx = api.resolve(str(tmp_path))
    _add(ctx, "note", "release close by")
    far = tmp_path / "far-notes"
    store.ensure_store(far)
    _serving(far, "http://127.0.0.1:20001/")
    monkeypatch.setattr(palette, "_ask", lambda url, q: None)
    assert [(r["text"], r["meta"]) for r in _groups(ctx, "")["other stores"]["rows"]] == \
        [("far", "no answer")]
    assert _groups(ctx, "release")["other stores"] == \
        {"head": "other stores", "aside": "1 no answer", "rows": []}


def test_a_read_only_serve_lists_no_other_store(repo, tmp_path, monkeypatch):
    """Read-only, three hex digits read the rows of a store another serve
    ran by id tail (audit 2026-10-28): a read-only serve names no other store."""
    ctx = api.resolve(str(tmp_path))
    far = tmp_path / "far-notes"
    store.ensure_store(far)
    tail = _add(api.resolve(str(far)), "note", "release far away")[-3:]
    _serving(far, "http://127.0.0.1:20001/")
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    for q in ("", "release", tail):
        assert "other stores" not in _groups(ctx, q) and "entries by id" not in _groups(ctx, q), q
