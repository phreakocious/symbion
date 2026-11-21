"""The writer stamp: each version names who ran its write, an agent, `gui` or
`cli`, tied to its own created_at (docs/superpowers/specs/2026-11-20-writer-stamp-design.md)."""
import json

import pytest

from symbion import cli, store
from symbion import summary as summ
from symbion.config import Config

pytestmark = pytest.mark.usefixtures("tmp_store")

ITEM = {"type": "item", "name": "x"}


def run(d, *args):
    return cli.main(["--dir", str(d), *args])


def _add(d, capsys, *args):
    capsys.readouterr()
    assert run(d, "add", *args) == 0
    return capsys.readouterr().out.strip()


def _json(d, capsys, *args):
    capsys.readouterr()
    assert run(d, "list", "--json", "--limit", "0", *args) == 0
    return json.loads(capsys.readouterr().out)


def _writers(d, capsys, nid):
    """Each version's writer verdict, oldest first."""
    rows = [r for r in _json(d, capsys, "--all") if r["id"] == nid
            or r["id"] in _ids_before(d, nid)]
    return [r["writer"] for r in sorted(rows, key=lambda r: r["created_at"])]


def _ids_before(d, nid):
    by_id = {n.id: n for n in store.load(d)}
    out, n = set(), by_id[nid]
    while n.supersedes:
        out.add(n.supersedes)
        n = by_id[n.supersedes]
    return out


def _clock(monkeypatch, sec):
    monkeypatch.setattr(store, "_now_iso", lambda: f"2099-01-01T00:00:{sec:02d}+00:00")


def test_each_mint_path_stamps_the_writer(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    nid = _add(tmp_path, capsys, "task", "--type", "item", "--name", "x", "--body", "b")
    assert store.load(tmp_path)[0].writer == {"name": "claude",
                                              "at": store.load(tmp_path)[0].created_at}
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setenv("CODEX_THREAD_ID", "t")
    _clock(monkeypatch, 1)
    assert run(tmp_path, "supersede", nid, "--append", "--body", "uncommitted edit") == 0
    assert run(tmp_path, "commit", "--all") == 0
    monkeypatch.delenv("CODEX_THREAD_ID")
    _clock(monkeypatch, 2)
    assert run(tmp_path, "supersede", nid, "--append", "--body", "committed edit") == 0
    monkeypatch.setenv("HERMES_AGENT", "1")
    _clock(monkeypatch, 3)
    assert run(tmp_path, "supersede", nid, "--add-tag", "t") == 0     # the fast path to retag
    monkeypatch.delenv("HERMES_AGENT")
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "c")
    _clock(monkeypatch, 4)
    assert run(tmp_path, "resolve", nid, "--body", "done") == 0
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID")
    _clock(monkeypatch, 5)
    assert run(tmp_path, "rename", "x", "y", "--type", "item") == 0
    assert _writers(tmp_path, capsys, nid) == ["claude", "codex", "cli", "hermes",
                                               "antigravity", "cli"]
    monkeypatch.setenv("AI_AGENT", "pi")
    capsys.readouterr()
    assert run(tmp_path, "arc", "create", "--name", "a") == 0
    arc = capsys.readouterr().out.strip()
    assert run(tmp_path, "arc", "seed", arc, "--scope", "item", "--name", "s") == 0
    box = next(n for n in store.load(tmp_path) if n.arc_id == arc)
    assert store.writer_of(box) == "pi"


def test_a_retried_add_is_still_a_twin_seconds_later(tmp_path, capsys, monkeypatch):
    argv = ["note", "--type", "item", "--name", "x", "--body", "same"]
    _clock(monkeypatch, 1)
    first = _add(tmp_path, capsys, *argv)
    _clock(monkeypatch, 9)
    assert _add(tmp_path, capsys, *argv) == first
    assert len(store.load(tmp_path)) == 1


def test_a_gui_write_reads_gui_whatever_the_serves_shell_holds(tmp_path, monkeypatch):
    serve = pytest.importorskip("symbion.gui.serve")
    monkeypatch.setattr(store, "_GUI", False)        # put back after: the flag is the process's
    env = {"CLAUDECODE": "1", "AI_AGENT": "claude-code", "CLAUDE_CODE_SESSION_ID": "s"}
    serve.as_gui(env)
    assert "CLAUDE_CODE_SESSION_ID" not in env and "SYMBION" not in " ".join(env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    n = store.add(tmp_path, kind="note", target=ITEM, author="ada", body="b")
    assert store.writer_of(n) == "gui"


def test_serve_marks_its_process_before_it_can_refuse_to_start(monkeypatch, capsys):
    serve = pytest.importorskip("symbion.gui.serve")
    monkeypatch.setattr(store, "_GUI", False)
    assert serve.main(None, author="ada", host="192.0.2.1") == 1     # no --allow: refused
    assert store._GUI


def test_symbion_author_changes_the_author_not_the_writer(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SYMBION_AUTHOR", "ada")
    _add(tmp_path, capsys, "note", "--type", "item", "--name", "x", "--body", "b")
    n = store.load(tmp_path)[0]
    assert (n.author, store.writer_of(n), n.via) == ("ada", "claude", "claude")


def test_a_stamp_copied_from_an_earlier_version_reads_unknown(tmp_path, capsys):
    """An older install's edit keeps the key it does not know, so its version
    carries the stamp it found, at the earlier version's time."""
    row = {"id": "20990101-000000-000000-abc", "kind": "note", "target": ITEM,
           "created_at": "2099-01-01T00:00:05+00:00", "author": "ada", "body": "b",
           "writer": {"name": "gui", "at": "2099-01-01T00:00:01+00:00"}}
    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    n = store.load(tmp_path)[0]
    assert store.writer_of(n) is None
    assert _json(tmp_path, capsys)[0]["writer"] is None


def _show(d, capsys, nid):
    capsys.readouterr()
    assert run(d, "show", nid) == 0
    return capsys.readouterr().out


def test_via_prints_only_where_an_agent_wrote_under_another_name(tmp_path, capsys, monkeypatch):
    own = _add(tmp_path, capsys, "note", "--type", "item", "--name", "x", "--body", "a person")
    assert "written" in _show(tmp_path, capsys, own) and " via " not in _show(tmp_path, capsys, own)
    monkeypatch.setenv("CLAUDECODE", "1")
    itself = _add(tmp_path, capsys, "note", "--type", "item", "--name", "x", "--body", "itself")
    assert " via " not in _show(tmp_path, capsys, itself)
    relayed = _add(tmp_path, capsys, "note", "--type", "item", "--name", "x",
                   "--body", "relayed", "--author", "ada")
    assert " by ada via claude" in _show(tmp_path, capsys, relayed)
    _clock(monkeypatch, 1)
    assert run(tmp_path, "supersede", relayed, "--append", "--author", "ada",
               "--body", "more") == 0
    assert "-- ada via claude added below" in _show(tmp_path, capsys, relayed)


def _labels(d, reader, nid):
    data = summ.summary(d, Config(project_root=d), reader=reader, full=True)
    rows = [r for k in ("waiting", "people", "heads", "priority", "closed")
            for r in data.get(k, ()) if r["id"] == nid]
    return rows[0] if rows else None


def test_two_same_author_chains_print_nothing_new(tmp_path, capsys, monkeypatch):
    """A row written before this release, then edited after it, and a GUI row
    amended at a terminal: neither version prints a `via`, so neither prints a
    new label (raw, unknown != cli and gui != cli)."""
    row = {"id": "20990101-000000-000000-abc", "kind": "task", "target": ITEM,
           "created_at": "2099-01-01T00:00:00+00:00", "author": "ada", "body": "old",
           "status": "open"}
    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    _clock(monkeypatch, 1)
    store.supersede(tmp_path, row["id"], author="ada", append_body="new")
    monkeypatch.setattr(store, "_GUI", True)
    gui = store.add(tmp_path, kind="task", target=ITEM, author="ada", body="at the gui")
    monkeypatch.setattr(store, "_GUI", False)
    _clock(monkeypatch, 2)
    store.supersede(tmp_path, gui.id, author="ada", append_body="at a terminal")
    for nid in (row["id"], gui.id):
        out = _show(tmp_path, capsys, nid)
        assert out.count("written") == 1 and "body changed" not in out, out
        for reader in ("ada", "bob", None):
            r = _labels(tmp_path, reader, nid) or {}
            assert "last" not in r and not any(k.endswith("_via") for k in r), (reader, r)


def test_an_answer_relayed_under_the_users_name_labels_and_waits_on_no_one(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(store, "_GUI", True)
    q = store.add(tmp_path, kind="question", target=ITEM, author="ada", body="which?")
    monkeypatch.setattr(store, "_GUI", False)
    monkeypatch.setenv("CLAUDECODE", "1")
    _clock(monkeypatch, 1)
    store.supersede(tmp_path, q.id, author="ada", append_body="the second")
    r = _labels(tmp_path, "ada", q.id)
    assert (r["last"], r["last_via"]) == ("ada", "claude")
    assert "from" not in r and not r.get("waits")
    assert "last by ada via claude" in summ._labelled(summ.plain, r)


def test_a_row_raised_under_the_readers_name_says_so_to_that_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    n = store.add(tmp_path, kind="task", target=ITEM, author="ada", body="b")
    for reader in ("ada", None):            # the GUI's user, and a person at a terminal
        r = _labels(tmp_path, reader, n.id)
        assert summ._labelled(summ.plain, r).startswith("from ada via claude"), (reader, r)
    other = store.add(tmp_path, kind="task", target=ITEM, author="claude", body="c")
    assert "from" not in (_labels(tmp_path, None, other.id) or {})


def test_closed_by_names_the_agent_and_keeps_a_last_by_that_differs_by_writer(tmp_path,
                                                                            monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    q = store.add(tmp_path, kind="question", target=ITEM, author="claude", body="which?")
    _clock(monkeypatch, 1)
    store.supersede(tmp_path, q.id, author="ada", status="resolved", append_body="yes")
    d = summ.summary(tmp_path, Config(project_root=tmp_path), reader="claude")
    c = next(r for r in d["closed"] if r["id"] == q.id)
    assert (c["closed_by"], c["closed_by_via"]) == ("ada", "claude")
    text = summ.render_summary(d)
    assert "closed by ada via claude" in text and "last by ada via claude" not in text
    # The same name, by writer alone: both print.
    d["closed"] = [{**c, "last": "ada"}]
    d["closed"][0].pop("last_via", None)
    text = summ.render_summary(d)
    assert "closed by ada via claude" in text and "last by ada" in text


def test_context_text_prints_via(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    _add(tmp_path, capsys, "note", "--type", "item", "--name", "x", "--body", "b",
         "--author", "ada")
    capsys.readouterr()
    assert run(tmp_path, "context", "--target", "item:x") == 0
    assert "by ada via claude" in capsys.readouterr().out


def _note(**d):
    return store.note_from_dict({"id": "20990101-000000-000000-abc", "kind": "note",
                                 "target": ITEM, "created_at": "2099-01-01T00:00:00+00:00",
                                 "body": "b", **d})


def test_a_shared_stores_side_joins_the_author_and_via_reads_the_stored_one():
    at = "2099-01-01T00:00:00+00:00"
    itself = _note(author="claude", side="B", writer={"name": "claude", "at": at})
    relayed = _note(author="ada", side="B", writer={"name": "claude", "at": at})
    a, b = summ.seen_from([itself, relayed], "A")
    assert summ.byline(a) == "claude (B)"
    assert summ.byline(b) == "ada (B) via claude"


def test_from_json_names_writer_among_what_symbion_mints(tmp_path, capsys):
    rows = tmp_path / "rows.jsonl"
    rows.write_text(json.dumps({"kind": "note", "target": ITEM, "body": "b",
                                "writer": {"name": "gui", "at": "x"}}) + "\n")
    capsys.readouterr()
    assert run(tmp_path, "add", "--from-json", str(rows)) == 1
    assert "target_blob and writer itself" in capsys.readouterr().err
    assert store.load(tmp_path) == []


def test_list_json_gives_the_verdict_and_summary_json_the_via_keys(tmp_path, capsys,
                                                                    monkeypatch):
    plain = _add(tmp_path, capsys, "note", "--type", "item", "--name", "x", "--body", "p")
    monkeypatch.setenv("CLAUDECODE", "1")
    t = _add(tmp_path, capsys, "task", "--type", "item", "--name", "x", "--body", "t",
             "--author", "ada")
    got = {r["id"]: r["writer"] for r in _json(tmp_path, capsys)}
    assert got == {plain: "cli", t: "claude"}
    capsys.readouterr()
    monkeypatch.setenv("SYMBION_AUTHOR", "bob")
    assert run(tmp_path, "summary", "--json") == 0
    rows = [r for v in json.loads(capsys.readouterr().out).values() if isinstance(v, list)
            for r in v if isinstance(r, dict) and r.get("id") == t]
    assert rows and all(r["from"] == "ada" and r["from_via"] == "claude" for r in rows)


def test_pull_news_drops_a_last_by_only_where_it_reads_as_the_closer(tmp_path, monkeypatch):
    import io
    monkeypatch.setenv("CLAUDECODE", "1")
    q = store.add(tmp_path, kind="question", target=ITEM, author="claude", body="which?")
    _clock(monkeypatch, 1)
    store.supersede(tmp_path, q.id, author="ada", status="resolved", append_body="yes")

    def news():
        by_id = {n.id: n for n in store.load(tmp_path)}
        out = io.StringIO()
        cli._print_news("news:", [by_id[q.id]], by_id, "claude", "", out)
        return out.getvalue()
    assert "closed by ada via claude" in news() and "last by" not in news()
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setattr(store, "_GUI", True)
    _clock(monkeypatch, 2)
    store.supersede(tmp_path, q.id, author="ada", tags=["t"])
    assert "closed by ada via claude, last by ada [" in news()


def test_an_authors_own_words_and_their_relay_are_each_an_added_line(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    q = store.add(tmp_path, kind="question", target=ITEM, author="claude", body="which?")
    monkeypatch.setattr(store, "_GUI", True)
    _clock(monkeypatch, 1)
    store.supersede(tmp_path, q.id, author="ada", append_body="yes.")
    monkeypatch.setattr(store, "_GUI", False)
    _clock(monkeypatch, 2)
    store.supersede(tmp_path, q.id, author="ada", append_body="ship it")
    by_id = {n.id: n for n in store.load(tmp_path)}
    a = summ.added(by_id, by_id[q.id], "claude", 8)       # a lead that shows neither line
    assert [(x["added"], summ._named(x, "added_by")) for x in summ.added_lines(a)] == [
        ("yes.", "ada"), ("ship it", "ada via claude")]
    assert summ.added_label(a).startswith("ada via claude added")
