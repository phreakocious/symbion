import pytest
from symbion import kinds as K
from symbion import store


def test_note_round_trips_through_dict():
    n = store.note_from_dict({
        "id": "20260904-000000-000000-abc", "kind": "decision",
        "target": {"type": "project", "name": None},
        "created_at": "2026-09-04T00:00:00", "author": "claude", "body": "b",
    })
    assert store.note_from_dict(store.note_to_dict(n)) == n


def test_the_atlas_key_is_no_longer_read():
    """v1's read-side synonym for `provenance`; repealed with v1 §Migration
    (spec 2026-09-08)."""
    n = store.note_from_dict({
        "id": "x", "kind": "check", "target": {"type": "project", "name": None},
        "created_at": "t", "atlas": {"sha": "abc"},
    })
    assert n.provenance is None


@pytest.mark.parametrize("kind", ["bug", "task", "idea", "question"])
def test_status_defaults_to_open_for_status_kinds(kind):
    assert store.default_status_for(K.DEFAULT_KINDS[kind]) == "open"


@pytest.mark.parametrize("kind", ["note", "decision", "check"])
def test_status_defaults_to_none_for_statusless_kinds(kind):
    assert store.default_status_for(K.DEFAULT_KINDS[kind]) is None


@pytest.mark.parametrize("old", ["followup", "anomaly", "audit"])
def test_a_retired_kind_is_refused_and_the_error_names_the_change(old):
    with pytest.raises(ValueError, match="vocabulary changed") as e:
        store.note_from_dict({"id": "x", "kind": old,
                              "target": {"type": "project", "name": None},
                              "created_at": "t"})
    assert "[kinds]" in str(e.value), "the hint must name the declaration route too"


def test_a_retired_name_that_is_declared_is_not_retired():
    n = store.note_from_dict({"id": "x", "kind": "anomaly",
                              "target": {"type": "project", "name": None},
                              "created_at": "t"},
                             kinds={"anomaly": K.Kind(status=True)})
    assert n.spec == K.Kind(status=True)


def test_spec_rides_on_the_row_and_never_reaches_disk():
    n = store.note_from_dict({"id": "x", "kind": "check",
                              "target": {"type": "project", "name": None},
                              "created_at": "t"})
    assert n.spec.verdict
    assert "spec" not in store.note_to_dict(n)
    assert store.note_from_dict(store.note_to_dict(n)) == n


def test_an_undeclared_kind_error_lists_the_declared_ones():
    with pytest.raises(ValueError, match="anomaly, note") as e:
        store.note_from_dict({"id": "x", "kind": "bug",
                              "target": {"type": "project", "name": None},
                              "created_at": "t"},
                             kinds={"anomaly": K.Kind(status=True), "note": K.Kind()})
    assert "vocabulary" not in str(e.value)


def test_a_genuinely_unknown_kind_gets_no_vocabulary_hint():
    """Both directions, or the message is decoration."""
    with pytest.raises(ValueError) as e:
        store.note_from_dict({"id": "x", "kind": "sausage",
                              "target": {"type": "project", "name": None},
                              "created_at": "t"})
    assert "vocabulary" not in str(e.value)


def test_provenance_key_is_also_read():
    n = store.note_from_dict({
        "id": "x", "kind": "check", "target": {"type": "project", "name": None},
        "created_at": "t", "provenance": {"sha": "abc"},
    })
    assert n.provenance == {"sha": "abc"}


def test_null_status_on_a_bug_is_neither_open_nor_resolved():
    """read_status used to return "open" here, on the fail-safe argument that
    an item wrongly shown costs a glance while one wrongly hidden costs the
    point of the store. At scale the glance is the cost: finished
    measurements read as a permanent backlog and the headline number
    stopped meaning anything. No adopting store relied on the guess
    (measured 2026-09-11). It is a defect in the file now, and
    `summary` names the count every run -- shown, without being guessed at."""
    n = store.note_from_dict({
        "id": "x", "kind": "bug", "target": {"type": "project", "name": None},
        "created_at": "t", "status": None,
    })
    assert store.read_status(n) is None


def test_null_status_on_a_decision_stays_none():
    n = store.note_from_dict({
        "id": "x", "kind": "decision", "target": {"type": "project", "name": None},
        "created_at": "t", "status": None,
    })
    assert store.read_status(n) is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        store.note_from_dict({"id": "x", "kind": "nope",
                              "target": {"type": "project", "name": None},
                              "created_at": "t"})


def test_the_kind_constants_are_gone():
    for name in ("NOTE_KINDS", "STATEFUL_KINDS", "CHECKLIST_KINDS"):
        assert not hasattr(store, name), f"store.{name} survived; read the bits off Note.spec"
