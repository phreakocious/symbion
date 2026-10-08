import json
import subprocess

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from types import SimpleNamespace

import pytest

from symbion import store, summary
from symbion.config import Config

pytestmark = pytest.mark.usefixtures("tmp_store")


def _run(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git repo with two real commits -- for --commit/--branch, a
    hand-built sha string proves nothing about canonical_commit's actual
    rev-parse behavior or branch_commits' actual rev-list output."""
    r = tmp_path / "p"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    for i in range(2):
        (r / f"f{i}").write_text("x")
        _run(r, "add", "-A")
        _run(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"c{i}")
    return r


def test_flatten_collapses_newlines_and_tabs():
    """A 200-char body can be 200 newlines, so truncation alone does not bound
    the output. Flatten first."""
    assert summary.flatten("a\nb\tc\rd") == "a b c d"


def test_body_is_flattened_before_truncation(tmp_path):
    cfg = Config(project_root=tmp_path)
    n = store.add(tmp_path, kind="bug", target={"type": "project", "name": None},
                  body="x\n" * 500, tags=["priority"])
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    assert text.count("\n") < 30, "line cap bypassed by embedded newlines"


def test_counts_stay_exact_when_the_listing_is_capped(tmp_path):
    cfg = Config(project_root=tmp_path)
    for i in range(25):
        store.add(tmp_path, kind="bug", target={"type": "item", "name": f"a{i}"},
                  tags=["priority"])
    data = summary.summary(tmp_path, cfg)
    assert data["open"]["bug"] == 25, "the cap must hide rows, never change a count"
    assert len(data["priority"]) == 10
    assert data["priority_elided"] == 15


def test_full_lifts_the_caps(tmp_path):
    cfg = Config(project_root=tmp_path)
    for i in range(25):
        store.add(tmp_path, kind="bug", target={"type": "item", "name": f"a{i}"},
                  tags=["priority"])
    data = summary.summary(tmp_path, cfg, full=True)
    assert len(data["priority"]) == 25


def test_a_null_status_row_leaves_the_open_count_and_is_reported(tmp_path):
    """store.add() normalizes status=None to "open" before it ever reaches
    disk, so a note minted through add() can never carry a null status --
    reaching this means writing the row the way a hand-written migration
    does: straight into notes.jsonl, bypassing add(). A sibling row with an
    explicit "resolved" status must NOT be counted, or the implementation
    could pass this by counting every bug regardless of status.

    The open count drops it AND the summary names it: hidden from the
    headline, never hidden from the reader."""
    cfg = Config(project_root=tmp_path)
    store.ensure_store(tmp_path)

    def raw(note_id, status):
        return json.dumps({
            "id": note_id, "kind": "bug",
            "target": {"type": "project", "name": None},
            "created_at": "2020-01-01T00:00:00", "author": "claude",
            "body": "", "status": status,
        }) + "\n"

    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(raw("legacy-null", None))
        f.write(raw("legacy-resolved", "resolved"))
    d = summary.summary(tmp_path, cfg)
    assert d["open"]["bug"] == 0, "a guessed 'open' inflates the headline count"
    assert d["no_status"] == 1
    # The count alone was permanent furniture: no command lists these rows,
    # so nothing consumed the line. Name the first id and the verb that
    # fixes it, as the unreadable line does.
    assert d["no_status_first"] == "legacy-null"
    text = summary.render_summary(d)
    assert "no status" in text
    assert "legacy-null" in text and "supersede" in text


def test_no_status_is_silent_when_every_row_carries_one(tmp_path):
    """The other direction: the line must not appear on a healthy store, or
    it becomes noise every session and stops being read."""
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="bug", target={"type": "project", "name": None}, body="x")
    d = summary.summary(tmp_path, cfg)
    assert d["no_status"] == 0
    assert d["no_status_first"] is None
    assert "no status" not in summary.render_summary(d)


def test_priority_surfaces_a_starred_decision(tmp_path):
    """Filtering priority by `read_status(n) == "open"` is None for
    note/decision/check, so a starred decision could never surface --
    despite SKILL.md presenting `supersede <id> --add-tag priority` as how a
    human stars ANY note, decisions included, and the spec parking decisions
    out of the summary specifically because the user can ask to pull one."""
    cfg = Config(project_root=tmp_path)
    d = store.add(tmp_path, kind="decision", target={"type": "project", "name": None},
                  body="chose x")
    store.supersede(tmp_path, d.id, tags=["priority"])
    data = summary.summary(tmp_path, cfg)
    assert len(data["priority"]) == 1
    assert data["priority"][0]["kind"] == "decision"


def test_priority_excludes_a_resolved_starred_bug(tmp_path):
    """The other direction: closing a starred item must still drop it."""
    cfg = Config(project_root=tmp_path)
    a = store.add(tmp_path, kind="bug", target={"type": "project", "name": None},
                  tags=["priority"])
    store.supersede(tmp_path, a.id, status="resolved")
    data = summary.summary(tmp_path, cfg)
    assert len(data["priority"]) == 0


def test_standalone_tasks_exclude_arc_members(tmp_path):
    cfg = Config(project_root=tmp_path)
    act = store.create_arc(tmp_path, "x", "", "item")
    store.seed_arc(tmp_path, act.id, "item", ["in-arc"])
    store.add(tmp_path, kind="task", target={"type": "item", "name": "standalone"})
    assert summary.summary(tmp_path, cfg)["open"]["task"] == 1


def test_ceiling_holds_for_a_large_store(tmp_path):
    """This fires at every session start, so the ceiling has to hold under
    real load, not just for a handful of rows. 50 arcs and 200
    tagged-priority bugs (names/bodies carrying newlines) together must
    still render under it, with exact counts behind the capped listings --
    the cap hides rows, it never changes a number.

    Raised 30 -> 35 on 2026-09-11 when the open heads moved in here. The
    startup output still SHRANK: the hook stopped printing `symbion schema`
    the same day, which was ~14 lines, against at most 9 added here.
    Raised 35 -> 45 on 2026-09-24 for the pre-registration block: only a store
    that declares a status+verdict kind pays it. 45 -> 56 the same day for the
    due block, paid only while a row is past due or due within DUE_DAYS."""
    cfg = Config(project_root=tmp_path)
    _declare(tmp_path, '[kinds]\nbug = { status = true }\ntask = { status = true }\n'
                       'prediction = { status = true, verdict = true }\n')
    for i in range(30):
        store.add(tmp_path, kind="prediction", target={"type": "item", "name": f"p{i}"},
                  checked="x", body=f"line1\nline2 {i}")
    for i in range(15):
        store.add(tmp_path, kind="task", target={"type": "item", "name": f"late{i}"},
                  due="2000-01-01", body=f"line1\nline2 {i}")
    for i in range(50):
        act = store.create_arc(tmp_path, f"act-{i}\nwith\nnewlines", "", "item")
        store.seed_arc(tmp_path, act.id, "item", [f"x{i}"])
    for i in range(200):
        store.add(tmp_path, kind="bug", target={"type": "item", "name": f"a{i}"},
                  tags=["priority"] if i % 2 else [], body=f"line1\nline2\nline3 body {i}")
    data = summary.summary(tmp_path, cfg)
    text = summary.render_summary(data)
    assert text.count("\n") + 1 <= 56, "ceiling must hold at scale, not just for a small store"
    assert data["open"] == {"bug": 200, "task": 15, "prediction": 30}
    assert (len(data["due"]), data["due_elided"]) == (summary.DUE_CAP, 15 - summary.DUE_CAP)
    assert len(data["priority"]) == 10
    assert data["priority_elided"] == 90
    assert len(data["arcs"]) == 10
    assert data["arcs_elided"] == 40
    assert len(data["heads"]) == summary.PREREG_CAP + summary.HEAD_CAP
    assert data["heads_elided"] == (100 - summary.HEAD_CAP) + (30 - summary.PREREG_CAP)


def test_name_and_target_are_truncated_not_just_flattened(tmp_path):
    """flatten() bounds newlines, not width: a control-char-free 10,000-char
    name sails through flatten() unchanged and renders one line 10,000+
    characters wide -- the line-COUNT ceiling holds while the output is
    unusable. Both the arc name and the freeform item target name must
    be truncated after flatten(), the same as body.

    The arc name is 10,000 chars but mostly SPACES (not control
    characters, so flatten() alone would not shorten it) rather than a
    uniform run of letters, specifically so the derived arc id
    (create_arc's slugify collapses one contiguous run of non-alnum
    characters to a single dash) stays short. That isolates this test to
    the name-truncation fix under review; the separately unbounded
    slugify-length behavior in store.py is not in scope here."""
    cfg = Config(project_root=tmp_path)
    long_name = "n" + " " * 9998 + "n"           # 10,000 chars; slug -> "n-n"
    long_target = "z" * 10000                     # freeform item target, no slug involved
    store.create_arc(tmp_path, long_name, "", "item")
    store.add(tmp_path, kind="bug", target={"type": "item", "name": long_target},
              tags=["priority"])
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    longest = max(len(line) for line in text.splitlines())
    assert longest <= 200, f"name/target must be truncated, not just flattened: {longest}"


def test_name_and_body_together_render_the_real_bound_not_200(tmp_path):
    """The prior test only maxed out the target name, with an empty body --
    it never combined the two, so its `<=200` assertion held by accident of
    scenario rather than by anything the code guarantees. A maxed target
    name AND a near-max body together exceed 200: the real per-line bound is
    NAME_CHARS + BODY_CHARS plus the fixed surrounding text (kind, target
    type, brackets, spacing, id) -- computed here, not a round number."""
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "z" * 10000},
              body="b" * 250, tags=["priority"])
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    longest = max(len(line) for line in text.splitlines())
    overhead = len("  priority [bug] item:")   # "  priority [" + kind + "] " + target_type + ":"
    id_width = len("20260101-000000-000000-000")   # new_id()'s format is fixed-width
    expected = overhead + summary.NAME_CHARS + len("  ") + summary.BODY_CHARS + len("  ") + id_width
    assert longest == expected, f"expected the computed bound {expected}, got {longest}"
    assert longest > 200, "the old <=200 ceiling does not hold once name and body are both maxed"


def test_context_target_routes_to_heads_for(tmp_path):
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="note", target={"type": "item", "name": "widget"}, body="on widget")
    store.add(tmp_path, kind="note", target={"type": "item", "name": "other"}, body="on other")
    data = summary.context(tmp_path, cfg, target="item:widget")
    assert len(data["notes"]) == 1
    assert data["notes"][0]["target"]["name"] == "widget"


def test_context_commit_canonicalizes_then_queries(repo, tmp_path):
    """Proves the composition: canonical_commit resolves the ref ("HEAD") to
    a full sha, then query matches notes stored against that sha -- not
    against the literal ref string the caller typed. A decoy note on a
    different (real) commit proves this is an actual filter, not a query
    that would just as happily return everything."""
    cfg = Config(project_root=repo)
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    head = _run(repo, "rev-parse", "HEAD")
    parent = _run(repo, "rev-parse", "HEAD~1")
    store.add(store_dir, kind="note", target={"type": "commit", "name": head}, body="reviewed")
    store.add(store_dir, kind="note", target={"type": "commit", "name": parent}, body="decoy")
    data = summary.context(store_dir, cfg, commit="HEAD")
    assert [n["target"]["name"] for n in data["notes"]] == [head]


def test_context_branch_filters_heads_by_branch_commits(repo, tmp_path):
    cfg = Config(project_root=repo, default_branch="main")
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    main_head = _run(repo, "rev-parse", "main")
    _run(repo, "checkout", "-q", "-b", "topic")
    (repo / "topic_file").write_text("z")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "topic1")
    topic_head = _run(repo, "rev-parse", "HEAD")
    store.add(store_dir, kind="note", target={"type": "commit", "name": topic_head}, body="on topic")
    store.add(store_dir, kind="note", target={"type": "commit", "name": main_head}, body="on main")
    data = summary.context(store_dir, cfg, branch="topic")
    names = {n["target"]["name"] for n in data["notes"]}
    assert names == {topic_head}, "must include only commits unique to the branch, not shared ancestors"


def test_context_bare_returns_open_items_and_decisions(tmp_path):
    cfg = Config(project_root=tmp_path)
    open_bug = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    store.add(tmp_path, kind="bug", target={"type": "project", "name": None}, status="resolved")
    decision = store.add(tmp_path, kind="decision", target={"type": "project", "name": None}, body="chose x")
    note = store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    data = summary.context(tmp_path, cfg)
    ids = {n["id"] for n in data["notes"]}
    assert ids == {open_bug.id, decision.id, note.id}, "a plain kind (no bits) is context too"


def test_render_labels_the_task_count_as_standalone(tmp_path):
    """The count excludes arc members by design (see the test above),
    so a bare "1 open task(s)" beside an arc at 0/4 reads as a
    contradiction. The line names what is counted: outside arcs."""
    cfg = Config(project_root=tmp_path)
    a = store.create_arc(tmp_path, "x", "", "item")
    store.add(tmp_path, kind="task", target={"type": "item", "name": "standalone"})
    store.add(tmp_path, kind="task", target={"type": "item", "name": "in-arc"}, arc_id=a.id)
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    assert "open outside arcs:" in text and "task 1" in text


def test_render_aligns_the_progress_column_on_the_longest_id():
    """The id column was a hardcoded 28; a long arc id such as
    `cold-bootstrap-in-example-project` overflowed it in every summary."""
    d = summary.empty_summary()
    d["arcs"] = [{"id": "short", "done": 1, "total": 2, "name": "S"},
                       {"id": "cold-bootstrap-in-example-project", "done": 12, "total": 12, "name": "L"}]
    lines = summary.render_summary(d).splitlines()[1:]
    assert len({ln.index(" 1/2") if "1/2" in ln else ln.index(" 12/12") for ln in lines}) == 1


def test_arcs_sort_by_open_count_then_id(tmp_path):
    """The key is (-open, id). Created beta-first so insertion order alone would
    put beta ahead of alpha, and named so id order alone would put zebra last:
    each half of the key is falsifiable on its own."""
    cfg = Config(project_root=tmp_path)
    for name, n_items in (("beta", 2), ("alpha", 2), ("zebra", 3)):
        a = store.create_arc(tmp_path, name, "", "item")
        for i in range(n_items):
            store.add(tmp_path, kind="task", target={"type": "item", "name": f"{name}{i}"},
                      status="open", arc_id=a.id)
    rows = summary.summary(tmp_path, cfg)["arcs"]
    assert [r["id"] for r in rows] == ["zebra", "alpha", "beta"]
    assert [r["open"] for r in rows] == [3, 2, 2]


def test_summary_reports_unreadable_rows_with_the_first_error(tmp_path):
    """load() skips a row it cannot read, and the CLI never said so: a store
    whose rows all fail to read summarized as an EMPTY store. The hook line
    is where that has to show."""
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    with open(tmp_path / "notes.jsonl", "a") as f:
        f.write("{not json\n")
    d = summary.summary(tmp_path, cfg)
    assert d["unreadable"] == 1
    assert d["unreadable_first"].startswith("line 2: ")
    assert "1 row unreadable, first: line 2: " in summary.render_summary(d)


def test_summary_is_silent_about_unreadable_rows_when_there_are_none(tmp_path):
    """The normally-true direction: a stuck count would read as broken."""
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    d = summary.summary(tmp_path, cfg)
    assert d["unreadable"] == 0 and d["unreadable_first"] is None
    assert "unreadable" not in summary.render_summary(d)
    assert summary.empty_summary()["unreadable"] == 0


def test_summary_counts_open_questions_and_renders_zero(tmp_path):
    """Questions are the owner's queue (spec: ad-hoc tags reinvented this
    twice). The zero case is asserted because it is the
    normally-true one: a stuck 0 would read as broken."""
    cfg = Config(project_root=tmp_path)
    d = summary.summary(tmp_path, cfg)
    assert d["open"]["question"] == 0
    assert "question 0" in summary.render_summary(d)
    store.add(tmp_path, kind="question", target={"type": "project", "name": None}, body="which?")
    store.add(tmp_path, kind="question", target={"type": "project", "name": None},
              status="resolved")
    d = summary.summary(tmp_path, cfg)
    assert d["open"]["question"] == 1
    assert "question 1" in summary.render_summary(d)


def test_an_open_idea_does_not_pin_an_arc_on_the_hook_line(tmp_path):
    cfg = Config(project_root=tmp_path)
    a = store.create_arc(tmp_path, "x", "", "item")
    store.add(tmp_path, kind="idea", target={"type": "item", "name": "i"}, arc_id=a.id)
    assert summary.summary(tmp_path, cfg)["arcs"][0]["total"] == 0


def test_context_default_set_excludes_ideas_and_includes_questions(tmp_path):
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="idea", target={"type": "project", "name": None})
    q = store.add(tmp_path, kind="question", target={"type": "project", "name": None})
    assert {d["id"] for d in summary.context(tmp_path, cfg)["notes"]} == {q.id}


def test_summary_names_the_vocabulary_change_for_an_unmigrated_store(tmp_path):
    """The retired-kind hint's only path to a screen is the unreadable line;
    this is the end-to-end assertion that it gets there."""
    cfg = Config(project_root=tmp_path)
    store.ensure_store(tmp_path)
    with open(tmp_path / "notes.jsonl", "a") as f:
        f.write(json.dumps({"id": "old-1", "kind": "followup",
                            "target": {"type": "item", "name": "x"},
                            "created_at": "2020-01-01T00:00:00", "author": "claude",
                            "body": "", "status": "open"}) + "\n")
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    assert "'followup'" in text and "vocabulary changed" in text
    # The remedy is the actionable half and it sits at the END of the hint:
    # BODY_CHARS truncated it mid-word, which is why ERROR_CHARS exists.
    assert "or declare it under [kinds] in symbion.toml" in text


from symbion import kinds as K


def _declare(store_dir, text):
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(text)


def test_summary_counts_every_non_parked_status_kind_under_its_label(tmp_path):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\nshelf = { status = true, parked = true }\n'
                       'fact = {}\naudit = { verdict = true }\n')
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="anomaly", target={"type": "project", "name": None})
    store.add(tmp_path, kind="shelf", target={"type": "project", "name": None})
    d = summary.summary(tmp_path, cfg)
    assert d["open"] == {"anomaly": 1}
    assert "open_bugs" not in d
    assert summary.render_summary(d).splitlines()[0] == "symbion: open outside arcs: anomaly 1"


def test_summary_open_counts_rows_outside_arcs_only(tmp_path):
    cfg = Config(project_root=tmp_path)
    a = store.create_arc(tmp_path, "x", "", "item")
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "in"}, arc_id=a.id)
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "out"})
    d = summary.summary(tmp_path, cfg)
    assert d["open"]["bug"] == 1
    assert d["arcs"][0]["total"] == 1, "the arc still counts its own bug"


def test_open_rows_in_an_archived_or_unknown_arc_count_outside_arcs(tmp_path):
    """summary kept only active arcs and dropped every row with an arc_id, so
    an open row in an archived arc, or under an id that names no arc, showed
    nowhere: no count, no line, no +N, while `list --status open` held it.
    A row is on an arc's checklist only while that arc is active."""
    cfg = Config(project_root=tmp_path)
    live = store.create_arc(tmp_path, "live", "", "item")
    gone = store.create_arc(tmp_path, "gone", "", "item")
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "a"}, arc_id=live.id)
    archived = store.add(tmp_path, kind="bug", target={"type": "item", "name": "b"},
                         arc_id=gone.id)
    ghost = store.add(tmp_path, kind="bug", target={"type": "item", "name": "c"},
                      arc_id="nosuch")
    store.archive_arc(tmp_path, gone.id)
    d = summary.summary(tmp_path, cfg)
    assert d["open"]["bug"] == 2
    assert {h["id"] for h in d["heads"]} == {archived.id, ghost.id}
    assert [r["id"] for r in d["arcs"]] == [live.id]


def test_summary_line_reads_the_same_shape_on_a_default_store(tmp_path):
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    line = summary.render_summary(summary.summary(tmp_path, cfg)).splitlines()[0]
    assert line == "symbion: open outside arcs: bug 1, task 0, question 0"


def test_summary_line_with_no_status_kinds_declared(tmp_path):
    _declare(tmp_path, '[kinds]\nfact = {}\n')
    cfg = Config(project_root=tmp_path)
    d = summary.summary(tmp_path, cfg)
    assert d["open"] == {}
    assert "no status kinds" in summary.render_summary(d).splitlines()[0]


def test_empty_summary_carries_the_open_map():
    assert summary.empty_summary()["open"] == {}


def test_context_default_view_is_open_non_parked_rows_plus_plain_heads(tmp_path):
    """Both directions on one fixture: an open bug, a note head and a decision
    head are in; an open idea, a check and a resolved task are out."""
    cfg = Config(project_root=tmp_path)
    b = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None}, body="fact")
    d = store.add(tmp_path, kind="decision", target={"type": "project", "name": None})
    store.add(tmp_path, kind="idea", target={"type": "project", "name": None})
    store.add(tmp_path, kind="check", target={"type": "project", "name": None}, result="ok")
    store.add(tmp_path, kind="task", target={"type": "project", "name": None}, status="resolved")
    assert {r["id"] for r in summary.context(tmp_path, cfg)["notes"]} == {b.id, n.id, d.id}


def test_schema_on_a_table_less_store_is_the_defaults(tmp_path):
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    d = summary.schema(tmp_path, cfg)
    assert d["declared"] is False
    assert [k["label"] for k in d["kinds"]] == list(K.DEFAULT_KINDS)
    by = {k["label"]: k for k in d["kinds"]}
    assert by["bug"] == {"label": "bug", "status": True, "parked": False, "verdict": False,
                         "when": K.DEFAULT_KINDS["bug"].when, "rows": 1}
    assert by["task"]["rows"] == 0
    assert [t["type"] for t in d["targets"]] == ["commit", "item", "project", "arc"]


def test_schema_rows_counts_superseded_rows_too(tmp_path):
    """A rename has to rewrite every row under the label, heads or not."""
    cfg = Config(project_root=tmp_path)
    b = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    store.supersede(tmp_path, b.id, status="resolved")
    assert {k["label"]: k["rows"] for k in summary.schema(tmp_path, cfg)["kinds"]}["bug"] == 2


def test_schema_on_a_declared_store_lists_exactly_the_table_and_the_catalogs(tmp_path):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true, when = "off the curve" }\n'
                       '[catalogs]\nfile = "git ls-files"\n')
    cfg = Config(project_root=tmp_path, catalogs={"file": "git ls-files"})
    d = summary.schema(tmp_path, cfg)
    assert d["declared"] is True
    assert [k["label"] for k in d["kinds"]] == ["anomaly"]
    assert d["targets"][-1] == {"type": "file", "catalog": "git ls-files", "resolver": None}
    text = summary.render_schema(d)
    assert text.splitlines()[0].startswith("kinds  (symbion.toml [kinds])")
    assert "anomaly" in text and "status" in text and "off the curve" in text
    assert "file" in text and "catalog: git ls-files" in text
    assert "resolver:" not in text
    assert "absent" not in text


def test_schema_lists_a_resolver_beside_its_catalog(tmp_path):
    _declare(tmp_path, '[catalogs]\nnum = "cat names"\n[resolvers]\nnum = "bin/resolve"\n')
    cfg = Config(project_root=tmp_path, catalogs={"num": "cat names"}, resolvers={"num": "bin/resolve"})
    d = summary.schema(tmp_path, cfg)
    assert d["targets"][-1] == {"type": "num", "catalog": "cat names", "resolver": "bin/resolve"}
    assert all("resolver" in t for t in d["targets"])
    text = summary.render_schema(d)
    assert "catalog: cat names" in text and "resolver: bin/resolve" in text


def test_render_schema_says_the_defaults_are_defaults(tmp_path):
    cfg = Config(project_root=tmp_path)
    text = summary.render_schema(summary.schema(tmp_path, cfg))
    assert "absent = these defaults" in text.splitlines()[0]
    assert "status parked" in text and "verdict" in text and "plain" in text
    assert "built in" in text
    # The flag is --type; a heading of bare "targets" hid the list from a
    # human who went looking for types (2026-09-24).
    assert "targets  (--type; symbion.toml [catalogs])" in text.splitlines()


# ---- clip: the one truncation every capped field routes through ----
def test_clip_drops_emphasis_but_keeps_identifiers_and_globs():
    """`--body` is markdown, so `**bold**` arrives; the terminal showed the
    asterisks literal. Underscores inside a word and a lone glob star are
    NOT emphasis and must survive."""
    assert summary.clip("**This was measured** and *never* in arc_id '*.py'", 100) \
        == "This was measured and never in arc_id '*.py'"
    assert summary.clip("**Plan:** two steps", 100) == "Plan: two steps"
    # A star before a code span's closing backtick opens nothing.
    assert summary.clip("`30 * * * *` runs *hourly*", 100) == "`30 * * * *` runs hourly"
    assert summary.clip("It is **#2 by count** at **-3.5%**", 100) \
        == "It is #2 by count at -3.5%"
    # A closing pair is not the tail of a `***` marker inside the span.
    assert summary.clip("run **rc=0, down from 4 `***` lines to 0**, and", 100) \
        == "run rc=0, down from 4 `***` lines to 0, and"


def test_clip_keeps_two_globs_and_underscore_identifiers_intact():
    """Two glob stars in one body read as a `*…*` pair, and the preview
    printed the paths with their stars gone (2026-10-08, on a row deciding
    which files to delete). In row bodies `_x_` and `__x__` are identifiers
    (`__init__`, `_cache … type_`), not emphasis."""
    for s in ("rm logs/run-*.csv and out/*-clean.csv",
              "touched __init__.py, set _cache and type_ here",
              "`30 * * * *` and app.*.gz",
              "read each *.json under cache/*/ today",
              "zcat logs/*access* here",
              "f(*args) wrote tmp/*.log"):
        assert summary.clip(s, 100) == s


def test_clip_cuts_at_a_word_boundary_with_an_ellipsis_within_the_cap():
    s = "29 distinct test cases were counted in the suite"
    out = summary.clip(s, 30)
    assert out == "29 distinct test cases were…", out
    assert len(out) <= 30
    # No boundary in the back half: a hard cut, still visibly a cut, still capped.
    assert summary.clip("z" * 500, 100) == "z" * 99 + "…"
    # Under the cap: untouched, no ellipsis.
    assert summary.clip("short", 100) == "short"


def test_heads_and_priority_bodies_render_through_clip(tmp_path):
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "x"},
              body="**bold** " + "word " * 100)
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "y"},
              body="**starred**", tags=["priority"])
    data = summary.summary(tmp_path, cfg)
    head = next(h for h in data["heads"] if h["target"] == "item:x")
    assert head["body"].startswith("bold word") and head["body"].endswith("…")
    assert "**" not in head["body"] and len(head["body"]) <= summary.HEAD_CHARS
    assert data["priority"][0]["body"] == "starred"


def test_render_labels_the_priority_block_not_a_bare_star(tmp_path):
    """The header counts open rows OUTSIDE arcs; this block is every
    unresolved priority row, in an arc or not. `*` beside a header that
    disagreed with it read as a bug in the tool on first sight."""
    cfg = Config(project_root=tmp_path)
    aid = store.create_arc(tmp_path, "A", "", "item").id
    store.add(tmp_path, kind="task", target={"type": "item", "name": "in-arc"},
              arc_id=aid, tags=["priority"])
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    assert "task 0" in text.splitlines()[0]
    assert any(ln.startswith("  priority [task] item:in-arc") for ln in text.splitlines()), text
    assert not any(ln.startswith("  * ") for ln in text.splitlines())


def test_a_star_says_how_long_it_has_been_on(tmp_path, monkeypatch):
    """A star on a row with no status never expires, and the priority block
    filled with finished results starred while they were news (2026-09-24).
    Each star prints its age, counted from the oldest row of its chain that
    carries it, so a later edit does not make it read fresh. Under a day it
    says nothing."""
    cfg = Config(project_root=tmp_path)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    n = store.add(tmp_path, kind="decision", target={"type": "project", "name": None},
                  body="chose x", tags=["priority"], created_at=t0.isoformat())
    monkeypatch.setattr(store, "_now_iso", lambda: (t0 + timedelta(days=3)).isoformat())
    store.supersede(tmp_path, n.id, author="other", body="picked x")
    data = summary.summary(tmp_path, cfg, _now=t0 + timedelta(days=12, hours=1))
    assert data["priority"][0]["starred_days"] == 12
    assert "[decision, starred 12d ago]" in summary.render_summary(data)
    data = summary.summary(tmp_path, cfg, _now=t0 + timedelta(hours=1))
    assert "starred" not in summary.render_summary(data)


def test_arc_rows_carry_an_age(tmp_path):
    """`0/10` on a 31-minute-old arc read as stalled, and a dead one reads
    the same. Age separates never-started from long-dead."""
    from datetime import datetime, timedelta
    cfg = Config(project_root=tmp_path)
    a = store.create_arc(tmp_path, "Fresh", "", "item")
    created = datetime.fromisoformat(a.created_at)
    data = summary.summary(tmp_path, cfg, _now=created + timedelta(days=12, hours=3))
    row = data["arcs"][0]
    assert row["created_at"] == a.created_at and row["age_days"] == 12
    assert "12d" in summary.render_summary(data).splitlines()[1]
    assert summary.summary(tmp_path, cfg)["arcs"][0]["age_days"] == 0


def test_age_days_reads_an_offset_free_stamp_as_local():
    from datetime import datetime, timedelta
    now = datetime(2026, 9, 20, 12, 0).astimezone()
    assert summary.age_days("2026-09-18T11:00:00", now) == 2
    assert summary.age_days((now + timedelta(days=1)).isoformat(), now) == 0


def test_context_default_view_drops_a_plain_head_tagged_retired(tmp_path):
    """The exit for a plain head: it has no status to resolve, so the tag is
    the retirement. Scoped to plain kinds: an open task tagged retired is still
    open work and stays in, and a note with any other tag stays in."""
    cfg = Config(project_root=tmp_path)
    p = {"type": "project", "name": None}
    kept = store.add(tmp_path, kind="note", target=p, body="gotcha", tags=["gotcha"])
    gone = store.add(tmp_path, kind="note", target=p, body="built at abc", tags=["retired"])
    t = store.add(tmp_path, kind="task", target=p, tags=["retired"])
    ids = {r["id"] for r in summary.context(tmp_path, cfg)["notes"]}
    assert kept.id in ids and t.id in ids
    assert gone.id not in ids


def test_every_elided_line_names_the_flag_that_lifts_it():
    """`+21 more open` with no way out sent a reader to `list --status open
    --json` and back (reported 2026-09-22 from an adopting project). list's
    own header already says `(--all)`; the summary uses the same idiom."""
    d = summary.empty_summary()
    d.update(arcs_elided=3, priority_elided=2, heads_elided=21)
    elided = [l for l in summary.render_summary(d).splitlines() if " more" in l]
    assert len(elided) == 3, elided
    assert all(l.endswith("(--full)") for l in elided), elided


def test_summary_full_says_when_nothing_was_elided():
    """`summary --full` with nothing over a cap printed the same text as
    `summary`, so the flag read as dropped. One trailing line, only when
    --full was asked for and lifted nothing."""
    d = summary.empty_summary()
    d["full"] = True
    assert summary.render_summary(d).splitlines()[-1] == "  (--full: nothing was elided)"
    d["heads_elided"] = 0; d["arcs_elided"] = 1
    assert "nothing was elided" not in summary.render_summary(d)
    d["arcs_elided"] = 0; d["full"] = False
    assert "nothing was elided" not in summary.render_summary(d)


def test_summary_dict_carries_the_full_flag(tmp_path):
    cfg = Config(project_root=tmp_path)
    assert summary.summary(tmp_path, cfg, full=True)["full"] is True
    assert summary.summary(tmp_path, cfg)["full"] is False
    assert summary.empty_summary()["full"] is False


def test_summary_on_an_empty_store_names_the_first_verb(tmp_path):
    """A fresh store printed zero counts and named no verb (2026-09-22).
    The empty state IS the onboarding surface.
    Bounded by store state, not session: gone after the first row, absent
    on a missing store (which already says `run symbion init`)."""
    cfg = Config(project_root=tmp_path)
    store.ensure_store(tmp_path)
    text = summary.render_summary(summary.summary(tmp_path, cfg))
    assert text.splitlines()[-1] == "  " + summary.FIRST_CONTACT
    assert "symbion add task --target project" in summary.FIRST_CONTACT \
        and "SKILL.md" in summary.FIRST_CONTACT
    store.add(tmp_path, kind="task", target={"type": "project", "name": None}, body="x")
    assert "no notes yet" not in summary.render_summary(summary.summary(tmp_path, cfg))
    assert "no notes yet" not in summary.render_summary(summary.empty_summary())


def test_a_starred_row_is_not_listed_again_in_the_heads(tmp_path):
    """The priority block already prints it. Both directions: the starred
    row leaves the heads and the unstarred one stays."""
    cfg = Config(project_root=tmp_path)
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "starred"},
              tags=["priority"])
    store.add(tmp_path, kind="bug", target={"type": "item", "name": "plain"})
    data = summary.summary(tmp_path, cfg)
    assert [p["target"] for p in data["priority"]] == ["item:starred"]
    assert [h["target"] for h in data["heads"]] == ["item:plain"]
    assert data["heads_elided"] == 0


def test_open_preregistrations_are_not_pushed_off_by_newer_rows(tmp_path):
    """Newest-first under one cap hid an open prediction for many
    sessions. Pre-registrations get their own slots ahead of the rest; the
    rest stay capped, which is the other direction of the same claim."""
    cfg = Config(project_root=tmp_path)
    _declare(tmp_path, '[kinds]\nbug = { status = true }\n'
                       'prediction = { status = true, verdict = true }\n')
    for i in range(3):
        store.add(tmp_path, kind="prediction", target={"type": "item", "name": f"p{i}"},
                  checked="x")
    for i in range(summary.HEAD_CAP + 2):
        store.add(tmp_path, kind="bug", target={"type": "item", "name": f"b{i}"})
    data = summary.summary(tmp_path, cfg)
    kinds = [h["kind"] for h in data["heads"]]
    assert kinds == ["prediction"] * 3 + ["bug"] * summary.HEAD_CAP
    assert data["heads_elided"] == 2
    assert "+2 more open (--full)" in summary.render_summary(data)


def test_a_kind_the_header_counts_cannot_be_missing_from_the_heads(tmp_path):
    """The header counts open rows per kind and the list was one newest-first
    cap over all of them, so a whole kind could be counted and unlisted:
    measured 2026-09-28 on an adopter store where a bootstrap wrote its two
    `question` rows first and both landed in `+6 more open`, though the kind
    exists for the owner to see at session start. The heads are dealt one kind
    at a time now. Both directions: every counted kind appears, and the cap
    still holds."""
    cfg = Config(project_root=tmp_path)
    _declare(tmp_path, '[kinds]\nbug = { status = true }\ntask = { status = true }\n'
                       'question = { status = true }\n')
    for i in range(2):                      # the oldest rows in the store
        store.add(tmp_path, kind="question", target={"type": "item", "name": f"q{i}"})
    for i in range(7):
        store.add(tmp_path, kind="bug", target={"type": "item", "name": f"b{i}"})
        store.add(tmp_path, kind="task", target={"type": "item", "name": f"t{i}"})
    data = summary.summary(tmp_path, cfg)
    assert data["open"] == {"bug": 7, "task": 7, "question": 2}
    kinds = [h["kind"] for h in data["heads"]]
    assert kinds.count("question") == 2, kinds
    assert len(kinds) == summary.HEAD_CAP and data["heads_elided"] == 16 - summary.HEAD_CAP
    assert "+8 more open (--full)" in summary.render_summary(data)
    full = summary.summary(tmp_path, cfg, full=True)
    assert [h["id"] for h in full["heads"][:summary.HEAD_CAP]] == [h["id"] for h in data["heads"]], \
        "--full lifts the cap; it must not re-order what was already shown"


def test_the_due_block_lists_past_due_and_due_soon_open_rows_once(tmp_path):
    """Past due is louder than every other block, so it is not limited to
    rows outside arcs, and a parked row with a date surfaces too: a due date
    is an explicit ask to be reminded. Each row prints once. The other
    direction: far-off, resolved and undated rows stay out of the block."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    cfg = Config(project_root=tmp_path)
    aid = store.create_arc(tmp_path, "A", "", "item").id
    t = {"type": "item", "name": None}
    add = lambda name, **kw: store.add(tmp_path, target=t | {"name": name}, **kw).id
    in_arc = add("in-arc", kind="task", due="2026-09-22", arc_id=aid)
    starred = add("starred", kind="bug", due="2026-09-23", tags=["priority"])
    soon = add("soon", kind="task", due="2026-09-27")
    parked = add("parked", kind="idea", due="2026-09-24T09:00:00+09:00")
    far = add("far", kind="task", due="2026-10-30")
    done = add("done", kind="task", due="2026-09-01")
    store.supersede(tmp_path, done, status="resolved")
    add("undated", kind="task")

    data = summary.summary(tmp_path, cfg, _now=now)
    assert [(d["id"], d["phrase"]) for d in data["due"]] == [
        (in_arc, "overdue 2d"), (starred, "overdue 1d"),
        (parked, "overdue today"), (soon, "due in 3d")]
    assert data["due_elided"] == 0
    listed = {p["id"] for p in data["priority"]} | {h["id"] for h in data["heads"]}
    assert not listed & {d["id"] for d in data["due"]}, "a due row prints once"
    assert far in {h["id"] for h in data["heads"]}
    text = summary.render_summary(data)
    assert text.splitlines()[1].startswith("  overdue 2d [task] item:in-arc"), \
        "the due block leads, right under the header"


def test_the_due_block_is_capped_and_says_how_many_it_hid(tmp_path):
    cfg = Config(project_root=tmp_path)
    for i in range(summary.DUE_CAP + 3):
        store.add(tmp_path, kind="task", target={"type": "item", "name": f"t{i}"},
                  due="2000-01-01")
    data = summary.summary(tmp_path, cfg)
    assert len(data["due"]) == summary.DUE_CAP
    assert data["due_elided"] == 3
    assert "+3 more due (--full)" in summary.render_summary(data)
    assert len(summary.summary(tmp_path, cfg, full=True)["due"]) == summary.DUE_CAP + 3


def _versions(*versions):
    """A supersede chain of (author, body), oldest first: by_id and its head."""
    rows = [SimpleNamespace(id=f"r{i}", supersedes=f"r{i - 1}" if i else None, author=a,
                            created_at=f"2026-10-0{i + 1}T00:00:00+00:00", body=b)
            for i, (a, b) in enumerate(versions)]
    return {r.id: r for r in rows}, rows[-1]


def test_seams_mark_where_each_append_and_prepend_joins_the_heads_body():
    by_id, head = _versions(("ada", "lead"), ("sam", "lead\n\nmore"),
                            ("kim", "top\n\nlead\n\nmore"))
    got = summary.seams(by_id, head)
    assert got == [(5, "kim", "2026-10-03T00:00:00+00:00", True),
                   (9, "sam", "2026-10-02T00:00:00+00:00", False)]
    assert [head.body[a:b] for a, b in pairwise([0, 5, 9, None])] == \
        ["top\n\n", "lead", "\n\nmore"]


def test_a_rewrite_drops_the_seams_before_it_and_an_unchanged_body_keeps_them():
    kept = _versions(("ada", "a"), ("sam", "a\n\nb"), ("kim", "a\n\nb"))
    assert summary.seams(*kept) == [(1, "sam", "2026-10-02T00:00:00+00:00", False)]
    rewritten = _versions(("ada", "a"), ("sam", "a\n\nb"), ("kim", "A\n\nb"))
    assert summary.seams(*rewritten) == []
    after = _versions(("ada", "a"), ("sam", "x"), ("kim", "x\n\nc"))
    assert summary.seams(*after) == [(1, "kim", "2026-10-03T00:00:00+00:00", False)]
    # A body written into an empty row is its first text, not an amendment.
    assert summary.seams(*_versions(("ada", ""), ("sam", "b"))) == []


def test_one_link_that_adds_above_and_below_gets_a_seam_at_each_end():
    """A prepend, then an append to the same uncommitted row, rewrites it in
    place: one link added text at both ends, and read as a rewrite it lost
    both seams."""
    by_id, head = _versions(("ada", "lead"), ("sam", "x"), ("kim", "top\n\nx\n\nend"))
    t = "2026-10-03T00:00:00+00:00"
    assert summary.seams(by_id, head) == [(5, "kim", t, True), (6, "kim", t, False)]
    # A newline is not an amendment: a seam marks text on its side.
    by_id, head = _versions(("ada", "x"), ("sam", "top\n\nx\n"))
    assert summary.seams(by_id, head) == [(5, "sam", "2026-10-02T00:00:00+00:00", True)]
    by_id, head = _versions(("ada", "x"), ("sam", "\n\nx\n\nend"))
    assert summary.seams(by_id, head) == [(3, "sam", "2026-10-02T00:00:00+00:00", False)]
