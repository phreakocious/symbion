try:
    import fcntl
except ImportError:                     # Windows
    fcntl = None
    import msvcrt
import json
import os
import re
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


def test_every_store_has_a_docs_dir(tmp_path):
    """docs/ is part of every store (the user, 2026-10-15): a new one has it,
    and one made before then has it from its next write."""
    s = tmp_path / "s"
    store.ensure_store(s)
    assert (s / "docs").is_dir()
    (s / "docs").rmdir()
    store.add(s, kind="note", target={"type": "project", "name": None}, body="x")
    assert (s / "docs").is_dir()


def test_add_then_load_round_trips(tmp_path):
    store.add(tmp_path, kind="decision", target={"type": "project", "name": None}, body="b")
    notes = store.load(tmp_path)
    assert len(notes) == 1 and notes[0].body == "b"


def test_a_load_reads_a_hand_edit_of_the_same_length_and_gives_its_own_list(tmp_path):
    """store._PARSED keeps a parse only for an equal text: a same-length
    edit can keep the size and, on a coarse clock, the mtime. A caller
    appends to what load returns, and the next load must not see it."""
    store.add(tmp_path, kind="decision", target={"type": "project", "name": None}, body="aaaa")
    store.load(tmp_path)
    rows = store.load(tmp_path)                 # the kept parse
    rows.append(rows[0])
    assert len(store.load(tmp_path)) == 1
    p = store.notes_path(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("aaaa", "bbbb"), encoding="utf-8")
    assert store.load(tmp_path)[0].body == "bbbb"


def test_supersede_collapses_to_one_head(tmp_path):
    a = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    b = store.supersede(tmp_path, a.id, status="resolved")
    hs = store.heads(store.load(tmp_path))
    assert [h.id for h in hs] == [b.id]
    assert len(store.load(tmp_path)) == 2, "the superseded entry must stay on disk"


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
    rows = store.load(tmp_path)
    by = {n.id: n for n in rows}
    assert [h.id for h in store.heads(rows)] == [c.id] == [b.id] == [a.id]
    assert [by[c.supersedes].status, by[by[c.supersedes].supersedes].status] == \
        ["resolved", "open"], "c chains onto b's version, which chains onto a's"


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


def test_malformed_line_does_not_break_the_read(tmp_path):
    store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    with open(tmp_path / "notes.jsonl", "a") as f:
        f.write("{not json\n")
    assert len(store.load(tmp_path)) == 1
    assert len(store.load_malformed(tmp_path)) == 1


def test_an_append_after_a_torn_last_line_starts_its_own_line(tmp_path):
    """ENOSPC, a killed write or an editor that drops the final newline leaves
    the last line unterminated, and the next row was written onto it: an id
    printed at exit 0, then read as neither row (2026-10-09)."""
    project = {"type": "project", "name": None}
    a = store.add(tmp_path, kind="note", target=project, body="a")
    path = store.notes_path(tmp_path)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    b = store.add(tmp_path, kind="note", target=project, body="b")
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"torn')
    c = store.add(tmp_path, kind="note", target=project, body="c")
    assert [n.id for n in store.load(tmp_path)] == [a.id, b.id, c.id]
    assert [raw for _, raw, _ in store.load_malformed(tmp_path)] == ['{"torn']


def test_a_status_or_tags_the_store_cannot_read_are_refused(tmp_path):
    """The CLI and GUI offer only open and resolved; a script against the
    store stored `wontfix`, a row that then sat in no view, and `tags="abc"`
    stored the tags a, b and c."""
    item = {"type": "item", "name": "s"}
    with pytest.raises(ValueError, match="status 'wontfix'"):
        store.add(tmp_path, kind="bug", target=item, status="wontfix")
    with pytest.raises(ValueError, match="tags must be a list"):
        store.add(tmp_path, kind="bug", target=item, tags="abc")
    n = store.add(tmp_path, kind="bug", target=item)
    with pytest.raises(ValueError, match="status 'bogus'"):
        store.supersede(tmp_path, n.id, status="bogus")
    assert [(r.status, r.tags) for r in store.load(tmp_path)] == [("open", ())]


def test_query_status_open_excludes_a_null_status_row(tmp_path):
    """`--status open` is a claim about what was written, not a guess. The
    sibling below holds the other end: an explicitly resolved row is excluded
    too, so this cannot pass by returning nothing for every query."""
    _write_raw_legacy_row(tmp_path)
    notes = store.load(tmp_path)
    assert len(store.query(notes, status="open")) == 0
    assert len(store.query(notes, kind="bug")) == 1, "the entry is still IN the store"


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


def test_a_kind_change_keeps_the_status_and_verdict_bits(tmp_path):
    """A note moved to task landed with no status, and a task moved to
    prediction landed with no registered run and no stamp: rows `add` refuses
    (2026-10-14). The parked bit may change, so a task parks as an idea in
    place; status and verdict may not."""
    P = {"type": "project", "name": None}
    t = store.add(tmp_path, kind="task", target=P)
    n = store.add(tmp_path, kind="note", target=P)
    for nid, kind in [(n.id, "task"), (t.id, "prediction"), (t.id, "note")]:
        with pytest.raises(ValueError, match="status and verdict bits"):
            store.supersede(tmp_path, nid, kind=kind)
    assert store.supersede(tmp_path, t.id, kind="idea").kind == "idea"
    assert store.supersede(tmp_path, n.id, kind="decision").kind == "decision"


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


@pytest.mark.parametrize("value,has", [(None, False), ("", False), ("  ", False),
                                       ("None", False), ("null", False),
                                       ("HELD", True), ("0", True)])
def test_has_result_is_one_rule_for_a_verdict_present(value, has):
    """The resolve gate and the `pending` state both ask it: a blank, or a
    null's spelling left on disk by an older write, is no verdict."""
    assert store.has_result(value) is has


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
    assert [h.id for h in heads] == [r.id] == [b.id]
    assert heads[0].kind == "anomaly"
    assert heads[0].supersedes == next(n.id for n in notes if n.id != b.id)


# ---- canonicalize= runs under the lock, over everything, before any write ----
def _lock_held(store_dir) -> bool:
    """True iff another open-file-description holds the store lock: a
    non-blocking LOCK_EX from a fresh fd fails while the writer holds it."""
    fd = os.open(store_dir / store.LOCK_FILE, os.O_RDWR)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, PermissionError):     # PermissionError: msvcrt's EACCES
        return True
    else:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
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


def test_a_supersede_that_drops_a_field_keeps_the_old_row(tmp_path):
    """A dropped field is gone from the head, not from disk: the old row
    stays as the copy that keeps it."""
    n = store.add(tmp_path, kind="decision", target={"type": "project", "name": None},
                  body="b", author="claude")
    path = store.notes_path(tmp_path)
    path.write_text(path.read_text(encoding="utf-8")
                    .replace('"checked": null', '"checked": "ran it"'), encoding="utf-8")
    new = store.supersede(tmp_path, n.id, author="claude", tags=["x"])
    assert new.supersedes is not None and new.checked is None
    assert [r.checked for r in store.load(tmp_path)] == ["ran it", None]


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
    assert n.status == "open", "the reader loads the entry as written"
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
    head = {n.id: n for n in store.heads(notes)}
    T = store.Target
    assert head[on].target == T("item", "new")
    assert head[ref_only].refs == (T("item", "new"),)
    assert (head[both].target, head[both].refs) == (T("item", "new"), ()), "self-edge dropped"
    assert (head[onto_ref].target, head[onto_ref].refs) == (T("item", "new"), ()), \
        "a rename never makes a self-edge"
    assert head[mixed].refs == (T("file", "a.py"), T("item", "new"), T("commit", "abc"))
    assert head[other_type].id == other_type, "a file:old ref moved on an item rename"
    assert head[stray.id].id == stray.id, "an entry the rename does not touch was rewritten"
    assert head[already].refs == (T("item", "new"),), "one object, one entry"
    assert len(notes) == 8 + 6, "each changed entry is superseded exactly once"
    assert {n.id for n in store.heads_for(tmp_path, "item", "new")} == \
        {head[k].id for k in (on, ref_only, both, onto_ref, mixed, already)}
    assert store.heads_for(tmp_path, "item", "old") == []


def test_rename_of_a_name_carried_only_by_refs_is_not_a_miss(tmp_path):
    """The CLI exits 1 on (0, 0): a name no row carries is a typo. A name only
    refs carry is a real object with nothing filed ON it."""
    store.add(tmp_path, kind="note", target=_ref("project", None), refs=[_ref("item", "old")])
    assert store.rename_target(tmp_path, "item", "old", "new") == (0, 1)
    assert store.rename_target(tmp_path, "item", "old", "new") == (0, 0)


def test_time_is_utc_unless_tz_is_set(monkeypatch):
    """Everything UTC unless the user asks, and TZ is the ask (ruled
    2026-09-30): stamps, ids, printed times, and an offset-less input. A
    stored stamp keeps the zone it was written in."""
    from datetime import timezone
    utc = timezone.utc
    assert store.zone() is utc
    monkeypatch.setattr(store, "_utcnow", lambda: datetime(2026, 9, 30, 23, 58, 29, tzinfo=utc))
    assert store._now_iso() == "2026-09-30T23:58:29+00:00"
    assert store.new_id(_rand=lambda: 0) == "20260930-235829-000000-000"
    assert store.since_cutoff("2026-09-03T11:00") == datetime(2026, 9, 3, 11, tzinfo=utc)
    assert store.since_cutoff("2026-09-03") == datetime(2026, 9, 3, tzinfo=utc)
    assert store.canon_due("2026-10-01T09:00") == "2026-10-01T09:00:00+00:00"
    assert store.shown("2026-10-01T09:00:00+09:00") == "2026-10-01 00:00:00Z"
    assert store.shown("2026-10-01") == "2026-10-01"
    monkeypatch.setenv("TZ", "set")
    assert store.zone() is None
    assert store.since_cutoff("2026-09-03T11:00") == datetime(2026, 9, 3, 11).astimezone()
    assert store.canon_due("2026-10-01T09:00") == datetime(2026, 10, 1, 9).astimezone().isoformat()


def test_a_coarse_clock_still_mints_ids_in_write_order():
    """Windows before Python 3.13 reads the clock in ~15.6 ms ticks, so a batch
    shared one microsecond and the random tail ordered it: the summary listed
    a batch out of order (measured 2026-10-06, Python 3.11 on Windows). A
    repeat of the last reading steps one microsecond on; a clock set back more
    than a second is read as it is."""
    from datetime import timezone
    tick = datetime(2026, 10, 6, 13, 49, 26, 953263, tzinfo=timezone.utc)
    tails = iter([0xfff, 0x800, 0x000])
    ids = [store.new_id(_clock=lambda: tick, _rand=lambda: next(tails)) for _ in range(3)]
    assert ids == sorted(ids) and len({i[:22] for i in ids}) == 3, ids
    back = datetime(2026, 10, 6, 13, 49, 20, tzinfo=timezone.utc)
    assert store.new_id(_clock=lambda: back, _rand=lambda: 0) == "20261006-134920-000000-000"


def test_rows_from_two_zones_list_in_the_order_they_were_written(tmp_path):
    """A string sort put `10-01T05:00+09:00` after `09-30T22:00+00:00`, though
    it is two hours earlier: a store written from two zones listed out of order."""
    for when, body in (("2026-09-30T22:00:00+00:00", "later"),
                       ("2026-10-01T05:00:00+09:00", "earlier"),
                       ("not a stamp", "unreadable")):
        store.add(tmp_path, kind="note", target={"type": "item", "name": "x"},
                  body=body, created_at=when)
    assert [n.body for n in store.heads_for(tmp_path, "item", "x")] == \
        ["later", "earlier", "unreadable"]


# ---- an edit before `symbion commit` ----

def _commit(store_dir):
    import subprocess
    subprocess.run(["git", "-C", str(store_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(store_dir), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "c"], check=True)


@pytest.mark.parametrize("case", ["uncommitted", "committed", "other author", "resolve",
                                  "pre-registration"])
def test_every_edit_keeps_the_version_it_replaces(tmp_path, case):
    """An uncommitted edit by the row's own author rewrote its line until
    2026-10-26: `show` gave an appended correction no date or author."""
    _declare(tmp_path, PREREG)
    kind = "prediction" if case == "pre-registration" else "task"
    a = store.add(tmp_path, kind=kind, target={"type": "project", "name": None},
                  body="v1", author="ann", checked="the run" if kind == "prediction" else None)
    if case == "committed":
        _commit(tmp_path)
    # A pre-registration's body may only extend its registered text.
    edit = ({"status": "resolved"} if case == "resolve"
            else {"body": "v1\n\nv2" if kind == "prediction" else "v2"})
    b = store.supersede(tmp_path, a.id, author="bob" if case == "other author" else "ann",
                        **edit)
    assert b.id == a.id and b.supersedes not in (None, a.id)
    kept = next(n for n in store.load(tmp_path) if n.id == b.supersedes)
    assert (kept.body, kept.created_at, kept.author) == ("v1", a.created_at, "ann")


# json.dumps(ensure_ascii=False) leaves these raw, and str.splitlines() splits
# on each: a body holding one was written as one line and read back as none.
SEPARATORS = "a b c\x85d"


def test_a_line_separator_in_a_body_reads_back_whole(tmp_path):
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  body=SEPARATORS)
    a = store.create_arc(tmp_path, "arc", SEPARATORS, "item")
    assert [r.body for r in store.load(tmp_path)] == [SEPARATORS], store.load_malformed(tmp_path)
    assert [r.description for r in store.load_arcs(tmp_path)] == [SEPARATORS]
    assert (n.id, a.id) == (store.load(tmp_path)[0].id, store.load_arcs(tmp_path)[0].id)


def test_an_edit_keeps_a_line_separator_row_whole(tmp_path):
    """The rewrite rebuilt the file from split lines joined by "\\n", so the
    separator became a real newline on disk."""
    project = {"type": "project", "name": None}
    a = store.add(tmp_path, kind="note", target=project, body=SEPARATORS, author="ann")
    b = store.add(tmp_path, kind="note", target=project, body="v1", author="ann")
    new = store.supersede(tmp_path, b.id, author="ann", body="v2")
    assert [(n.id, n.body) for n in store.load(tmp_path)] == \
        [(a.id, SEPARATORS), (new.supersedes, "v1"), (b.id, "v2")]


def test_a_committed_line_separator_row_reads_as_committed(tmp_path):
    """A committed row split in two read as uncommitted, and the commit
    trailer counted it as `?`."""
    from types import SimpleNamespace
    store.add(tmp_path, kind="note", target={"type": "project", "name": None},
              body=SEPARATORS, author="ann")
    assert store.commit(tmp_path, "c", SimpleNamespace(git_name="t", git_email="t@t"))
    import subprocess
    msg = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%B"],
                         capture_output=True, text=True, encoding="utf-8").stdout
    assert "Rows: ann 1" in msg, msg


def test_a_commit_with_keep_leaves_the_other_rows_pending(tmp_path):
    """The GUI's commit button took an agent's pending rows under the GUI's
    message (2026-10-09). With `keep`, the other rows stay pending for their
    writer, and so does any file but the registry; a kept revision brings
    its pending base, or the commit would hold a revision of no row."""
    import subprocess
    from types import SimpleNamespace
    from symbion import gitref
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    store.add(tmp_path, kind="note", target=project, body="old", author="ann")
    assert store.commit(tmp_path, "c", cfg)
    bob = store.add(tmp_path, kind="note", target=project, body="bob's", author="bob")
    store.add(tmp_path, kind="note", target=project, body="ann's", author="ann")
    (tmp_path / "plan.md").write_text("bob's plan")
    ann = lambda r: r.get("author") == "ann"
    assert store.commit(tmp_path, "c2", cfg, keep=ann)

    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], capture_output=True,
                              text=True, encoding="utf-8").stdout
    assert "Rows: ann 1" in git("log", "-1", "--format=%B")
    assert [r["body"] for r in store.pending_rows(tmp_path)] == ["bob's"]
    assert gitref.uncommitted(tmp_path) == (1, False)
    assert "plan.md" in git("status", "--porcelain")
    assert not store.commit(tmp_path, "c3", cfg, keep=ann), "nothing of ann's is pending"

    task = store.add(tmp_path, kind="task", target=project, body="t", author="bob",
                     status="open")
    store.supersede(tmp_path, task.id, author="ann", status="resolved")
    assert store.commit(tmp_path, "c4", cfg, keep=ann)
    assert "Rows: ann 1, bob 1" in git("log", "-1", "--format=%B")
    assert [r["id"] for r in store.pending_rows(tmp_path)] == [bob.id]


def test_own_rows_are_the_sessions_and_with_none_the_authors(tmp_path, monkeypatch):
    """`commit` swept a live session's rows into another's commit four times
    (2026-10-06 to 10-10). Every Claude session writes as `claude`, so the
    session key decides: a row this session wrote is its own, whoever it is
    written as (a row in the user's words is written as the user). With no key, the
    author's rows that no session wrote."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    store.add(tmp_path, kind="note", target=project, body="old", author="ann")
    assert store.commit(tmp_path, "c", cfg)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    mine = store.add(tmp_path, kind="note", target=project, body="mine", author="claude")
    as_ann = store.add(tmp_path, kind="idea", target=project, body="ann's words", author="ann")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    theirs = store.add(tmp_path, kind="note", target=project, body="theirs", author="claude")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    by_hand = store.add(tmp_path, kind="note", target=project, body="by hand", author="ann")

    rows = store.pending_rows(tmp_path)
    def own(key, author):
        if key:
            monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", key)
        else:
            monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        keep = store.own_rows(tmp_path, author)
        return {r["id"] for r in rows if keep(r)}
    assert own("s1", "claude") == {mine.id, as_ann.id}
    assert own("s2", "claude") == {theirs.id}
    assert own(None, "ann") == {by_hand.id}
    assert own(None, "claude") == set()
    monkeypatch.setenv("CODEX_THREAD_ID", "s2")         # the innermost agent's key
    assert own("s1", "codex") == {theirs.id}


def test_a_commit_forgets_the_sessions_of_the_rows_it_took(tmp_path, monkeypatch):
    """The record of which session wrote a row matters only while the row is
    pending, so a commit keeps the entries of the rows it left and no more."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    a = store.add(tmp_path, kind="note", target=project, body="a", author="claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    b = store.add(tmp_path, kind="note", target=project, body="b", author="claude")
    assert set(store.sessions(tmp_path)) == {a.id, b.id}
    assert store.commit(tmp_path, "c", cfg, keep=store.own_rows(tmp_path, "claude"))
    assert store.sessions(tmp_path) == {a.id: "s1"}
    assert [r["id"] for r in store.pending_rows(tmp_path)] == [a.id]


def test_each_version_is_pending_for_the_session_that_wrote_it(tmp_path, monkeypatch):
    """One Claude session's edit of another's uncommitted `claude` row keeps
    that session's version, and that version is still that session's to
    commit."""
    project = {"type": "project", "name": None}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    a = store.add(tmp_path, kind="task", target=project, body="v1", author="claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    b = store.supersede(tmp_path, a.id, author="claude", body="v2")
    assert b.supersedes, "s2's edit kept s1's version"
    # The version s2's edit kept is still s1's to commit; the edit is s2's.
    rows = store.pending_rows(tmp_path)
    assert {r["body"] for r in rows if store.own_rows(tmp_path, "claude")(r)} == {"v2"}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert {r["body"] for r in rows if store.own_rows(tmp_path, "claude")(r)} == {"v1"}
    c = store.supersede(tmp_path, b.id, author="claude", body="v3")
    assert c.supersedes != b.supersedes and len(store.load(tmp_path)) == 3
    # An edit outside any session is no session's, and the author's own.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    store.supersede(tmp_path, a.id, author="ann", body="v4")
    rows = store.pending_rows(tmp_path)
    assert {r["body"] for r in rows if store.own_rows(tmp_path, "ann")(r)} == {"v4"}


def test_interleaved_keep_commits_leave_the_right_rows_pending(tmp_path, monkeypatch):
    """Two sessions commit their own rows out of file order. A moved line
    must not read as pending, and the file takes HEAD's order, with the rows
    a commit left after it: a commit wrote its rows after HEAD's, but left
    them among the others on disk, and git read the difference as moved
    lines that no later commit stages (2026-10-12)."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    store.add(tmp_path, kind="note", target=project, body="old", author="ann")
    assert store.commit(tmp_path, "c", cfg)
    ids = {}
    for key, body in (("s1", "a1"), ("s2", "b1"), ("s1", "a2"), ("s2", "b2")):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", key)
        ids[body] = store.add(tmp_path, kind="note", target=project, body=body,
                              author="claude").id
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    assert store.commit(tmp_path, "c2", cfg, keep=store.own_rows(tmp_path, "claude"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s3")
    store.add(tmp_path, kind="note", target=project, body="c1", author="claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert store.commit(tmp_path, "c3", cfg, keep=store.own_rows(tmp_path, "claude"))
    import subprocess
    head = subprocess.run(["git", "-C", str(tmp_path), "show", "HEAD:notes.jsonl"],
                          capture_output=True, text=True).stdout.splitlines()
    disk = (tmp_path / "notes.jsonl").read_text().splitlines()
    assert disk[:len(head)] == head, "the file is in HEAD's order"
    assert [json.loads(ln)["body"] for ln in disk[len(head):]] == ["c1"]
    assert [r["body"] for r in store.pending_rows(tmp_path)] == ["c1"]


def test_a_keep_commit_leaves_no_line_moved_against_head(tmp_path, monkeypatch):
    """A version another session's edit kept stays where its row's line is,
    and a pure move, which no commit stages, is put back by the next one."""
    import subprocess
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    path = tmp_path / "notes.jsonl"
    old = store.add(tmp_path, kind="note", target=project, body="old", author="ann")
    x = store.add(tmp_path, kind="task", target=project, body="x", author="ann", status="open")
    assert store.commit(tmp_path, "c", cfg)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    kept = store.supersede(tmp_path, x.id, author="claude", body="x2").supersedes
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    y = store.add(tmp_path, kind="note", target=project, body="y", author="claude")
    assert store.commit(tmp_path, "c2", cfg, keep=store.own_rows(tmp_path, "claude"))
    ids = lambda: [json.loads(ln)["id"] for ln in path.read_text().splitlines()]
    assert ids() == [old.id, kept, y.id, x.id]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    assert store.commit(tmp_path, "c3", cfg, keep=store.own_rows(tmp_path, "claude"))
    head = lambda: subprocess.run(["git", "-C", str(tmp_path), "show", "HEAD:notes.jsonl"],
                                  capture_output=True, text=True).stdout
    assert path.read_text() == head()

    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[1], lines[0], *lines[2:]]) + "\n")
    assert not store.commit(tmp_path, "c4", cfg, keep=lambda r: False), "nothing to take"
    assert path.read_text() == head()


def test_an_edit_keeps_the_rows_id_and_the_earlier_version_takes_a_new_one(tmp_path):
    """A row went through three ids in one session, and cites, commit
    messages and chat went stale within the hour (2026-10-11). The head keeps
    the id; the version it replaces keeps its line, its time and its author
    under a new id, so the chain every reader walks is whole."""
    project = {"type": "project", "name": None}
    a = store.add(tmp_path, kind="task", target=project, body="v1", author="ann")
    z = store.add(tmp_path, kind="note", target=project, body="after", author="ann")
    _commit(tmp_path)
    b = store.supersede(tmp_path, a.id, author="bob", append_body="more")
    rows = store.load(tmp_path)
    assert [(n.id, n.body, n.author, n.created_at) for n in rows] == [
        (b.supersedes, "v1", "ann", a.created_at), (z.id, "after", "ann", z.created_at),
        (a.id, "v1\n\nmore", "bob", b.created_at)]
    assert [h.id for h in store.heads(rows)] == [z.id, a.id]
    assert len({n.id for n in rows}) == len(rows)


def test_a_duplicate_id_reads_as_unreadable_and_its_row_refuses_a_write(tmp_path):
    """Two copies of a store edited one row, and a merge that kept both sides
    left two heads under one id: `list` printed both, and a resolve closed
    one (2026-10-13). The later line is skipped and named, and a write to
    that id is refused, since it would rewrite both lines."""
    a = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  body="v1", author="ann")
    path = store.notes_path(tmp_path)
    line = path.read_text().splitlines()[0]
    path.write_text(line + "\n" + line.replace('"v1"', '"v2"') + "\n")
    assert [n.body for n in store.load(tmp_path)] == ["v1"]
    (lineno, _, err), = store.load_malformed(tmp_path)
    assert lineno == 2 and err.startswith(f"duplicate id {a.id}: line 1 has it too"), err
    before = path.read_text()
    with pytest.raises(ValueError, match=r"is on lines 1 and 2"):
        store.supersede(tmp_path, a.id, author="bob", append_body="more")
    assert path.read_text() == before


def test_a_kept_versions_id_carries_its_own_time(tmp_path, monkeypatch):
    """An id read as a date names the version it is on. The kept version's
    new id took the edit's time, so a reader took two versions' times for
    each other's (2026-10-13)."""
    from datetime import timezone
    clock = [datetime(2026, 10, 13, 4, 10, 30, 123456, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "_utcnow", lambda: clock[0])
    a = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  body="v1", author="ann")
    clock[0] = datetime(2026, 10, 13, 4, 10, 32, 654321, tzinfo=timezone.utc)
    b = store.supersede(tmp_path, a.id, author="bob", append_body="more")
    assert (b.id, b.supersedes[:15]) == (a.id, "20261013-041030")


def test_a_kept_version_is_not_a_pending_row(tmp_path):
    """The earlier version's line moved to a new id, and read as a row its
    author had written: `Rows: ann 1, bob 1` for bob's one edit."""
    import subprocess
    from types import SimpleNamespace
    from symbion import gitref
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    a = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  body="v1", author="ann")
    assert store.commit(tmp_path, "c", cfg)
    store.supersede(tmp_path, a.id, author="bob", body="v2")
    assert [r["author"] for r in store.pending_rows(tmp_path)] == ["bob"]
    assert gitref.uncommitted(tmp_path) == (1, False)
    assert not store.commit(tmp_path, "c2", cfg, keep=lambda r: r.get("author") == "ann")
    assert store.commit(tmp_path, "c3", cfg, keep=lambda r: r.get("author") == "bob")
    git = ["git", "-C", str(tmp_path)]
    assert "Rows: bob 1" in subprocess.run([*git, "log", "-1", "--format=%B"],
                                           capture_output=True, text=True).stdout
    assert subprocess.run([*git, "status", "--porcelain"], capture_output=True,
                          text=True).stdout == ""


def test_of_rows_written_in_one_second_the_later_write_lists_first(tmp_path, monkeypatch):
    """A stamp is to the second, and the id broke the tie. An edit keeps the
    row's id, so an edit of an older row sorted behind a row it followed."""
    monkeypatch.setattr(store, "_now_iso", lambda: "2026-10-11T00:00:00+00:00")
    project = {"type": "project", "name": None}
    a = store.add(tmp_path, kind="task", target=project, body="a", author="ann")
    b = store.add(tmp_path, kind="task", target=project, body="b", author="ann")
    store.supersede(tmp_path, a.id, author="bob", body="a2")
    assert [n.id for n in store.newest_first(store.heads(store.load(tmp_path)))] == [a.id, b.id]


def test_a_rename_sweep_keeps_every_rows_id(tmp_path):
    item = {"type": "item", "name": "old"}
    a = store.add(tmp_path, kind="task", target=item, body="a")
    b = store.add(tmp_path, kind="note", target=item, body="b")
    assert store.rename_target(tmp_path, "item", "old", "new", author="ann") == (2, 0)
    rows = store.load(tmp_path)
    assert len({n.id for n in rows}) == len(rows) == 4
    assert {h.id: h.target.name for h in store.heads(rows)} == {a.id: "new", b.id: "new"}


def test_a_store_is_written_lf_on_every_platform(tmp_path):
    """Text mode on Windows writes CRLF, and a store is shared by clones on
    every platform."""
    store.ensure_store(tmp_path)
    store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="a")
    store._replace_atomically(tmp_path / "x", "a\nb\n")
    for f in (store.notes_path(tmp_path), tmp_path / ".gitignore", tmp_path / "x"):
        assert b"\r" not in f.read_bytes(), f


def test_a_row_written_with_evidence_loads_and_drops_it(tmp_path):
    """`evidence` never had a writer, and no row in any store held a path:
    deleted 2026-10-09. Every row written before carries `"evidence": []`,
    and still loads; nothing writes the key again."""
    store.ensure_store(tmp_path)
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="x")
    path = store.notes_path(tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(row | {"evidence": ["a.log"]}) + "\n", encoding="utf-8")
    loaded = store.load(tmp_path)
    assert [x.id for x in loaded] == [n.id] and not store.load_malformed(tmp_path)
    assert "evidence" not in store.note_to_dict(loaded[0])


def test_an_edit_keeps_a_field_a_newer_symbion_wrote(tmp_path):
    """A key this symbion does not know is a newer one's field, and an edit
    writes it back: the head dropped it (2026-10-16), so an older install's
    resolve took a question's options off it (the user, 2026-10-19)."""
    n = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  body="x", author="ann")
    path = store.notes_path(tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(row | {"later": {"a": [1, None]}}) + "\n", encoding="utf-8")
    store.supersede(tmp_path, n.id, author="ann", tags=("t",))
    head, = store.heads(store.load(tmp_path))
    assert head.tags == ("t",) and store.note_to_dict(head).get("later") == {"a": [1, None]}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r.get("later") for r in rows] == [{"a": [1, None]}] * 2


def test_undo_takes_back_the_last_write_whole(tmp_path, monkeypatch):
    """One level: an add, an edit of a committed row, an edit of an
    uncommitted one, a batch and an arc each come back as they were,
    sessions file included (the user, 2026-10-01)."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    yes = lambda last, rows: True
    files = lambda: [(tmp_path / f).read_text() for f in ("notes.jsonl", "arcs.jsonl")]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    x = store.add(tmp_path, kind="task", target=project, body="v1", author="ann", status="open")
    assert store.commit(tmp_path, "c", cfg)
    with pytest.raises(store.UndoRefused, match="nothing to undo"):
        store.undo(tmp_path, x.id, yes)

    was = files()
    store.supersede(tmp_path, x.id, author="claude", status="resolved")
    u = store.undo(tmp_path, x.id, yes)
    assert (u["changed"], u["removed"]) == ([x.id], []) and [r["author"] for r in u["rows"]] == ["claude"]
    assert files() == was

    d = store.add(tmp_path, kind="note", target=project, body="draft1", author="claude")
    was, known = files(), store.sessions(tmp_path)
    store.supersede(tmp_path, d.id, author="claude", body="draft2")
    assert store.undo(tmp_path, d.id, yes)["changed"] == [d.id]
    assert files() == was and store.sessions(tmp_path) == known

    rows = store.add_many(tmp_path, [dict(kind="note", target=project, body=b, author="claude")
                                     for b in ("a", "b")])
    with pytest.raises(KeyError):           # refused under the lock: the level stands
        store.supersede(tmp_path, "no-such-entry", author="claude", body="x")
    assert store.undo(tmp_path, rows[1].id, yes)["removed"] == sorted(r.id for r in rows)
    assert files() == was and store.sessions(tmp_path) == known

    arc = store.create_arc(tmp_path, "Arc", "", "item", author="claude")
    assert store.undo(tmp_path, arc.id, yes)["arcs"] == [arc.id] and files() == was


@pytest.mark.parametrize("own", [False, True], ids=["everyone", "own rows"])
def test_a_refused_commit_leaves_the_undo_level(tmp_path, refusing_hook, own):
    """A secret scanner refuses the commit, and the secret is in the last
    write: `undo` takes it back. The commit dropped the level before git
    ran, so undo said nothing was written since the last commit
    (2026-10-26)."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    store.add(tmp_path, kind="note", target=project, body="kept", author="ann")
    assert store.commit(tmp_path, "c", cfg)
    x = store.add(tmp_path, kind="note", target=project, body="token=hunter2", author="ann")
    refusing_hook(tmp_path)
    with pytest.raises(RuntimeError, match="secret found"):
        store.commit(tmp_path, "refused", cfg, keep=store.own_rows(tmp_path, "ann") if own else None)
    assert store.undo(tmp_path, x.id, lambda last, rows: True)["removed"] == [x.id]
    assert "hunter2" not in store.notes_path(tmp_path).read_text(encoding="utf-8")


def test_a_commit_with_nothing_to_take_ends_the_undo_level(tmp_path):
    """A hand `git commit` took the last write: undo would take back a row
    git holds. `symbion commit` finds nothing to stage and ends the level,
    as a commit does."""
    import subprocess
    from types import SimpleNamespace
    store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="b",
              author="ann")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "by hand"], check=True)
    assert store.undoable(tmp_path), "control: the level outlives a hand commit"
    assert store.commit(tmp_path, "c", SimpleNamespace(git_name="t", git_email="t@t")) is False
    assert not store.undoable(tmp_path)


def test_undo_refuses_what_it_should_not_take_back(tmp_path):
    """It names the last write in each refusal, so the caller sees what they
    would have taken back."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    project = {"type": "project", "name": None}
    a = store.add(tmp_path, kind="note", target=project, body="a", author="ann")
    b = store.add(tmp_path, kind="note", target=project, body="b", author="bob")
    with pytest.raises(store.UndoRefused, match=f"did not touch {a.id}.*the add of {b.id} by bob"):
        store.undo(tmp_path, a.id, lambda last, rows: True)
    with pytest.raises(store.UndoRefused, match=f"not yours: it was the add of {b.id} by bob"):
        store.undo(tmp_path, b.id, lambda last, rows: False)
    with open(tmp_path / "notes.jsonl", "a") as f:         # a hand edit since
        f.write("\n")
    with pytest.raises(store.UndoRefused, match="changed after its last write, outside symbion"):
        store.undo(tmp_path, b.id, lambda last, rows: True)
    store.add(tmp_path, kind="note", target=project, body="c", author="ann")
    assert store.commit(tmp_path, "c", cfg)
    with pytest.raises(store.UndoRefused, match="nothing to undo"):
        store.undo(tmp_path, b.id, lambda last, rows: True)


def test_an_undo_preview_changes_nothing_and_names_each_earlier_version(tmp_path, monkeypatch):
    """The GUI's undo button asks first, showing what the undo changes: a
    preview (`apply=False`, no id) runs every check and writes nothing.
    `undoable` is the button's own read, every page and every few seconds:
    the caller's session's write, as that write left the files."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(git_name="t", git_email="t@t")
    yes = lambda last, rows: True
    files = lambda: [(tmp_path / f).read_text() for f in ("notes.jsonl", "arcs.jsonl")]
    x = store.add(tmp_path, kind="task", target={"type": "project", "name": None},
                  body="b", author="ann", status="open")
    assert store.commit(tmp_path, "c", cfg) and not store.undoable(tmp_path)
    store.supersede(tmp_path, x.id, author="ann", status="resolved")
    assert store.undoable(tmp_path)
    was = files()
    u = store.undo(tmp_path, None, yes, apply=False)
    assert files() == was and store.undoable(tmp_path)
    assert u["changed"] == [x.id] and u["before"][x.id]["status"] == "open"
    with pytest.raises(store.UndoRefused, match="not yours"):
        store.undo(tmp_path, None, lambda last, rows: False, apply=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert not store.undoable(tmp_path), "another session's write"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    with open(tmp_path / "notes.jsonl", "a") as f:
        f.write("\n")
    assert not store.undoable(tmp_path), "the files changed outside symbion"


def test_a_ref_of_a_type_the_store_lacks_is_refused_on_add(tmp_store):
    """The repos design: the store's type check covers an add's refs, so a
    name the api's rule missed fails loud instead of reading as nothing."""
    with pytest.raises(ValueError, match="unknown target type: 'file'"):
        store.add_many(tmp_store, [{"kind": "note", "target": {"type": "item", "name": "x"},
                                    "refs": [{"type": "file", "name": "a"}]}],
                       target_types={"item"})


def test_supersede_and_rename_check_new_names_and_never_inherited_ones(tmp_store):
    n = store.add_many(tmp_store, [{"kind": "note", "target": {"type": "item", "name": "x"},
                                    "refs": [{"type": "file", "name": "a"}]}])[0]
    store.supersede(tmp_store, n.id, author="t", body="edited", target_types={"item"})
    with pytest.raises(ValueError, match="unknown target type: 'file'"):
        store.supersede(tmp_store, n.id, author="t", add_refs=[{"type": "file", "name": "b"}],
                        target_types={"item"})
    with pytest.raises(ValueError, match="unknown target type: 'file'"):
        store.rename_target(tmp_store, "item", "x", "y", author="t", new_type="file",
                            target_types={"item"})


def _question(tmp_path, options, kind="question", **fields):
    return store.add(tmp_path, kind=kind, target={"type": "project", "name": None},
                     body="Ship it?", options=options, **fields)


def test_options_round_trip_in_order(tmp_path):
    """A question's answers are data, the default first (the user,
    2026-10-16): the order is the datum, so the list is kept as given,
    where measurements sort by name."""
    _question(tmp_path, ["ship", "hold", "ask"])
    store.add(tmp_path, kind="question", target={"type": "project", "name": None}, body="?")
    by_body = {n.body: n for n in store.load(tmp_path)}
    with_, without = by_body["Ship it?"], by_body["?"]
    assert with_.options == ("ship", "hold", "ask")
    assert store.note_to_dict(with_)["options"] == ["ship", "hold", "ask"]
    assert without.options is None and store.note_to_dict(without)["options"] is None


def test_options_are_refused_on_any_kind_but_question(tmp_path):
    kinds = K.read_kinds(tmp_path)
    with pytest.raises(ValueError, match="options on kind 'task': only a question takes answers"):
        store.check_fields("task", kinds["task"], {"options": ["a"]})
    store.check_fields("question", kinds["question"], {"options": ["a"]})
    with pytest.raises(ValueError, match="options on kind 'task'"):
        _question(tmp_path, ["a"], kind="task")


def test_a_kind_change_off_question_keeps_or_clears_its_options(tmp_path):
    """A supersede inherits the tip's fields, and --kind can move a question
    to a task (they share bits): the rule judges the row as written, so the
    move is refused unless the same write clears the list."""
    q = _question(tmp_path, ["ship", "hold"])
    with pytest.raises(ValueError, match="options on kind 'task'"):
        store.supersede(tmp_path, q.id, kind="task")
    moved = store.supersede(tmp_path, q.id, kind="task", options=None)
    assert (moved.kind, moved.options) == ("task", None)
    t = store.add(tmp_path, kind="task", target={"type": "item", "name": "t"}, body="t")
    back = store.supersede(tmp_path, t.id, kind="question", options=["a", "b"])
    assert back.options == ("a", "b")


def test_filter_grep_reads_options(tmp_path):
    q = _question(tmp_path, ["ship", "hold for a check"])
    found = store.query(store.load(tmp_path), grep=re.compile("hold"))
    assert [n.id for n in found] == [q.id]


def test_a_renamed_kind_drops_inherited_options_instead_of_refusing_every_write(tmp_path):
    """The reference renames a kind with a [kinds] edit and a sed over
    notes.jsonl. Done to `question`, every row with options then refused
    every write, resolve and rename included, and the GUI's buttons raised
    (review, 2026-10-17). An inherited field the kind cannot hold is dropped
    on the next write, as a legacy `checked` on a note is; a kind CHANGE in
    the same write still refuses."""
    (tmp_path / "symbion.toml").write_text('[kinds]\nask = { status = true }\n')
    _write_raw_legacy_row(tmp_path, kind="ask", status="open", options=["a", "b"])
    (old,) = store.load(tmp_path)
    assert store.unholdable(old) == ["options"]
    new = store.supersede(tmp_path, "legacy-1", tags=["t"])
    assert (new.options, new.tags) == (None, ("t",))


def test_symbion_now_stops_the_clock(monkeypatch):
    """The public demo serves a snapshot whose ages must read as on the day it
    was taken, however long it is served (the user, 2026-10-22)."""
    from datetime import timezone
    from symbion import summary as summ
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:00:00Z")    # far from any real today
    assert store._utcnow() == datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert summ.age("2026-01-08T12:00:00+00:00") == "2d"
    assert summ.age_days("2026-01-03T12:00:00+00:00") == 7
    assert summ.age("2026-01-11T00:00:00+00:00") == "now"      # newer than the clock


def test_symbion_now_refuses_a_time_without_an_offset(monkeypatch):
    """A clock that slipped to the real one would read `5w ago` with no error
    anywhere; a time with no offset would read as the host's local time."""
    from datetime import timezone
    for bad in ("2026-10-22T19:47:54", "yesterday"):
        monkeypatch.setenv("SYMBION_NOW", bad)
        with pytest.raises(store.BadClock, match="SYMBION_NOW"):
            store._utcnow()
    monkeypatch.setenv("SYMBION_NOW", "")
    assert abs((store._utcnow() - datetime.now(timezone.utc)).total_seconds()) < 5


def test_a_store_commit_is_stamped_by_symbions_clock_in_utc(tmp_path, monkeypatch):
    """The store's git log read local time beside rows stamped in UTC
    (2026-10-22). A stopped clock stamps the commit too."""
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:00:00Z")
    store.add(tmp_path, kind="note", target={"type": "project", "name": None},
              body="b", author="ann")
    assert store.commit(tmp_path, "c", SimpleNamespace(git_name="t", git_email="t@t"))
    dates = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%ai%n%ci"],
                           capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    assert dates == ["2026-01-10 12:00:00 +0000"] * 2, dates
