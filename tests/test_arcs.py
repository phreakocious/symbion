import json

import pytest
from symbion import store

pytestmark = pytest.mark.usefixtures("tmp_store")


def test_arc_from_dict_validates_scope_against_an_injected_set():
    d = {"id": "x", "name": "x", "target_scope": "item", "created_at": "2026-01-01T00:00:00"}
    assert store.arc_from_dict(d, legal_scopes={"item"}).target_scope == "item"
    with pytest.raises(ValueError):
        store.arc_from_dict(d | {"target_scope": "bogus"}, legal_scopes={"item"})


def test_create_arc_validates_scope_against_an_injected_set(tmp_path):
    ok = store.create_arc(tmp_path, "x", "", "item", legal_scopes={"item"})
    assert ok.target_scope == "item"
    with pytest.raises(ValueError):
        store.create_arc(tmp_path, "y", "", "bogus", legal_scopes={"item"})


def test_seed_creates_n_then_zero(tmp_path):
    """Both directions. 'second seed creates nothing' alone passes on a
    seed_arc that always creates nothing; the N>0 assertion carries it."""
    act = store.create_arc(tmp_path, "Add assertions", "", "item")
    first = store.seed_arc(tmp_path, act.id, "item", ["a", "b", "c"])
    assert len(first) == 3
    second = store.seed_arc(tmp_path, act.id, "item", ["a", "b", "c"])
    assert len(second) == 0


def test_progress_counts_a_ticked_item_once(tmp_path):
    act = store.create_arc(tmp_path, "x", "", "item")
    made = store.seed_arc(tmp_path, act.id, "item", ["a", "b"])
    store.supersede(tmp_path, made[0].id, status="resolved")
    assert store.arc_progress(store.load(tmp_path), act.id) == (1, 2)


def test_rename_keeps_the_id_so_checkboxes_still_resolve(tmp_path):
    act = store.create_arc(tmp_path, "old", "", "item")
    store.seed_arc(tmp_path, act.id, "item", ["a"])
    renamed = store.rename_arc(tmp_path, act.id, "new")
    assert renamed.name == "new"
    assert store.arc_progress(store.load(tmp_path), act.id) == (0, 1)


def test_slugify_bounds_the_arc_id_length(tmp_path):
    """The last hole in the width bound NAME_CHARS was added to close --
    the arc id (slug) renders straight into the summary, uncapped."""
    act = store.create_arc(tmp_path, "n" * 500, "", "item")
    assert len(act.id) <= 48


def test_slugify_bounds_hold_past_the_collision_suffix(tmp_path):
    """`_slugify` caps the BASE at 48, but create_arc appends `-2`,
    `-3`... past it -- measured len(id) == 50 on the second same-name
    arc when only the base is capped. The bound must hold on the
    FINAL id, so this creates three same-named (max-length) arcs and
    checks every one, not just the first."""
    acts = [store.create_arc(tmp_path, "n" * 500, "", "item") for _ in range(3)]
    ids = [a.id for a in acts]
    assert len(set(ids)) == 3, "ids collided"
    for id_ in ids:
        assert len(id_) <= 48, f"{id_!r} exceeds the 48-char bound"


def test_mint_unique_retries_until_distinct(tmp_path):
    """A scripted id source, not a pinned clock.

    Pinning _clock/_rand does not work: _mint_unique calls new_id() with no
    arguments so they are unreachable, and if they were, the retry loop would
    spin forever. Here the source collides on the first attempt of each of the
    first 5 names, then advances.

    The call-count assertion is the one that carries the test: without it this
    passes on an implementation with no retry loop at all."""
    calls = []

    def gen():
        i = len(calls)
        calls.append(i)
        # first attempt for each of the first 5 names duplicates id "dup-0"
        if i < 10 and i % 2 == 0:
            return "dup-0"
        return f"id-{i}"

    act = store.create_arc(tmp_path, "x", "", "item")
    made = store.seed_arc(tmp_path, act.id, "item",
                               [f"n{i}" for i in range(200)], _gen=gen)
    ids = [n.id for n in made]
    assert len(set(ids)) == 200, "ids collided"
    # 200 names + exactly 4 retries (names 2-5 each collide once; name 0 takes
    # "dup-0" uncontested and name 1 draws an odd index).
    assert len(calls) == 204, f"expected 204 generator calls, got {len(calls)}"


def _task(store_dir, aid, name, kind="task"):
    return store.add(store_dir, kind=kind, target={"type": "item", "name": name},
                     status="open", arc_id=aid)


def test_several_items_on_one_target_all_stay_on_the_checklist(tmp_path):
    """arc_items used to collapse by (target_type, target_name), so three
    tickets on one file read back as ONE in `todo` and in the count while
    `list --arc` showed all three -- two reads of one campaign disagreeing,
    silently. heads() already collapses real supersede chains, which is the only
    thing the collapse was needed for."""
    act = store.create_arc(tmp_path, "C", "", "item")
    for body in ("first", "second", "third"):
        store.add(tmp_path, kind="task", target={"type": "item", "name": "x.h"},
                  status="open", arc_id=act.id, body=body)
    items = store.arc_items(store.load(tmp_path), act.id)
    assert len(items) == 3, "collapsed by target again"
    assert store.arc_progress(store.load(tmp_path), act.id) == (0, 3)

    store.supersede(tmp_path, items[0].id, status="resolved")
    assert store.arc_progress(store.load(tmp_path), act.id) == (1, 3), \
        "a superseded item still collapses to one head, and ticks exactly one box"


def test_a_bug_in_an_arc_is_a_checklist_item(tmp_path):
    """Both stateful kinds count. A bug carrying an arc_id used to
    show up in `list --arc` but in neither `todo` nor the progress count,
    so the campaign's own two reads disagreed by the number of bugs."""
    act = store.create_arc(tmp_path, "C", "", "item")
    _task(tmp_path, act.id, "a")
    anom = _task(tmp_path, act.id, "broken", kind="bug")
    assert store.arc_progress(store.load(tmp_path), act.id) == (0, 2)

    store.supersede(tmp_path, anom.id, status="resolved")
    assert store.arc_progress(store.load(tmp_path), act.id) == (1, 2)


def test_a_statusless_kind_in_an_arc_is_not_a_checklist_item(tmp_path):
    """The other direction of the STATEFUL_KINDS filter: a decision or note
    tagged into a campaign is context, not a box. Filtering on arc_id
    alone would silently make every one of them an unclosable open item."""
    act = store.create_arc(tmp_path, "C", "", "item")
    _task(tmp_path, act.id, "a")
    for kind in ("decision", "note", "check"):
        store.add(tmp_path, kind=kind, target={"type": "item", "name": f"ctx-{kind}"},
                  arc_id=act.id)
    assert store.arc_progress(store.load(tmp_path), act.id) == (0, 1)


def test_an_idea_is_not_a_checklist_box_but_a_question_is(tmp_path):
    """Both directions on one arc: progress reads 0/1 and the items are the
    question only. Testing the idea's absence alone would pass on an
    arc_items that returns nothing."""
    act = store.create_arc(tmp_path, "x", "", "item")
    store.add(tmp_path, kind="idea", target={"type": "item", "name": "i"}, arc_id=act.id)
    q = store.add(tmp_path, kind="question", target={"type": "item", "name": "q"}, arc_id=act.id)
    notes = store.load(tmp_path)
    assert [n.id for n in store.arc_items(notes, act.id)] == [q.id]
    assert store.arc_progress(notes, act.id) == (0, 1)


def _declare(store_dir, text):
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(text)


def test_seed_mints_the_kind_it_is_given(tmp_path):
    _declare(tmp_path, '[kinds]\nfollowup = { status = true }\ntask = { status = true }\n')
    act = store.create_arc(tmp_path, "x", "", "item")
    made = store.seed_arc(tmp_path, act.id, "item", ["a"], kind="followup")
    assert [n.kind for n in made] == ["followup"]
    assert store.seed_arc(tmp_path, act.id, "item", ["b"])[0].kind == "task"   # the default


def test_seed_refuses_an_undeclared_kind_naming_the_table(tmp_path):
    _declare(tmp_path, '[kinds]\nfollowup = { status = true }\n')
    act = store.create_arc(tmp_path, "x", "", "item")
    with pytest.raises(ValueError, match=r"no kind 'task'.*\[kinds\]"):
        store.seed_arc(tmp_path, act.id, "item", ["a"])


@pytest.mark.parametrize("label, table, bit", [
    ("fact", "fact = {}", "status"),
    ("shelf", "shelf = { status = true, parked = true }", "parked"),
    ("prediction", "prediction = { status = true, verdict = true }", "verdict"),
])
def test_seed_refuses_an_ineligible_kind_naming_the_bit(tmp_path, label, table, bit):
    """A seeded row is a checklist box that is nothing but its name: it needs
    status, and neither parked nor verdict (seed never stamps provenance, so
    a seeded prediction would have no commitment sha and no falsifier)."""
    _declare(tmp_path, f"[kinds]\n{table}\n")
    act = store.create_arc(tmp_path, "x", "", "item")
    with pytest.raises(ValueError, match=bit):
        store.seed_arc(tmp_path, act.id, "item", ["a"], kind=label)
    assert store.arc_items(store.load(tmp_path), act.id) == []


def test_archiving_an_archived_arc_keeps_its_date(tmp_path):
    """`archived_at` is when the arc left the summary. A second archive
    re-stamped it, at exit 0 (the 2026-10-09 audit)."""
    from datetime import datetime, timezone
    store.ensure_store(tmp_path)
    a = store.create_arc(tmp_path, "x", "", "item")
    at = lambda m: lambda: datetime(2026, m, 1, tzinfo=timezone.utc)       # noqa: E731
    first = store.archive_arc(tmp_path, a.id, _clock=at(1))
    again = store.archive_arc(tmp_path, a.id, _clock=at(2))
    assert again == first == store.load_arcs(tmp_path)[0]
    assert first.archived_at.startswith("2026-01-01"), first


def test_an_arc_edit_keeps_a_field_a_newer_symbion_wrote(tmp_path):
    """Any arc's edit writes every arc line again, so a field a newer symbion
    added to one arc went from it at another's rename (the user,
    2026-10-19)."""
    a = store.create_arc(tmp_path, "a", "", "item")
    b = store.create_arc(tmp_path, "b", "", "item")
    path = store.arcs_path(tmp_path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    path.write_text("".join(json.dumps(r | ({"later": "x"} if r["id"] == a.id else {})) + "\n"
                            for r in rows), encoding="utf-8")
    store.rename_arc(tmp_path, b.id, "b2")
    rows = {r["id"]: r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())}
    assert rows[a.id].get("later") == "x" and rows[b.id]["name"] == "b2"


# ---- phases, placement and the derived board ----
def _board_arc(tmp_path, names, phases=None, placement=None):
    act = store.create_arc(tmp_path, "board", "", "item")
    made = store.seed_arc(tmp_path, act.id, "item", names)
    act = store._edit_arc(tmp_path, act.id, lambda a: store.replace(
        a, phases=phases, placement=placement))
    return act, {n.target.name: n.id for n in made}


def _cols(tmp_path, act):
    return {name: [n.target.name for n in rows]
            for name, rows in store.arc_board(store.load(tmp_path), act)}


def test_an_arc_without_phases_keeps_its_line_byte_for_byte(tmp_path):
    act = store.create_arc(tmp_path, "a", "", "item")
    store.rename_arc(tmp_path, act.id, "b")
    store.rename_arc(tmp_path, act.id, "a")
    row = json.loads(store.arcs_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert "phases" not in row and "placement" not in row


def test_phases_and_placement_round_trip():
    d = {"id": "x", "name": "x", "description": "", "target_scope": "item",
         "created_at": "2026-01-01T00:00:00", "author": "user", "archived": False,
         "archived_at": None, "phases": ["next", "doing"],
         "placement": [{"id": "e1", "phase": "doing", "by": "user",
                        "at": "2026-01-01T00:00:00+00:00"}]}
    act = store.arc_from_dict(d)
    assert act.phases == ("next", "doing") and act.extra is None
    assert store.arc_to_dict(act) == d


def test_unplaced_boxes_fall_back_to_write_order(tmp_path):
    act, _ = _board_arc(tmp_path, ["zeta", "alpha", "mid"])
    assert _cols(tmp_path, act)[None] == ["zeta", "alpha", "mid"]


def test_a_resolved_box_shows_in_done_and_keeps_its_placement(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b", "c"], phases=("next", "doing"))
    act = store._edit_arc(tmp_path, act.id, lambda a: store.replace(a, placement=(
        {"id": ids["c"], "phase": "doing"}, {"id": ids["b"], "phase": "doing"})))
    head = store.supersede(tmp_path, ids["b"], status="resolved")
    cols = _cols(tmp_path, act)
    assert cols["doing"] == ["c"] and cols[store.DONE] == ["b"] and cols["next"] == ["a"]
    assert any(p["id"] == ids["b"] for p in act.placement)
    store.supersede(tmp_path, head.id, status="open")
    assert _cols(tmp_path, act)["doing"] == ["c", "b"]


def test_a_null_phase_reads_as_the_first_phase(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b", "c"])
    act = store._edit_arc(tmp_path, act.id, lambda a: store.replace(
        a, phases=("next", "doing"), placement=(
            {"id": ids["b"], "phase": None}, {"id": ids["a"], "phase": None})))
    assert _cols(tmp_path, act) == {"next": ["b", "a", "c"], "doing": [], store.DONE: []}


def test_a_stale_phase_lands_at_the_end_of_the_first_phase(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b", "c"], phases=("next", "doing"))
    act = store._edit_arc(tmp_path, act.id, lambda a: store.replace(a, placement=(
        {"id": ids["c"], "phase": "gone"}, {"id": ids["b"], "phase": "next"})))
    assert _cols(tmp_path, act)["next"] == ["b", "a", "c"]


def test_a_box_placed_twice_reads_its_first_placement(tmp_path):
    """arc_board reads one {id: phase} map, not a scan per box (audit,
    2026-11-08); the scan took the first match, and so does the map."""
    act, ids = _board_arc(tmp_path, ["a", "b"], phases=("next", "doing"))
    act = store._edit_arc(tmp_path, act.id, lambda a: store.replace(a, placement=(
        {"id": ids["b"], "phase": "doing"}, {"id": ids["b"], "phase": "next"})))
    assert _cols(tmp_path, act)["doing"] == ["b"]


def test_a_placement_for_an_entry_off_the_arc_is_ignored(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b"], phases=("next", "doing"))
    act = store._edit_arc(tmp_path, act.id, lambda a: store.replace(a, placement=(
        {"id": ids["b"], "phase": "doing"},)))
    other = store.create_arc(tmp_path, "other", "", "item")
    head = store.supersede(tmp_path, ids["b"], arc_id=other.id)
    cols = _cols(tmp_path, act)
    assert all("b" not in v for v in cols.values())
    store.supersede(tmp_path, head.id, arc_id=act.id)
    assert _cols(tmp_path, act)["doing"] == ["b"]


def test_parked_entries_are_never_on_the_board(tmp_path):
    act, _ = _board_arc(tmp_path, ["a"])
    n = store.load(tmp_path)[0]
    idea = store.replace(n, id="idea-1", kind="idea", target="side",
                         spec=store.K.DEFAULT_KINDS["idea"])
    cols = store.arc_board([n, idea], act)
    assert [x.target.name for _, rows in cols for x in rows] == ["a"]


# ---- the three writes ----
def _phased(tmp_path, names, phases=("next", "doing")):
    act, ids = _board_arc(tmp_path, names)
    act, _ = store.set_phases(tmp_path, act.id, list(phases))
    return act, ids


@pytest.mark.parametrize("bad", [["done"], ["Done"], ["DONE"], [" next"], [""], ["next", "next"], ["a,b"]])
def test_set_phases_refuses_bad_names(tmp_path, bad):
    act, _ = _board_arc(tmp_path, ["a"])
    with pytest.raises(ValueError, match="reserved|empty or has|twice|comma"):
        store.set_phases(tmp_path, act.id, bad)
    assert store.load_arcs(tmp_path)[0].phases is None


def test_set_phases_keeps_inner_spaces_and_non_ascii(tmp_path):
    act, _ = _board_arc(tmp_path, ["a"])
    store.set_phases(tmp_path, act.id, ["next step", "révision"])
    assert store.load_arcs(tmp_path)[0].phases == ("next step", "révision")


def test_set_phases_refuses_to_drop_a_phase_holding_an_open_box(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b"])
    store.move_box(tmp_path, ids["b"], phase="doing", author="user")
    before = store.load_arcs(tmp_path)
    with pytest.raises(ValueError, match=f"phase 'doing' holds open boxes: {ids['b'][-10:]}"):
        store.set_phases(tmp_path, act.id, ["next"])
    assert store.load_arcs(tmp_path) == before


def test_clearing_phases_of_an_arc_without_any_writes_nothing(tmp_path):
    act, _ = _board_arc(tmp_path, ["a", "b"])
    raw = store.arcs_path(tmp_path).read_bytes()
    assert store.set_phases(tmp_path, act.id, []) == (act, 0)
    assert store.arcs_path(tmp_path).read_bytes() == raw


def test_set_phases_drops_resolved_placements_and_counts_them(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b", "c"], phases=("next", "doing", "later"))
    store.move_box(tmp_path, ids["b"], phase="later", author="user")
    store.move_box(tmp_path, ids["c"], phase="doing", author="user")
    store.supersede(tmp_path, ids["b"], status="resolved")
    store.supersede(tmp_path, ids["c"], status="resolved")
    act, dropped = store.set_phases(tmp_path, act.id, ["next", "doing"])
    held = {p["id"] for p in act.placement}
    assert dropped == 1 and ids["b"] not in held and ids["c"] in held


def test_clearing_phases_keeps_the_order(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b", "c"])
    store.move_box(tmp_path, ids["c"], phase="next", author="user")
    store.move_box(tmp_path, ids["b"], phase="doing", author="user")
    store.move_box(tmp_path, ids["a"], phase="doing", author="user", top=True)
    store.move_box(tmp_path, ids["b"], phase="doing", author="user", top=True)
    act = store.load_arcs(tmp_path)[0]
    assert _cols(tmp_path, act)["doing"] == ["b", "a"]
    act, _ = store.set_phases(tmp_path, act.id, [])
    assert act.phases is None and _cols(tmp_path, act)[None] == ["c", "b", "a"]


def test_clearing_phases_keeps_unplaced_boxes_in_order(tmp_path):
    act, ids = _phased(tmp_path, ["u", "p"])
    store.move_box(tmp_path, ids["p"], phase="doing", author="user")
    act = store.load_arcs(tmp_path)[0]
    assert _cols(tmp_path, act)["next"] == ["u"]
    act, _ = store.set_phases(tmp_path, act.id, [])
    assert _cols(tmp_path, act)[None] == ["u", "p"]
    assert {"id": ids["u"], "phase": None} in act.placement


def test_move_before_an_unplaced_box_lands_there(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b", "c", "d"])
    act = store.move_box(tmp_path, ids["d"], before=ids["c"], author="user")
    assert _cols(tmp_path, act)[None] == ["a", "b", "d", "c"]
    by_id = {p["id"]: p for p in act.placement}
    assert "by" not in by_id[ids["a"]] and "at" not in by_id[ids["b"]]
    assert by_id[ids["d"]]["by"] == "user" and by_id[ids["d"]]["at"]


def test_a_later_move_keeps_an_earlier_movers_by_and_at(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b", "c", "d"])
    act = store.move_box(tmp_path, ids["b"], top=True, author="alice")
    at = {p["id"]: p for p in act.placement}[ids["b"]]["at"]
    store.set_phases(tmp_path, act.id, ["next", "doing"])
    act = store.move_box(tmp_path, ids["d"], after=ids["c"], author="bob")
    b = {p["id"]: p for p in act.placement}[ids["b"]]
    assert (b["by"], b["at"], b["phase"]) == ("alice", at, "next")


def test_a_move_across_phases_keeps_the_phase_it_left(tmp_path):
    """The board card says where a box came from; a reorder, or an arc
    without phases, has nothing to say."""
    act, ids = _board_arc(tmp_path, ["a", "b", "c"])
    act = store.move_box(tmp_path, ids["a"], top=True, author="user")
    assert "from" not in {p["id"]: p for p in act.placement}[ids["a"]]
    store.set_phases(tmp_path, act.id, ["next", "doing"])
    act = store.move_box(tmp_path, ids["b"], phase="doing", author="user")
    assert {p["id"]: p for p in act.placement}[ids["b"]]["from"] == "next"
    act = store.move_box(tmp_path, ids["c"], phase="doing", before=ids["b"], author="user")
    act = store.move_box(tmp_path, ids["b"], before=ids["c"], author="user")
    assert "from" not in {p["id"]: p for p in act.placement}[ids["b"]]


def test_a_move_writes_placements_only_up_to_the_target(tmp_path):
    act, ids = _board_arc(tmp_path, [f"n{i}" for i in range(200)])
    col = [n.id for n in store.arc_board(store.load(tmp_path), act)[0][1]]
    act = store.move_box(tmp_path, col[-1], before=col[3], author="user")
    assert len(act.placement) == 4


def test_move_to_the_end_of_a_phase_lands_after_unplaced_boxes(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b", "c"])
    act = store.move_box(tmp_path, ids["a"], author="user")
    assert _cols(tmp_path, act)["next"] == ["b", "c", "a"]
    act = store.move_box(tmp_path, ids["b"], author="user", after=ids["c"])
    assert _cols(tmp_path, act)["next"] == ["c", "b", "a"]


def test_move_refusals(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b", "c"])
    mv = lambda e, **kw: store.move_box(tmp_path, e, author="user", **kw)
    plain = store.create_arc(tmp_path, "plain", "", "item")
    e = store.add(tmp_path, kind="task", target={"type": "item", "name": "p"},
                  status="open", arc_id=plain.id, body="p")
    with pytest.raises(ValueError, match="arc phases"):
        mv(e.id, phase="x")
    with pytest.raises(ValueError, match="in no arc"):
        mv(store.add(tmp_path, kind="task", target={"type": "item", "name": "q"},
                     status="open", body="q").id)
    with pytest.raises(ValueError, match="is an idea, which is never a box"):
        mv(store.add(tmp_path, kind="idea", target={"type": "item", "name": "i"},
                     arc_id=act.id, body="i").id)
    with pytest.raises(ValueError, match="never a box"):
        mv(store.add(tmp_path, kind="note", target={"type": "item", "name": "n"},
                     arc_id=act.id, body="n").id)
    with pytest.raises(ValueError, match="no phase 'gone'.*next, doing"):
        mv(ids["a"], phase="gone")
    for d in ("done", "Done"):
        with pytest.raises(ValueError, match="resolve"):
            mv(ids["a"], phase=d)
    store.supersede(tmp_path, ids["c"], status="resolved")
    with pytest.raises(ValueError, match="supersede"):
        mv(ids["c"])
    store.move_box(tmp_path, ids["b"], phase="doing", author="user")
    with pytest.raises(ValueError, match="not an open box"):
        mv(ids["a"], before=ids["b"])
    with pytest.raises(ValueError, match="not an open box.*`arc todo "):
        mv(ids["a"], after=ids["a"])
    with pytest.raises(ValueError, match="one of"):
        mv(ids["a"], top=True, before=ids["b"], phase="doing")
    with pytest.raises(KeyError):
        mv("nope")
    store.archive_arc(tmp_path, act.id)
    with pytest.raises(ValueError, match="arc unarchive"):
        mv(ids["a"])


def test_undo_takes_back_a_move_by_the_arc_id(tmp_path):
    act, ids = _board_arc(tmp_path, ["a", "b"])
    store.move_box(tmp_path, ids["a"], author="user")
    old = store.load_arcs(tmp_path)[0].placement
    store.move_box(tmp_path, ids["b"], top=True, author="user")
    assert store.load_arcs(tmp_path)[0].placement != old
    store.undo(tmp_path, act.id, lambda *a: True)
    assert store.load_arcs(tmp_path)[0].placement == old


def test_reopen_and_place_is_one_write(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b"])
    store.supersede(tmp_path, ids["b"], status="resolved")
    arcs = store.load_arcs(tmp_path)
    note, new = store.reopen_and_place(tmp_path, ids["b"], phase="doing", author="user")
    assert note.status == "open" and _cols(tmp_path, new)["doing"] == ["b"]
    store.undo(tmp_path, None, lambda *a: True)
    assert next(n for n in store.load(tmp_path) if n.id == ids["b"]).status == "resolved"
    assert store.load_arcs(tmp_path) == arcs


def test_a_refused_reopen_writes_nothing(tmp_path):
    act, ids = _phased(tmp_path, ["a", "b"])
    store.move_box(tmp_path, ids["a"], phase="doing", author="user")
    store.supersede(tmp_path, ids["b"], status="resolved")
    with pytest.raises(ValueError, match="not an open box"):
        store.reopen_and_place(tmp_path, ids["b"], phase="next", before=ids["a"], author="user")
    assert next(n for n in store.load(tmp_path) if n.id == ids["b"]).status == "resolved"
    with pytest.raises(ValueError, match="open already"):
        store.reopen_and_place(tmp_path, ids["a"], author="user")


def test_a_malformed_placement_reads_as_a_malformed_arc_line(tmp_path):
    act, _ = _board_arc(tmp_path, ["a"])
    other = store.create_arc(tmp_path, "other", "", "item")
    bad = json.dumps(dict(store.arc_to_dict(act), id="bad", placement=[{"phase": "x"}]))
    with store.arcs_path(tmp_path).open("a", encoding="utf-8") as f:
        f.write(bad + "\n")
    assert [m[0] for m in store.load_arcs_malformed(tmp_path)] == [3]
    store.rename_arc(tmp_path, other.id, "renamed")
    assert bad in store.arcs_path(tmp_path).read_text(encoding="utf-8")
    assert [a.id for a in store.load_arcs(tmp_path)] == [act.id, other.id]


def test_an_arcs_last_change_is_its_newest_entry_version_or_move(repo, tmp_path, monkeypatch):
    """Arcs sorts and labels arcs by it (2026-11-08): the latest version of any
    entry filed in the arc, or a move on its board, else the arc's creation.
    An entry in another arc does not count, and neither does a phase edit:
    the arc's row is rewritten with no time."""
    from datetime import datetime
    from symbion import api
    store.ensure_store(tmp_path)
    ctx = api.resolve(str(tmp_path))
    at = lambda hh: monkeypatch.setenv("SYMBION_NOW", f"2026-01-10T{hh}:00:00+00:00")
    def last():
        act = next(a for a in store.load_arcs(tmp_path) if a.id == "a")
        stamp, by = store.arc_last_change(store.load(tmp_path), act)
        return datetime.fromisoformat(stamp).hour, by
    task = lambda arc, who, name: api.add(ctx, {"kind": "task", "target": {"type": "item", "name": name},
                                          "arc_id": arc}, author=who)
    at("01"); api.create_arc(ctx, "a", "d", "item", author="ada")
    api.create_arc(ctx, "b", "d", "item", author="ada")
    assert last() == (1, "ada")
    at("02"); one, two = task("a", "bo", "x1"), task("a", "bo", "x2")
    assert last() == (2, "bo")
    at("03"); task("b", "cy", "x3")
    assert last() == (2, "bo")
    at("04"); api.supersede(ctx, one.id, author="cy", status="resolved")
    assert last() == (4, "cy")
    at("05"); api.set_phases(ctx, "a", ["todo", "doing"], author="user")
    assert last() == (4, "cy")
    at("06"); api.move_box(ctx, two.id, author="dee", phase="doing")
    assert last() == (6, "dee")
