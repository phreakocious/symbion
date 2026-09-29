import json

import pytest
from symbion import store, catalog
from symbion.config import Config

pytestmark = pytest.mark.usefixtures("tmp_store")


def test_reconcile_reports_all_four_states(tmp_path):
    """Asserting only that a departed target reports `stale` passes on code
    that always says stale. `live` is the direction that must not rot."""
    act = store.create_arc(tmp_path, "x", "", "file")
    store.seed_arc(tmp_path, act.id, "file", ["here.py", "moved.py", "gone.py"])
    store.seed_arc(tmp_path, act.id, "item", ["freeform"])
    rows = store.reconcile_arc(
        store.load(tmp_path), act.id,
        live_for=lambda t: {"here.py", "renamed.py"},
        rename_map={"moved.py": "renamed.py"},
        catalog_types={"file"},
    )
    by_name = {r["target_name"]: r["status"] for r in rows}
    assert by_name == {"here.py": "live", "moved.py": "renamed",
                       "gone.py": "stale", "freeform": "uncheckable"}


def test_apply_retargets_renamed_and_leaves_stale_open(tmp_path):
    """The safe default needs its own assertion, or a regression to
    closing stale items passes silently."""
    act = store.create_arc(tmp_path, "x", "", "file")
    store.seed_arc(tmp_path, act.id, "file", ["moved.py", "gone.py"])
    cfg = Config(project_root=tmp_path)
    rows = store.reconcile_arc(
        store.load(tmp_path), act.id,
        live_for=lambda t: {"renamed.py"},
        rename_map={"moved.py": "renamed.py"}, catalog_types={"file"})
    retargeted, _, resolved = store.apply_reconciliation(tmp_path, rows, cfg)
    assert (retargeted, resolved) == (1, 0)
    items = {n.target.name: store.read_status(n)
             for n in store.arc_items(store.load(tmp_path), act.id)}
    assert items["renamed.py"] == "open"
    assert items["gone.py"] == "open", "stale must NOT be closed by --apply"


def test_resolve_stale_closes_them_explicitly(tmp_path):
    act = store.create_arc(tmp_path, "x", "", "file")
    store.seed_arc(tmp_path, act.id, "file", ["gone.py"])
    cfg = Config(project_root=tmp_path)
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                                    live_for=lambda t: set(), rename_map={},
                                    catalog_types={"file"})
    *_, resolved = store.apply_reconciliation(tmp_path, rows, cfg, resolve_stale=True)
    assert resolved == 1
    items = store.arc_items(store.load(tmp_path), act.id)
    assert store.read_status(items[0]) == "resolved"


def test_reconcile_excludes_a_resolved_task(tmp_path):
    """The exclusion direction: `reconcile_arc` skips non-open rows via
    `if read_status(n) != "open": continue`, but nothing asserted that a
    resolved item is actually dropped -- deleting the filter outright still
    left every other reconcile test green."""
    act = store.create_arc(tmp_path, "x", "", "file")
    made = store.seed_arc(tmp_path, act.id, "file", ["gone.py"])
    store.supersede(tmp_path, made[0].id, status="resolved")
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                                    live_for=lambda t: set(), rename_map={},
                                    catalog_types={"file"})
    assert rows == []


def test_reconcile_skips_a_null_status_task(tmp_path):
    """reconcile checks each OPEN task's target; a row that never said it was
    open is not one. Written straight into notes.jsonl, bypassing seed_arc's
    own normalization, which is what made an earlier version of this
    vacuous. test_reconcile_reports_all_four_states holds the other
    direction -- a genuinely open task with a dead target IS reported stale
    -- so this cannot pass by reporting nothing for everything."""
    act = store.create_arc(tmp_path, "x", "", "file")
    row = {
        "id": "legacy-1", "kind": "task",
        "target": {"type": "file", "name": "gone.py"},
        "created_at": "2020-01-01T00:00:00", "author": "claude",
        "body": "", "status": None, "arc_id": act.id,
    }
    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                                    live_for=lambda t: set(), rename_map={},
                                    catalog_types={"file"})
    assert rows == []


def test_git_rename_map_resolves_chains_transitively(tmp_path, monkeypatch):
    """A->B->C must map A to C, not to a B that no longer exists."""
    monkeypatch.setattr(catalog, "_git_rename_pairs",
                        lambda cfg: [("a.py", "b.py"), ("b.py", "c.py")])
    m = catalog.rename_map(Config(project_root=tmp_path, renames={"file": "git"}), "file")
    assert m["a.py"] == "c.py"
    assert m["b.py"] == "c.py"


def test_ambiguous_rename_is_dropped_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_git_rename_pairs",
                        lambda cfg: [("a.py", "b.py"), ("a.py", "c.py")])
    m = catalog.rename_map(Config(project_root=tmp_path, renames={"file": "git"}), "file")
    assert "a.py" not in m, "an ambiguous rename must fall through to stale"


PREREG = ('[kinds]\nprediction = { status = true, verdict = true }\n'
          'task = { status = true }\n')


def test_resolve_stale_skips_a_prediction_and_closes_a_task_in_the_same_run(tmp_path):
    """A pre-registration's verdict cannot be 'the target disappeared'. Both
    directions in one fixture: the task closes, the prediction stays open and
    is reported as needing a result."""
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(PREREG)
    act = store.create_arc(tmp_path, "x", "", "file")
    store.seed_arc(tmp_path, act.id, "file", ["gone.py"])
    p = store.add(tmp_path, kind="prediction", target={"type": "file", "name": "also_gone.py"},
                  arc_id=act.id, checked="the sweep")
    cfg = Config(project_root=tmp_path)
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                               live_for=lambda t: set(), rename_map={},
                               catalog_types={"file"})
    by_name = {r["target_name"]: r for r in rows}
    assert by_name["gone.py"]["needs_result"] is False
    assert by_name["also_gone.py"]["needs_result"] is True
    retargeted, _, resolved = store.apply_reconciliation(tmp_path, rows, cfg, resolve_stale=True)
    assert (retargeted, resolved) == (0, 1)
    items = {n.target.name: store.read_status(n)
             for n in store.arc_items(store.load(tmp_path), act.id)}
    assert items == {"gone.py": "resolved", "also_gone.py": "open"}
    head = [n for n in store.heads(store.load(tmp_path)) if n.kind == "prediction"][0]
    assert head.id == p.id, "the prediction was not superseded at all"


def test_apply_retargets_a_renamed_prediction(tmp_path):
    """Re-targeting preserves status, so the closure rule does not apply."""
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(PREREG)
    act = store.create_arc(tmp_path, "x", "", "file")
    store.add(tmp_path, kind="prediction", target={"type": "file", "name": "moved.py"},
              arc_id=act.id)
    cfg = Config(project_root=tmp_path)
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                               live_for=lambda t: {"renamed.py"},
                               rename_map={"moved.py": "renamed.py"}, catalog_types={"file"})
    assert store.apply_reconciliation(tmp_path, rows, cfg) == (1, 0, 0)
    (item,) = store.arc_items(store.load(tmp_path), act.id)
    assert (item.target.name, store.read_status(item)) == ("renamed.py", "open")


def test_apply_moves_every_row_on_a_renamed_name_not_only_the_arc_item(tmp_path):
    """`--apply` follows a rename the catalog proved, and a rename is a fact
    about the object: a row outside the arc on the old name, or one REF'ing
    it, must move too, or `context --target` on the new name splits the
    file's history the way `symbion rename` once did."""
    act = store.create_arc(tmp_path, "x", "", "file")
    store.seed_arc(tmp_path, act.id, "file", ["moved.py"])
    outside = store.add(tmp_path, kind="note", target={"type": "file", "name": "moved.py"})
    referrer = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                         refs=[{"type": "file", "name": "moved.py"}])
    rows = store.reconcile_arc(store.load(tmp_path), act.id,
                               live_for=lambda t: {"renamed.py"},
                               rename_map={"moved.py": "renamed.py"}, catalog_types={"file"})
    assert store.apply_reconciliation(tmp_path, rows, Config(project_root=tmp_path)) \
        == (2, 1, 0)
    hs = {n.supersedes: n for n in store.heads(store.load(tmp_path))}
    assert hs[outside.id].target.name == "renamed.py"
    assert hs[referrer.id].refs == (store.Target("file", "renamed.py"),)
