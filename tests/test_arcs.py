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
    the arc id (slug) renders straight into the summary, unbounded."""
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
