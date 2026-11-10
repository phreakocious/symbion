"""store.discard / api.discard: throwing away what a commit would take for you."""
import json
import subprocess

from symbion import api, store


def _note(ctx, body, who, **kw):
    return api.add(ctx, {"kind": "task", "target": {"type": "item", "name": body},
                         "body": body, **kw}, author=who)


def _lines(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _head_line(store_dir, entry_id):
    head = subprocess.run(["git", "-C", str(store_dir), "show", "HEAD:notes.jsonl"],
                          capture_output=True, text=True, check=True).stdout
    (line,) = [ln for ln in head.splitlines() if ln and json.loads(ln)["id"] == entry_id]
    return line


def test_discard_takes_your_uncommitted_rows_and_arcs_and_undo_brings_them_back(
        repo, tmp_path, monkeypatch):
    """The user made an arc and tasks to test things, and only a git command
    could throw them away (2026-11-08). Discard takes what a commit would take
    for you: a new entry goes, an edit of a committed entry goes back to its
    committed line, and an arc you made since the last commit goes. Another
    writer's row stays. A dry run writes nothing, and undo takes it back."""
    store.ensure_store(tmp_path)
    ctx = api.resolve(str(tmp_path))
    old = _note(ctx, "committed", "ada")
    api.commit(ctx, "c", everyone=True)
    act = api.create_arc(ctx, "test", "d", "item", author="ada")
    _note(ctx, "t1", "ada", arc_id=act.id)
    api.supersede(ctx, old.id, author="ada", status="resolved")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    _note(ctx, "theirs", "claude")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    notes, arcs = store.notes_path(tmp_path), store.arcs_path(tmp_path)
    was = (_lines(notes), _lines(arcs))

    plan = api.discard(ctx, author="ada", apply=False)
    assert (_lines(notes), _lines(arcs)) == was, "a dry run writes nothing"
    done = api.discard(ctx, author="ada")
    assert plan == done
    assert [r["body"] for r in store.pending_rows(tmp_path)] == ["theirs"]
    assert _head_line(tmp_path, old.id) in _lines(notes), "the committed line, as committed"
    assert [a.id for a in store.load_arcs(tmp_path)] == []
    assert [r["id"] for r in done["restored"]] == [old.id]
    assert sorted(r["body"] for r in store.entries(done["removed"])) == ["committed", "t1"]
    assert [a["id"] for a in done["arcs"]] == [act.id]

    api.undo(ctx, None, author="ada")
    assert (_lines(notes), _lines(arcs)) == was


def test_discard_keeps_what_another_writer_builds_on(repo, tmp_path, monkeypatch):
    """A row of yours that another writer's pending revision builds on stays,
    and so does an arc that holds a row that stays. A change to a committed
    arc records no writer, so it stays too. Each is named."""
    store.ensure_store(tmp_path)
    ctx = api.resolve(str(tmp_path))
    done_arc = api.create_arc(ctx, "committed arc", "d", "item", author="ada")
    api.commit(ctx, "c", everyone=True)
    api.set_phases(ctx, done_arc.id, ["todo", "doing"], author="user")
    held = api.create_arc(ctx, "held", "d", "item", author="ada")
    mine = _note(ctx, "mine", "ada", arc_id=held.id)
    other = api.create_arc(ctx, "other's row", "d", "item", author="ada")
    gone = _note(ctx, "gone", "ada")
    # An arc a staying row targets or refs stays too (audit, 2026-11-08).
    aimed = api.create_arc(ctx, "targeted", "d", "item", author="ada")
    cited = api.create_arc(ctx, "reffed", "d", "item", author="ada")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    api.supersede(ctx, mine.id, author="claude", body="claude's edit")
    _note(ctx, "in ada's arc", "claude", arc_id=other.id)
    api.add(ctx, {"kind": "note", "target": {"type": "arc", "name": aimed.id},
                  "body": "about the arc"}, author="claude")
    _note(ctx, "refs ada's arc", "claude", refs=[{"type": "arc", "name": cited.id}])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    done = api.discard(ctx, author="ada")
    assert [r["body"] for r in store.entries(done["removed"])] == ["gone"]
    assert [r["body"] for r in done["kept"]] == ["mine"]
    assert sorted(done["kept_arcs"]) == sorted([held.id, other.id, aimed.id, cited.id])
    assert done["arcs_changed"] == [done_arc.id]
    left = {a.id: a for a in store.load_arcs(tmp_path)}
    assert set(left) == {done_arc.id, held.id, other.id, aimed.id, cited.id}
    assert left[done_arc.id].phases == ("todo", "doing")
    assert gone.id not in {r["id"] for r in store.pending_rows(tmp_path)}


def test_discard_refuses_a_plan_that_moved(repo, tmp_path):
    """The dialog lists one plan and its button applies another: a write in
    between would throw away what nobody saw. With the plan's ids it refuses,
    and writes nothing."""
    import pytest
    store.ensure_store(tmp_path)
    ctx = api.resolve(str(tmp_path))
    _note(ctx, "shown", "ada")
    plan = api.discard(ctx, author="ada", apply=False)
    _note(ctx, "written after the list", "ada")
    was = _lines(store.notes_path(tmp_path))
    with pytest.raises(store.DiscardChanged):
        api.discard(ctx, author="ada", expect=store.discard_ids(plan))
    assert _lines(store.notes_path(tmp_path)) == was


def test_discard_gives_another_writers_version_the_entry_id_back(repo, tmp_path, monkeypatch):
    """An agent amends a committed entry, then the user edits it: a discard
    put the committed line back and left the agent's version as a second
    head, its `supersedes` naming a line gone (audit, 2026-11-08). The
    agent's version takes the entry's id back, and its session with it."""
    store.ensure_store(tmp_path)
    ctx = api.resolve(str(tmp_path))
    x = _note(ctx, "as committed", "ada")
    api.commit(ctx, "c", everyone=True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    api.supersede(ctx, x.id, author="claude", append_body="claude's amendment")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    agent = _lines(store.notes_path(tmp_path))
    api.supersede(ctx, x.id, author="ada", status="resolved")
    api.supersede(ctx, x.id, author="ada", append_body="and a line")    # two edits on it

    # A plan whose returned version changed is refused, as one whose removed
    # rows changed is: here a merge rewrote the agent's line (audit, 2026-11-08).
    plan = api.discard(ctx, author="ada", apply=False)
    path = store.notes_path(tmp_path)
    was = path.read_text(encoding="utf-8")
    path.write_text(was.replace("claude's amendment", "claude's merged amendment"),
                    encoding="utf-8")
    import pytest
    with pytest.raises(store.DiscardChanged):
        api.discard(ctx, author="ada", expect=store.discard_ids(plan))
    path.write_text(was, encoding="utf-8")
    assert api.discard(ctx, author="ada", apply=False) == plan

    done = api.discard(ctx, author="ada")
    assert _lines(store.notes_path(tmp_path)) == agent, "as the agent left it"
    assert [r["id"] for r in done["returned"]] == [x.id]
    assert (done["restored"], done["kept"]) == ([], [])
    assert store.sessions(tmp_path).get(x.id) == "s1"
