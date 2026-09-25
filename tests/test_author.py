import json
from symbion import store
from symbion.config import Config


def test_supersede_stamps_the_new_author_not_the_old_one(tmp_path):
    """The whole point. A correction is authored by the corrector."""
    a = store.add(tmp_path, kind="bug",
                  target={"type": "project", "name": None}, author="claude")
    b = store.supersede(tmp_path, a.id, status="resolved", author="ada")
    assert b.author == "ada"
    # and the original row is untouched, so the chain reads as a real timeline
    rows = store.load(tmp_path)
    assert [n.author for n in rows] == ["claude", "ada"]


def test_supersede_without_an_author_records_unknown_not_the_inherited_one(tmp_path):
    """An obviously-absent attribution beats a plausible false one."""
    a = store.add(tmp_path, kind="bug",
                  target={"type": "project", "name": None}, author="claude")
    b = store.supersede(tmp_path, a.id, status="resolved")
    assert b.author == "unknown"


def test_add_without_an_author_records_unknown_not_claude(tmp_path):
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    assert n.author == "unknown"


def test_seed_and_create_do_not_default_to_claude(tmp_path):
    act = store.create_arc(tmp_path, "x", "", "item")
    assert act.author == "unknown"
    made = store.seed_arc(tmp_path, act.id, "item", ["a"])
    assert made[0].author == "unknown"


def test_reconcile_attributes_its_writes_to_the_runner(tmp_path):
    """apply_reconciliation supersedes internally; those rows must carry the
    person who ran reconcile, not the person who wrote the task."""
    act = store.create_arc(tmp_path, "x", "", "file", author="claude")
    store.seed_arc(tmp_path, act.id, "file", ["gone.py"], author="claude")
    cfg = Config(project_root=tmp_path)
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                                    live_for=lambda t: set(), rename_map={},
                                    catalog_types={"file"})
    store.apply_reconciliation(tmp_path, rows, cfg, resolve_stale=True,
                               author="ada")
    head = store.arc_items(store.load(tmp_path), act.id)[0]
    assert head.author == "ada"


def test_rename_target_attributes_its_writes_to_the_runner(tmp_path):
    store.add(tmp_path, kind="note", target={"type": "item", "name": "old"},
              author="claude")
    store.rename_target(tmp_path, "item", "old", "new", author="ada")
    head = store.heads(store.load(tmp_path))[0]
    assert head.author == "ada"


def test_note_from_dict_with_no_author_key_reads_as_unknown_not_claude():
    """Same defect one layer down: a hand-edited row missing `author`
    entirely must not be guessed as claude's."""
    n = store.note_from_dict({
        "id": "x", "kind": "note", "target": {"type": "project", "name": None},
        "created_at": "t",
    })
    assert n.author == "unknown"


def test_arc_from_dict_with_no_author_key_reads_as_unknown_not_claude():
    d = {"id": "x", "name": "x", "target_scope": "item",
         "created_at": "2026-01-01T00:00:00"}
    assert store.arc_from_dict(d).author == "unknown"
