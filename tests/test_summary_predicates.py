import json

import pytest
from symbion import store
from symbion import summary as summ

pytestmark = pytest.mark.usefixtures("tmp_store")


def _n(store_dir, **kw):
    base = {"kind": "note", "target": {"type": "project", "name": None}}
    return store.add(store_dir, **{**base, **kw})


def _write_raw_legacy_row(store_dir, **overrides):
    """A pre-invariant row written straight into notes.jsonl, bypassing
    add()'s own normalization -- add() would silently turn status=None into
    "open" and make a null-status test vacuous. Mirrors
    test_store.py's helper of the same name (not imported: test files
    shouldn't reach into each other's internals)."""
    store.ensure_store(store_dir)
    row = {
        "id": "legacy-1", "kind": "bug",
        "target": {"type": "project", "name": None},
        "created_at": "2020-01-01T00:00:00", "author": "claude",
        "body": "", "status": None,
    }
    row.update(overrides)
    with open(store.notes_path(store_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_query_filters_by_author(tmp_path):
    _n(tmp_path, author="ada")
    _n(tmp_path, author="sam")
    rows = store.query(store.load(tmp_path), author="ada")
    assert [r.author for r in rows] == ["ada"]


def test_open_outside_arcs_excludes_arc_members_for_any_kind(tmp_path):
    _n(tmp_path, kind="task")
    _n(tmp_path, kind="task", arc_id="camp-1")
    _n(tmp_path, kind="bug", arc_id="camp-1")
    heads = store.heads(store.load(tmp_path))
    assert len(summ.open_outside_arcs(heads, "task", {"camp-1"})) == 1
    assert summ.open_outside_arcs(heads, "bug", {"camp-1"}) == [], "a bug in an arc is the arc's"


def test_starred_includes_a_decision(tmp_path):
    """read_status is None for note/decision/check, so a `status == "open"`
    test would make a starred decision unstarrable -- and SKILL.md presents
    --add-tag priority as how ANY note gets starred."""
    _n(tmp_path, kind="decision", tags=["priority"])
    rows = summ.starred(store.heads(store.load(tmp_path)))
    assert len(rows) == 1 and rows[0].kind == "decision"


def test_starred_excludes_a_resolved_task(tmp_path):
    _n(tmp_path, kind="task", tags=["priority"], status="resolved")
    assert summ.starred(store.heads(store.load(tmp_path))) == []


def test_a_null_status_bug_is_not_open_but_is_counted_as_unstated(tmp_path):
    """A row carrying status: null is neither open nor resolved. Both halves
    asserted: it must leave open_notes AND arrive in no_status, or dropping
    the read_status tolerance would simply hide it.

    Must go in via _write_raw_legacy_row, not _n/add() -- add() normalizes a
    missing status to "open" before the row is ever written, which would
    make this test pass without ever exercising the None branch."""
    _write_raw_legacy_row(tmp_path)
    assert store.load(tmp_path)[0].status is None  # prove it isn't vacuous
    heads = store.heads(store.load(tmp_path))
    assert summ.open_notes(heads, "bug") == []
    assert len(summ.no_status(heads)) == 1
