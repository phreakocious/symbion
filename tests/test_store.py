import fcntl
import json
import os
from datetime import datetime

import pytest

from symbion import kinds as K
from symbion import store

pytestmark = pytest.mark.usefixtures("tmp_store")


def _write_raw_legacy_row(store_dir, **overrides):
    """A pre-invariant row written straight into notes.jsonl, bypassing
    add()'s own normalization -- add() would silently turn status=None into
    "open" and make a null-status test vacuous."""
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


def test_add_then_load_round_trips(tmp_path):
    store.add(tmp_path, kind="decision", target={"type": "project", "name": None}, body="b")
    notes = store.load(tmp_path)
    assert len(notes) == 1 and notes[0].body == "b"


def test_supersede_collapses_to_one_head(tmp_path):
    a = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    b = store.supersede(tmp_path, a.id, status="resolved")
    hs = store.heads(store.load(tmp_path))
    assert [h.id for h in hs] == [b.id]
    assert len(store.load(tmp_path)) == 2, "the superseded row must stay on disk"


def test_supersede_inherits_the_stored_target(tmp_path):
    a = store.add(tmp_path, kind="task",
                  target={"type": "item", "name": "departed thing"})
    b = store.supersede(tmp_path, a.id, status="resolved")
    assert b.target.name == "departed thing"


def test_supersede_fast_forwards_a_stale_old_id_to_the_live_tip(tmp_path):
    """Everyday flow: correcting via an id copied from an earlier listing,
    after someone else already supersede()d it. Must chain onto the live
    tip, not fork off the original id a second time."""
    a = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    b = store.supersede(tmp_path, a.id, status="resolved")
    c = store.supersede(tmp_path, a.id, body="second correction")
    hs = store.heads(store.load(tmp_path))
    assert [h.id for h in hs] == [c.id]
    assert c.supersedes == b.id


@pytest.mark.parametrize("old, want", [
    (None, "more"),                      # a check may carry no body at all
    ("", "more"),
    ("text", "text\n\nmore"),
    ("text\n", "text\n\nmore"),          # a heredoc body ends in one newline
    ("text\n\n", "text\n\nmore"),
    ("text\n\n\n", "text\n\n\nmore"),    # never strips: the prefix is verbatim
])
def test_append_body_keeps_the_prior_body_as_a_verbatim_prefix(tmp_path, old, want):
    """An amendment to a pre-registration must leave the registered text
    byte-identical. Rebuild-and-replace made that the author's discipline;
    append makes it the tool's, so it may add the missing newlines of the
    paragraph break and nothing else."""
    a = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body=old)
    b = store.supersede(tmp_path, a.id, append_body="more")
    assert b.body == want
    assert b.body.startswith(old or "")


def test_append_body_extends_the_live_tip_not_the_named_row(tmp_path):
    """The prefix is read under the lock from the chain tip. Reading the
    NAMED row instead would drop every revision after it, silently -- the
    same shape as the tag merge that read the named row (api.retag)."""
    a = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="v1")
    store.supersede(tmp_path, a.id, body="v2")
    c = store.supersede(tmp_path, a.id, append_body="more")
    assert c.body == "v2\n\nmore"


@pytest.mark.parametrize("fields", [{"append_body": "  \n"},
                                    {"append_body": "x", "body": "y"}])
def test_append_body_refuses_nothing_and_a_second_body(tmp_path, fields):
    """Whitespace would change the body by a newline and say nothing; a body
    beside it is two sources for one field."""
    a = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="v1")
    with pytest.raises(ValueError):
        store.supersede(tmp_path, a.id, **fields)
    assert len(store.load(tmp_path)) == 1


def test_supersede_raises_on_a_cyclic_chain_instead_of_hanging(tmp_path):
    """add() doesn't validate the id/supersedes fields it's given, so a
    hand-edited notes.jsonl (or two raw add() calls) can produce a cycle.
    The fast-forward walk must detect it and raise, not loop forever while
    holding the store-wide lock."""
    target = {"type": "project", "name": None}
    store.add(tmp_path, id="aaa", kind="note", target=target, supersedes="bbb")
    store.add(tmp_path, id="bbb", kind="note", target=target, supersedes="aaa")
    with pytest.raises(ValueError):
        store.supersede(tmp_path, "aaa", body="racer")


def test_query_filters_on_every_field(tmp_path):
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "a"},
              tags=["x"], arc_id="act1")
    store.add(tmp_path, kind="decision", target={"type": "project", "name": None})
    notes = store.load(tmp_path)
    assert len(store.query(notes, kind="bug")) == 1
    assert len(store.query(notes, target_type="item")) == 1
    assert len(store.query(notes, target_name="a")) == 1
    assert len(store.query(notes, status="open")) == 1
    assert len(store.query(notes, arc_id="act1")) == 1
    assert len(store.query(notes, tag="x")) == 1
    assert len(store.query(notes, structured=True)) == 0


def test_malformed_line_does_not_break_the_read(tmp_path):
    store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    with open(tmp_path / "notes.jsonl", "a") as f:
        f.write("{not json\n")
    assert len(store.load(tmp_path)) == 1
    assert len(store.load_malformed(tmp_path)) == 1


def test_query_status_open_excludes_a_null_status_row(tmp_path):
    """`--status open` is a claim about what was written, not a guess. The
    sibling below holds the other end: an explicitly resolved row is excluded
    too, so this cannot pass by returning nothing for every query."""
    _write_raw_legacy_row(tmp_path)
    notes = store.load(tmp_path)
    assert len(store.query(notes, status="open")) == 0
    assert len(store.query(notes, kind="bug")) == 1, "the row is still IN the store"


def test_query_status_open_excludes_an_explicitly_resolved_row(tmp_path):
    """The direction that must not rot: a query that counted every bug
    regardless of status would also pass the sibling test above."""
    _write_raw_legacy_row(tmp_path, id="resolved-1", status="resolved")
    notes = store.load(tmp_path)
    assert len(store.query(notes, status="open")) == 0


def test_add_rejects_a_status_on_a_stateless_kind(tmp_path):
    with pytest.raises(ValueError):
        store.add(tmp_path, kind="decision", target={"type": "project", "name": None},
                  status="resolved")


def test_add_accepts_a_status_on_a_stateful_kind(tmp_path):
    n = store.add(tmp_path, kind="bug", target={"type": "project", "name": None},
                  status="resolved")
    assert n.status == "resolved"


def test_supersede_rejects_a_status_on_a_stateless_kind(tmp_path):
    """`add` guards stateless kinds against a caller-supplied status, but a
    correction reaches the same state a different way: `supersede` merges
    **fields onto the inherited row with no equivalent check, and `resolve`
    is `supersede(status="resolved")` unconditionally -- so `resolve` on a
    `note`/`decision`/`check` head used to silently succeed. This is the
    decision to close that hole at the shared write path instead (see
    test_cli.py for `resolve` reaching the same guard through the CLI)."""
    n = store.add(tmp_path, kind="decision", target={"type": "project", "name": None})
    with pytest.raises(ValueError):
        store.supersede(tmp_path, n.id, status="resolved")


def test_supersede_with_a_new_kind_that_cannot_hold_the_merged_fields_is_refused(tmp_path):
    """`_supersede_unlocked` used to validate against the OLD row's kind/spec,
    so changing kind on supersede could land a row `add` itself would refuse
    (kind=note, status=open). The merged kind -- what the row is BECOMING --
    must be the one checked, not what it WAS."""
    b = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    with pytest.raises(ValueError, match="status"):
        store.supersede(tmp_path, b.id, kind="note")


def test_supersede_with_a_new_kind_that_can_hold_the_merged_fields_succeeds(tmp_path):
    """The other direction: a kind change that the merged row's fields DO fit
    under must go through, and the head must actually read the new kind."""
    b = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    t = store.supersede(tmp_path, b.id, kind="task")
    assert (t.kind, t.status) == ("task", "open")


def test_supersede_to_an_undeclared_kind_names_the_kinds_table(tmp_path):
    b = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    with pytest.raises(ValueError, match=r"\[kinds\]"):
        store.supersede(tmp_path, b.id, kind="nope")


def test_created_at_carries_the_local_utc_offset(tmp_path):
    """Naive local reads as a lie beside any UTC stamp written by other tooling.
    The offset is appended, not converted, so rows written before this change
    still sort against rows written after it."""
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    stamp = datetime.fromisoformat(n.created_at)
    assert stamp.utcoffset() is not None, n.created_at
    assert n.created_at[:10].replace("-", "") == n.id[:8], "same wall clock as the id"


def test_query_by_id_selects_exactly_that_row(tmp_path):
    """A note's id is the handle every surface hands out -- the hook prints it,
    every add and supersede echoes it -- and it was the one lookup query could
    not do. Both directions: the named row comes back, its sibling does not,
    and an id that matches nothing returns empty rather than everything."""
    a = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  body="wanted")
    b = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  body="other")
    notes = store.load(tmp_path)
    assert len(notes) == 2
    assert [n.id for n in store.query(notes, id=a.id)] == [a.id]
    assert [n.id for n in store.query(notes, id=b.id)] == [b.id]
    assert store.query(notes, id="no-such-id") == []


def _declare(store_dir, text):
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(text)


PREREG = ('[kinds]\nprediction = { status = true, verdict = true }\n'
          'check = { verdict = true }\ntask = { status = true }\nnote = {}\n')


def test_load_reads_the_declared_table_and_files_an_undeclared_kind_as_unreadable(tmp_path):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\n')
    n = store.add(tmp_path, kind="anomaly", target={"type": "project", "name": None})
    assert store.read_status(n) == "open"
    with open(store.notes_path(tmp_path), "a") as f:
        f.write(json.dumps({"id": "old-1", "kind": "bug",
                            "target": {"type": "project", "name": None},
                            "created_at": "t", "author": "t", "body": "",
                            "status": "open"}) + "\n")
    assert [x.kind for x in store.load(tmp_path)] == ["anomaly"]
    (lineno, raw, err), = store.load_malformed(tmp_path)
    assert lineno == 2 and "'bug'" in err


def test_a_verdict_field_is_refused_on_a_kind_without_the_bit(tmp_path):
    with pytest.raises(ValueError, match="no verdict"):
        store.add(tmp_path, kind="decision", target={"type": "project", "name": None},
                  result="412 passed")
    with pytest.raises(ValueError, match="no verdict"):
        store.add(tmp_path, kind="bug", target={"type": "project", "name": None},
                  checked="suite")
    n = store.add(tmp_path, kind="check", target={"type": "project", "name": None},
                  checked="suite", result="412 passed")          # the other direction
    assert (n.checked, n.result) == ("suite", "412 passed")


def test_supersede_refuses_a_verdict_field_on_a_kind_without_the_bit(tmp_path):
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    with pytest.raises(ValueError, match="no verdict"):
        store.supersede(tmp_path, n.id, result="x")


def test_a_row_with_a_verdict_field_on_a_plain_kind_still_loads(tmp_path):
    """Read-side tolerance: the refusal is on write only."""
    store.ensure_store(tmp_path)
    with open(store.notes_path(tmp_path), "a") as f:
        f.write(json.dumps({"id": "old-1", "kind": "decision",
                            "target": {"type": "project", "name": None},
                            "created_at": "t", "author": "t", "body": "",
                            "result": "stray"}) + "\n")
    assert store.load(tmp_path)[0].result == "stray"


def test_a_prediction_cannot_be_resolved_without_a_result(tmp_path):
    _declare(tmp_path, PREREG)
    p = store.add(tmp_path, kind="prediction", target={"type": "project", "name": None},
                  checked="the benchmark with the shuffled-input control",
                  body="predicted: a 2x speedup; falsified if the control shows the same speedup")
    assert store.read_status(p) == "open"
    with pytest.raises(ValueError, match="--result"):
        store.supersede(tmp_path, p.id, status="resolved")
    with pytest.raises(ValueError, match="--result"):
        store.supersede(tmp_path, p.id, status="resolved", result="   ")
    r = store.supersede(tmp_path, p.id, status="resolved", result="HELD: control was flat")
    assert (r.status, r.result) == ("resolved", "HELD: control was flat")


def test_a_prediction_cannot_be_added_already_resolved_without_a_result(tmp_path):
    _declare(tmp_path, PREREG)
    with pytest.raises(ValueError, match="--result"):
        store.add(tmp_path, kind="prediction", target={"type": "project", "name": None},
                  status="resolved")
    n = store.add(tmp_path, kind="prediction", target={"type": "project", "name": None},
                  status="resolved", result="FIRED")
    assert n.status == "resolved"


def test_a_status_only_kind_resolves_without_a_result(tmp_path):
    """The other direction: the rule is on the shape, not on resolve."""
    _declare(tmp_path, PREREG)
    t = store.add(tmp_path, kind="task", target={"type": "project", "name": None})
    assert store.supersede(tmp_path, t.id, status="resolved").status == "resolved"


def test_a_resolved_prediction_keeps_its_result_on_a_later_correction(tmp_path):
    """`result` inherits, so a body-only correction to a resolved prediction
    is not refused as if the verdict had vanished."""
    _declare(tmp_path, PREREG)
    p = store.add(tmp_path, kind="prediction", target={"type": "project", "name": None})
    r = store.supersede(tmp_path, p.id, status="resolved", result="FIRED")
    c = store.supersede(tmp_path, r.id, body="and here is why")
    assert (c.status, c.result) == ("resolved", "FIRED")


def test_arc_items_keys_on_the_bits_not_the_label(tmp_path):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\nshelf = { status = true, parked = true }\nfact = {}\n')
    act = store.create_arc(tmp_path, "x", "", "item")
    a = store.add(tmp_path, kind="anomaly", target={"type": "item", "name": "a"}, arc_id=act.id)
    store.add(tmp_path, kind="shelf", target={"type": "item", "name": "s"}, arc_id=act.id)
    store.add(tmp_path, kind="fact", target={"type": "item", "name": "f"}, arc_id=act.id)
    assert [n.id for n in store.arc_items(store.load(tmp_path), act.id)] == [a.id]


def test_renaming_a_used_label_with_the_documented_sed_keeps_ids_and_chains(tmp_path):
    """Pins the on-disk pattern the starter toml's rename recipe relies on:
    `_dumps` uses json.dumps' default separators, so every row reads
    `"kind": "bug"` verbatim, and a plain string substitution over
    notes.jsonl is the whole migration -- ids and supersede chains survive
    it untouched."""
    b = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    r = store.supersede(tmp_path, b.id, status="resolved")

    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\n')
    assert store.load(tmp_path) == [], "the old label is no longer declared"
    assert len(store.load_malformed(tmp_path)) == 2

    path = store.notes_path(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert '"kind": "bug"' in text
    new_text = text.replace('"kind": "bug"', '"kind": "anomaly"')
    assert new_text != text, "the substitution must actually have matched"
    path.write_text(new_text, encoding="utf-8")

    notes = store.load(tmp_path)
    assert len(notes) == 2
    heads = store.heads(notes)
    assert [h.id for h in heads] == [r.id]
    assert heads[0].kind == "anomaly"
    assert heads[0].supersedes == b.id


# ---- canonicalize= runs under the lock, over everything, before any write ----
def _lock_held(store_dir) -> bool:
    """True iff another open-file-description holds the store lock: a
    non-blocking LOCK_EX from a fresh fd fails while the writer holds it."""
    fd = os.open(store_dir / store.LOCK_FILE, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_add_many_runs_canonicalize_under_the_lock_and_stores_its_answer(tmp_path):
    seen = {}

    def canon(rows):
        seen["held"] = _lock_held(tmp_path)
        return [{**r, "target": {"type": "item", "name": r["target"]["name"].upper()}}
                for r in rows]

    notes = store.add_many(tmp_path, [{"kind": "note", "target": {"type": "item", "name": "a"}},
                                      {"kind": "note", "target": {"type": "item", "name": "b"}}],
                           canonicalize=canon)
    assert seen["held"] is True
    assert [n.target.name for n in notes] == ["A", "B"]
    assert _lock_held(tmp_path) is False, "released after the write"


def test_add_many_without_canonicalize_is_unchanged(tmp_path):
    n = store.add_many(tmp_path, [{"kind": "note", "target": {"type": "item", "name": "a"}}])[0]
    assert n.target.name == "a"


def test_a_canonicalize_failure_writes_nothing(tmp_path):
    def canon(rows):
        raise ValueError("ambiguous, say")

    with pytest.raises(ValueError):
        store.add_many(tmp_path, [{"kind": "note", "target": {"type": "item", "name": "a"}},
                                  {"kind": "note", "target": {"type": "item", "name": "b"}}],
                       canonicalize=canon)
    assert store.load(tmp_path) == []
    assert _lock_held(tmp_path) is False


def test_seed_arc_runs_canonicalize_under_the_lock_over_the_names(tmp_path):
    act = store.create_arc(tmp_path, "x", "", "item")
    seen = {}

    def canon(names):
        seen["held"] = _lock_held(tmp_path)
        return [n.upper() for n in names]

    made = store.seed_arc(tmp_path, act.id, "item", ["a", "b"], canonicalize=canon)
    assert seen["held"] is True
    assert sorted(n.target.name for n in made) == ["A", "B"]
    assert store.seed_arc(tmp_path, act.id, "item", ["a"], canonicalize=canon) == [], \
        "idempotence is judged on the canonical name"


def test_supersede_runs_canonicalize_under_the_lock_over_the_fields(tmp_path):
    n = store.add(tmp_path, kind="note", target={"type": "item", "name": "a"})
    seen = {}

    def canon(fields):
        seen["held"] = _lock_held(tmp_path)
        seen["fields"] = dict(fields)
        return {**fields, "refs": [{"type": "item", "name": "R"}]}

    m = store.supersede(tmp_path, n.id, canonicalize=canon, refs=[{"type": "item", "name": "r"}])
    assert seen["held"] is True and seen["fields"] == {"refs": [{"type": "item", "name": "r"}]}
    assert [r.name for r in m.refs] == ["R"]
    assert m.target.name == "a", "the target is inherited, not the callback's to touch"


# ---- a legacy row's stray fields must not block every later correction ----
def test_supersede_heals_a_legacy_row_carrying_a_field_its_kind_cannot_hold(tmp_path):
    """A `note` row hand-written with a
    `checked` (a migration, or a kind whose bits changed) inherited it into
    every superseding row, and check_fields refused the correction naming
    flags the caller never passed. The reader loads a row as written; the
    WRITER owns the invariants, so the new row drops what its kind cannot
    carry and the old row stays on disk as history."""
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="b")
    path = store.notes_path(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.count('"checked": null') == 1
    path.write_text(text.replace('"checked": null', '"checked": "stray"'), encoding="utf-8")
    assert store.load(tmp_path)[0].checked == "stray", "the reader must load it as written"

    new = store.supersede(tmp_path, n.id, tags=["x"])
    assert new.tags == ("x",)
    assert new.checked is None
    assert store.heads(store.load(tmp_path)) == [new]


def test_a_status_the_kind_no_longer_holds_is_not_read(tmp_path):
    """The table is the semantics; the disk is the history. A kind given a
    status bit it should not carry mints every finished result open.
    Dropping the bit must take them out of every status view
    at once -- not after a healing sweep nobody knows to run -- and restoring
    it must bring them back, because nothing on disk changed."""
    both = '[kinds]\nmeasurement = { status = true, verdict = true }\n'
    _declare(tmp_path, both)
    store.add(tmp_path, kind="measurement", target={"type": "project", "name": None})
    assert store.query(store.load(tmp_path), status="open"), "control: open while the bit is set"

    _declare(tmp_path, '[kinds]\nmeasurement = { verdict = true }\n')
    n, = store.load(tmp_path)
    assert n.status == "open", "the reader loads the row as written"
    assert store.read_status(n) is None
    assert store.query([n], status="open") == []

    _declare(tmp_path, both)
    assert store.read_status(store.load(tmp_path)[0]) == "open"


def test_supersede_still_refuses_a_verdict_field_passed_explicitly_on_a_plain_kind(tmp_path):
    """The other direction: healing is for INHERITED fields only. A caller
    that passes one is the misuse check_fields exists to refuse."""
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="b")
    with pytest.raises(ValueError, match="verdict bit"):
        store.supersede(tmp_path, n.id, checked="x")
    assert len(store.load(tmp_path)) == 1


def test_supersede_with_a_raising_canonicalize_writes_nothing(tmp_path):
    """add_many's twin (test_add_many_runs_canonicalize_under_the_lock_...):
    a raise inside supersede's callback -- an ambiguous ref -- must leave the
    chain exactly as it was, no half-built correction appended."""
    n = store.add(tmp_path, kind="note", target={"type": "item", "name": "a"})

    def boom(fields):
        raise ValueError("ambiguous")

    with pytest.raises(ValueError, match="ambiguous"):
        store.supersede(tmp_path, n.id, canonicalize=boom, tags=["x"])
    assert store.load(tmp_path) == [n]


# ---- due: a date an open row falls past due ----
from datetime import timedelta, timezone

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=9)))


@pytest.mark.parametrize("due, want", [
    ("2026-09-23", (True, -1)),
    ("2026-09-24", (False, 0)),               # a date is due through the end of its day
    ("2026-10-01", (False, 7)),
    ("2026-09-24T11:00:00+09:00", (True, 0)),  # an instant earlier today is past
    ("2026-09-24T13:00:00+09:00", (False, 0)),
    ("2026-09-24T02:30:00+00:00", (True, 0)),  # 11:30 at NOW's offset
    ("2026-09-23T23:00:00+00:00", (True, 0)),  # 08:00 at NOW's offset: today, though UTC reads the 23rd
    ("2026-09-24T14:00:00+00:00", (False, 0)),  # 23:00 at NOW's offset: still today
])
def test_due_state_is_past_due_and_calendar_days_at_now(due, want):
    assert store.due_state(due, NOW) == want


def test_due_state_of_an_unreadable_value_is_none_not_a_crash():
    """The writer refuses a bad date; a hand-edited row still has to load and
    summarize."""
    assert store.due_state("next week", NOW) is None


@pytest.mark.parametrize("given, want", [
    ("2026-10-01", "2026-10-01"),
    (" 2026-10-01 ", "2026-10-01"),
    ("2026-10-01T22:30Z", "2026-10-01T22:30:00+00:00"),
])
def test_canon_due_normalizes_what_it_accepts(given, want):
    assert store.canon_due(given) == want


def test_canon_due_pins_a_naive_datetime_to_the_writers_offset():
    """A naive time means the author's local time; a reader elsewhere must not
    reinterpret it, so it is stored with the offset of the machine that wrote it."""
    assert datetime.fromisoformat(store.canon_due("2026-10-01T09:00")).tzinfo is not None


def test_canon_due_refuses_anything_else_and_names_the_format():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        store.canon_due("next week")


def test_due_only_on_a_status_kind(tmp_path):
    """Only an open row can be past due. A plain note or a check has no open
    state, so a date on it could never fire."""
    with pytest.raises(ValueError, match="status bit"):
        store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  due="2026-10-01")
    t = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  due="2026-10-01")
    assert t.due == "2026-10-01"


def test_a_bad_due_is_refused_at_the_store(tmp_path):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  due="soon")
    assert store.load(tmp_path) == []


def test_supersede_inherits_due_and_none_clears_it(tmp_path):
    a = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  due="2026-10-01")
    b = store.supersede(tmp_path, a.id, body="edited")
    assert b.due == "2026-10-01"
    c = store.supersede(tmp_path, a.id, due=None)
    assert c.due is None


def test_both_write_paths_store_the_canonical_due(tmp_path):
    """add and supersede each normalize, so no caller -- CLI, --from-json,
    GUI -- can store a second spelling of one date."""
    a = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  due="2026-10-01T22:30Z")
    assert a.due == "2026-10-01T22:30:00+00:00"
    b = store.supersede(tmp_path, a.id, due=" 2026-10-02 ")
    assert b.due == "2026-10-02"


# ---- rename_target: every shape that carries the name ----
def _ref(t, n):
    return {"type": t, "name": n}


def test_rename_moves_targets_and_repoints_refs_one_case_per_shape(tmp_path):
    """Measured 2026-09-24: `rename` moved the notes ON the item and left a
    note that REF'd it on the old name, so `context --target` on the new
    name omitted it. One row per shape: target only, ref only, both on one
    row, a ref list with a second entry that must survive, a row that
    already REF'd the new name, the same name under another type, which
    must not move, and an unrelated row with a legacy self-edge, which the
    sweep once superseded uncounted (2026-09-29)."""
    add = lambda **kw: store.add(tmp_path, kind="note", **kw).id
    on = add(target=_ref("item", "old"))
    ref_only = add(target=_ref("project", None), refs=[_ref("item", "old")])
    # A self-edge from before add refused one: written the way it arrived.
    legacy = store.note_from_dict({"id": store.new_id(), "kind": "note",
                                   "created_at": "2026-09-01T00:00:00Z",
                                   "target": _ref("item", "old"), "refs": [_ref("item", "old")]})
    store._append_note_unlocked(tmp_path, legacy)
    both = legacy.id
    stray = store.note_from_dict({"id": store.new_id(), "kind": "note",
                                  "created_at": "2026-09-01T00:00:00Z",
                                  "target": _ref("item", "elsewhere"),
                                  "refs": [_ref("item", "elsewhere")]})
    store._append_note_unlocked(tmp_path, stray)
    onto_ref = add(target=_ref("item", "old"), refs=[_ref("item", "new")])
    mixed = add(target=_ref("project", None),
                refs=[_ref("file", "a.py"), _ref("item", "old"), _ref("commit", "abc")])
    other_type = add(target=_ref("project", None), refs=[_ref("file", "old")])
    already = add(target=_ref("project", None), refs=[_ref("item", "new"), _ref("item", "old")])

    assert store.rename_target(tmp_path, "item", "old", "new") == (3, 4)

    notes = store.load(tmp_path)
    head = {n.supersedes or n.id: n for n in store.heads(notes)}
    T = store.Target
    assert head[on].target == T("item", "new")
    assert head[ref_only].refs == (T("item", "new"),)
    assert (head[both].target, head[both].refs) == (T("item", "new"), ()), "self-edge dropped"
    assert (head[onto_ref].target, head[onto_ref].refs) == (T("item", "new"), ()), \
        "a rename never makes a self-edge"
    assert head[mixed].refs == (T("file", "a.py"), T("item", "new"), T("commit", "abc"))
    assert head[other_type].id == other_type, "a file:old ref moved on an item rename"
    assert head[stray.id].id == stray.id, "a row the rename does not touch was rewritten"
    assert head[already].refs == (T("item", "new"),), "one object, one entry"
    assert len(notes) == 8 + 6, "each changed row is superseded exactly once"
    assert {n.id for n in store.heads_for(tmp_path, "item", "new")} == \
        {head[k].id for k in (on, ref_only, both, onto_ref, mixed, already)}
    assert store.heads_for(tmp_path, "item", "old") == []


def test_rename_of_a_name_carried_only_by_refs_is_not_a_miss(tmp_path):
    """The CLI exits 1 on (0, 0): a name no row carries is a typo. A name only
    refs carry is a real object with nothing filed ON it."""
    store.add(tmp_path, kind="note", target=_ref("project", None), refs=[_ref("item", "old")])
    assert store.rename_target(tmp_path, "item", "old", "new") == (0, 1)
    assert store.rename_target(tmp_path, "item", "old", "new") == (0, 0)
