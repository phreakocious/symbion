"""The merge of two copies of a store: a set merge of notes.jsonl, a field merge of
arcs.jsonl, and the git merge driver that runs them."""
import json
import shlex
import subprocess
import sys

import pytest

from symbion import cli, gitref, merge

# An entry X: its head under id X, its replaced version kept under Xk. Each
# side's edit renames the replaced line and appends the new head.
X, Xk, Xk2 = "20261026-100000-000001-d47", "20261026-100001-000002-a01", "20261026-100002-000003-b02"
Y, Yk = "20261026-100010-000004-c03", "20261026-100011-000005-e04"


def row(id, **kw):
    return json.dumps({"id": id, "kind": "note", "body": f"body of {id}", **kw}, ensure_ascii=False)


def ids(lines):
    return [json.loads(ln)["id"] for ln in lines]


def arc(id, **kw):
    return json.dumps({"id": id, "name": id, "description": "", "target_scope": "item",
                       "created_at": "2026-10-20T00:00:00+00:00", "author": "a",
                       "archived": False, "archived_at": None, **kw}, ensure_ascii=False)


def arcs_of(lines):
    return {d["id"]: d for d in map(json.loads, lines)}


def test_edits_to_adjacent_lines_merge_without_a_duplicate_id():
    base = [row(X, body="x1"), row(Y, body="y1")]
    ours = [row(Xk, body="x1"), row(Y, body="y1"), row(X, body="x2", supersedes=Xk)]
    theirs = [row(X, body="x1"), row(Yk, body="y1"), row(Y, body="y2", supersedes=Yk)]
    lines, splits = merge.merge_notes(base, ours, theirs)
    assert ids(lines) == [Xk, X, Yk, Y] and splits == []


def test_appends_on_both_sides_keep_both_in_ours_then_theirs_order():
    a, b, c, d = (row(f"20261026-10000{i}-000000-00{i}") for i in range(4))
    lines, splits = merge.merge_notes([a], [a, b, c], [a, d])
    assert lines == [a, b, c, d] and splits == []


def test_both_sides_editing_one_entry_splits_it_and_upstream_keeps_the_id():
    base = [row(X, body="x1")]
    ours = [row(Xk, body="x1"), row(X, body="ours", supersedes=Xk)]
    theirs = [row(Xk2, body="x1"), row(X, body="theirs", supersedes=Xk2)]
    lines, splits = merge.merge_notes(base, ours, theirs)
    got = [json.loads(ln) for ln in lines]
    assert [d["id"] for d in got].count(X) == 1 and len({d["id"] for d in got}) == 4
    assert [d["body"] for d in got if d["id"] == X] == ["theirs"]
    (kept, new), = splits
    assert kept == X and len(new) == len(X) and new[:-3] == X[:-3] and new[-3:] != X[-3:]
    assert set(new[-3:]) <= set("0123456789abcdef"), new
    mine = next(d for d in got if d["id"] == new)
    assert (mine["body"], mine["supersedes"]) == ("ours", Xk)
    assert ids(lines) == [Xk, new, Xk2, X]


def test_a_split_id_is_not_one_already_in_the_merge():
    taken = X[:-3] + "d48"
    base = [row(X), row(taken)]
    ours = [row(Xk), row(taken), row(X, body="ours", supersedes=Xk)]
    theirs = [row(Xk2), row(taken), row(X, body="theirs", supersedes=Xk2)]
    lines, splits = merge.merge_notes(base, ours, theirs)
    assert len(set(ids(lines))) == len(lines) == 5
    assert splits[0][1] not in (X, taken)


def test_an_id_made_outside_symbion_splits_too():
    base = [row("old")]
    ours = [row("older"), row("old", body="ours", supersedes="older")]
    theirs = [row("oldest"), row("old", body="theirs", supersedes="oldest")]
    lines, splits = merge.merge_notes(base, ours, theirs)
    assert len(set(ids(lines))) == 4 and splits == [("old", "old-001")]


def test_an_unparseable_line_is_kept_verbatim():
    odd = '{"id":"A",   "body":"x"}'
    bad, worse = '{"id": "broken', "not json at all"
    base, ours, theirs = [odd, bad], [odd, bad, row(X)], [odd, bad, worse]
    lines, splits = merge.merge_notes(base, ours, theirs)
    assert lines == [odd, bad, row(X), worse] and splits == []
    # A line with no id, or an id that is not a string, is never split.
    ours, theirs = [json.dumps({"id": 7, "body": "a"})], [json.dumps({"id": 7, "body": "b"})]
    assert merge.merge_notes([], ours, theirs) == (ours + theirs, [])


def test_an_arc_field_one_side_changed_takes_that_side():
    base = [arc("a")]
    ours = [arc("a", description="mine")]
    theirs = [arc("a", name="theirs")]
    lines, dropped = merge.merge_arcs(base, ours, theirs)
    got = arcs_of(lines)["a"]
    assert (got["description"], got["name"], dropped) == ("mine", "theirs", [])


def test_both_sides_archiving_keeps_the_earlier_archived_at():
    base = [arc("a")]
    early = arc("a", archived=True, archived_at="2026-10-21T00:00:00+00:00")
    late = arc("a", archived=True, archived_at="2026-10-24T00:00:00+00:00")
    for ours, theirs in ((early, late), (late, early)):
        lines, dropped = merge.merge_arcs(base, [ours], [theirs])
        got = arcs_of(lines)["a"]
        assert (got["archived"], got["archived_at"], dropped) == \
            (True, "2026-10-21T00:00:00+00:00", [])


def test_a_slug_both_sides_created_keeps_upstreams_and_names_the_dropped_value():
    mine, theirs, only = arc("a", name="Mine"), arc("a", name="Theirs"), arc("b")
    lines, dropped = merge.merge_arcs([], [mine, arc("c")], [theirs, only])
    assert list(arcs_of(lines)) == ["a", "c", "b"] and arcs_of(lines)["a"]["name"] == "Theirs"
    (msg,) = dropped
    assert "a" in msg and "name" in msg and "Mine" in msg and "Theirs" in msg
    # The same field changed on both sides from a shared base is the same conflict.
    lines, dropped = merge.merge_arcs([arc("a")], [mine], [theirs])
    assert arcs_of(lines)["a"]["name"] == "Theirs" and len(dropped) == 1


def test_an_arc_field_the_merge_does_not_know_is_compared_too():
    base = [arc("a")]
    ours = [arc("a", color="red")]
    theirs = [arc("a", color="blue")]
    lines, dropped = merge.merge_arcs(base, ours, theirs)
    assert arcs_of(lines)["a"]["color"] == "blue" and "color" in dropped[0]
    lines, dropped = merge.merge_arcs(base, ours, base)
    assert arcs_of(lines)["a"]["color"] == "red" and dropped == []
    # A key only the other side holds comes across.
    lines, dropped = merge.merge_arcs(base, base, theirs)
    assert arcs_of(lines)["a"]["color"] == "blue" and dropped == []


def test_an_unreadable_arc_line_is_kept_unless_a_side_fixed_it():
    bad, fixed = '{"id": "broken', arc("fixed")
    lines, dropped = merge.merge_arcs([bad], [bad, arc("a")], [bad, arc("b")])
    assert lines == [bad, arc("a"), arc("b")] and dropped == []
    assert merge.merge_arcs([bad], [bad], [fixed])[0] == [fixed]
    worse = "not json"
    assert merge.merge_arcs([bad], [fixed], [bad, worse])[0] == [fixed, worse]


@pytest.mark.parametrize("rewriter", ["ours", "theirs"])
def test_a_rewrite_on_one_side_and_an_edit_on_the_other_leave_one_head(rewriter):
    # sed over every line on one side (task -> note); an edit of X on the other
    x1, y1 = row(X, kind="task", body="x1"), row(Y, kind="task", body="y1")
    edit = [row(Xk, kind="task", body="x1"), y1, row(X, kind="task", body="x2", supersedes=Xk)]
    rewrite = [ln.replace('"task"', '"note"') for ln in (x1, y1)]
    ours, theirs = (rewrite, edit) if rewriter == "ours" else (edit, rewrite)
    said = []
    lines, splits = merge.merge_notes([x1, y1], ours, theirs, dropped=said)
    got = [json.loads(ln) for ln in lines]
    assert sorted(d["id"] for d in got) == sorted([Xk, X, Y]) and splits == [] and said == []
    head = next(d for d in got if d["id"] == X)
    assert (head["body"], head["supersedes"]) == ("x2", Xk)
    assert next(d for d in got if d["id"] == Y)["kind"] == "note"


def test_two_differing_rewrites_of_one_line_keep_the_upstreams_and_say_so(tmp_path, capsys):
    x1 = row(X, kind="todo")
    mine, upstream = x1.replace("todo", "task"), x1.replace("todo", "note")
    said = []
    assert merge.merge_notes([x1], [mine], [upstream], dropped=said) == ([upstream], [])
    (msg,) = said
    assert X in msg
    base, ours, theirs = (tmp_path / n for n in ("base", "ours", "theirs"))
    for path, line in ((base, x1), (ours, mine), (theirs, upstream)):
        path.write_text(line + "\n", encoding="utf-8")
    assert merge.driver(base, ours, theirs, "notes.jsonl") == 0
    assert ours.read_text(encoding="utf-8") == upstream + "\n"
    (err,) = capsys.readouterr().err.splitlines()
    assert err == f"symbion: {msg}"


def test_a_whole_file_rewrite_on_one_side_keeps_the_other_sides_new_rows():
    a, b, n = (row(f"20261026-10000{i}-000000-00{i}", kind="todo") for i in range(3))
    a2, b2 = (ln.replace('"todo"', '"task"') for ln in (a, b))
    lines, splits = merge.merge_notes([a, b], [a2, b2], [a, b, n])
    assert lines == [a2, b2, n] and splits == []


def test_the_driver_writes_lf_and_says_each_split_and_dropped_value(tmp_path, capsys):
    base, ours, theirs = (tmp_path / n for n in ("base", "ours", "theirs"))
    one = [row(X, body="x1")]
    base.write_text("\n".join(one) + "\n", encoding="utf-8")
    ours.write_text("\r\n".join([row(Xk), row(X, body="ours", supersedes=Xk)]) + "\r\n",
                    encoding="utf-8", newline="")
    theirs.write_text("\n".join([row(Xk2), row(X, body="theirs", supersedes=Xk2)]) + "\n",
                      encoding="utf-8")
    assert merge.driver(base, ours, theirs, "store/notes.jsonl") == 0
    raw = ours.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and len(raw.splitlines()) == 4
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith("symbion: ") and X in err[0]

    arcs = tmp_path / "arcs.jsonl"
    base.write_text("", encoding="utf-8")
    arcs.write_text(arc("a", name="Mine") + "\n", encoding="utf-8")
    theirs.write_text(arc("a", name="Theirs") + "\n", encoding="utf-8")
    assert merge.driver(base, arcs, theirs, "arcs.jsonl") == 0
    assert json.loads(arcs.read_text(encoding="utf-8"))["name"] == "Theirs"
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith("symbion: ") and "Mine" in err[0]


def test_the_merge_driver_verb_needs_no_store(tmp_path, monkeypatch):
    work = tmp_path / "nowhere"
    work.mkdir()
    monkeypatch.chdir(work)
    base, ours, theirs = (work / n for n in ("base", "ours", "theirs"))
    base.write_text("", encoding="utf-8")
    ours.write_text(row(X) + "\n", encoding="utf-8")
    theirs.write_text(row(Y) + "\n", encoding="utf-8")
    assert cli.main(["merge-driver", str(base), str(ours), str(theirs), "notes.jsonl"]) == 0
    assert ids(ours.read_text(encoding="utf-8").splitlines()) == [X, Y]
    assert cli.main(["merge-driver", str(base)]) == 2


def test_the_driver_starts_no_process(tmp_path, monkeypatch):
    # git runs the driver from the store's root, where on Windows a bare `git`
    # finds a git.exe the other side committed before the real one
    def refuse(*args, **kw):
        raise AssertionError(f"the driver started {args[0]!r}")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    (tmp_path / ".git" / "rebase-merge").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GIT_DIR", raising=False)
    base, ours, theirs = (tmp_path / n for n in ("base", "ours", "theirs"))
    base.write_text("", encoding="utf-8")
    ours.write_text(row(X) + "\n", encoding="utf-8")
    theirs.write_text(row(Y) + "\n", encoding="utf-8")
    assert merge.driver(base, ours, theirs, "notes.jsonl") == 0
    assert ids(ours.read_text(encoding="utf-8").splitlines()) == [Y, X]     # a rebase's sides


def _kept_id(tmp_path) -> str:
    """Whose edit of X the driver lets keep its id: %A's or %B's."""
    base, ours, theirs = (tmp_path / n for n in ("base", "ours", "theirs"))
    base.write_text(row(X, body="x1") + "\n", encoding="utf-8")
    ours.write_text(row(Xk, body="x1") + "\n" + row(X, body="%A", supersedes=Xk) + "\n",
                    encoding="utf-8")
    theirs.write_text(row(Xk2, body="x1") + "\n" + row(X, body="%B", supersedes=Xk2) + "\n",
                      encoding="utf-8")
    assert merge.driver(base, ours, theirs, "notes.jsonl") == 0
    return next(d["body"] for d in map(json.loads, ours.read_text(encoding="utf-8").splitlines())
                if d["id"] == X)


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_the_driver_finds_a_rebase_through_a_git_file(tmp_path, monkeypatch, absolute):
    # A linked worktree's .git is a file that names its own git dir, where rebase-merge is
    gitdir = tmp_path / "main" / ".git" / "worktrees" / "w"
    gitdir.mkdir(parents=True)
    w = tmp_path / "w"
    w.mkdir()
    named = gitdir if absolute else "../main/.git/worktrees/w"
    (w / ".git").write_text(f"gitdir: {named}\n", encoding="utf-8")
    monkeypatch.chdir(w)
    monkeypatch.delenv("GIT_DIR", raising=False)
    assert _kept_id(tmp_path) == "%B"               # a merge: %B is the upstream
    (gitdir / "rebase-merge").mkdir()
    assert _kept_id(tmp_path) == "%A"               # a rebase: %A is


@pytest.mark.parametrize("named", ["absolute", "relative", "gitfile"])
def test_the_driver_reads_git_dir_before_the_cwds_git(tmp_path, monkeypatch, named):
    # git sets GIT_DIR for the driver in a linked worktree, and under --git-dir
    gitdir = tmp_path / "elsewhere.git"
    gitdir.mkdir()
    w = tmp_path / "w"
    (w / ".git" / "rebase-apply").mkdir(parents=True)   # the cwd's says rebase: not read
    (tmp_path / "link").write_text("gitdir: elsewhere.git\n", encoding="utf-8")  # from its dir
    monkeypatch.chdir(w)
    monkeypatch.setenv("GIT_DIR", {"absolute": str(gitdir), "relative": "../elsewhere.git",
                                   "gitfile": "../link"}[named])
    assert _kept_id(tmp_path) == "%B"
    (gitdir / "rebase-merge").mkdir()
    assert _kept_id(tmp_path) == "%A"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_ensure_merge_driver_registers_once_and_quotes_the_interpreter(tmp_path, monkeypatch):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main", "--template=")       # no info/ in the git dir
    info = r / ".git" / "info"
    assert not info.exists()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "a b" / "python"))
    assert gitref.ensure_merge_driver(r) is True
    assert (info / "attributes").read_text(encoding="utf-8").splitlines() == \
        ["notes.jsonl merge=symbion", "arcs.jsonl merge=symbion"]
    driver = _git(r, "config", "--local", "merge.symbion.driver")
    # as_posix: git runs the driver through sh, which reads a backslash as an escape
    assert shlex.split(driver) == [(tmp_path / "a b" / "python").as_posix(), "-P", "-m",
                                   "symbion", "merge-driver", "%O", "%A", "%B", "%P"]
    assert _git(r, "config", "--local", "merge.symbion.name")
    assert gitref.ensure_merge_driver(r) is False
    assert _git(r, "status", "--porcelain") == ""


def test_ensure_merge_driver_keeps_the_attributes_a_person_wrote(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / ".git" / "info").mkdir(exist_ok=True)
    (r / ".git" / "info" / "attributes").write_text("*.png binary", encoding="utf-8")
    assert gitref.ensure_merge_driver(r) is True
    assert (r / ".git" / "info" / "attributes").read_text(encoding="utf-8").splitlines() == \
        ["*.png binary", "notes.jsonl merge=symbion", "arcs.jsonl merge=symbion"]


def test_ensure_merge_driver_in_a_linked_worktree_writes_the_common_dir(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "commit", "-q", "--allow-empty", "-m", "c0")
    _git(r, "worktree", "add", "-q", str(tmp_path / "w"))
    assert gitref.ensure_merge_driver(tmp_path / "w") is True
    assert "notes.jsonl merge=symbion" in (r / ".git" / "info" / "attributes").read_text(
        encoding="utf-8")


def _commit(r, lines, msg):
    (r / "notes.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", msg)


def _diverge(tmp_path, base, upstream, local, extra=None):
    """A repo with the driver registered: `base` on main, then `upstream` on
    branch other and `local` on main. Back on main."""
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    assert gitref.ensure_merge_driver(r) is True
    for name, text in (extra or {}).items():
        (r / name).write_text(text, encoding="utf-8")
    _commit(r, base, "base")
    _git(r, "checkout", "-q", "-b", "other")
    _commit(r, upstream, "upstream")
    _git(r, "checkout", "-q", "main")
    _commit(r, local, "local")
    return r


def _git_run(r, *args):
    return subprocess.run(["git", "-C", str(r), *args], capture_output=True, text=True)


def test_the_driver_merges_a_real_git_conflict(tmp_path):
    r = _diverge(tmp_path, [row(X, body="x1"), row(Y, body="y1")],
                 [row(X, body="x1"), row(Yk, body="y1"), row(Y, body="y2", supersedes=Yk)],
                 [row(Xk, body="x1"), row(Y, body="y1"), row(X, body="x2", supersedes=Xk)])
    done = _git_run(r, "merge", "--no-edit", "other")
    assert done.returncode == 0, done.stdout + done.stderr
    assert ids((r / "notes.jsonl").read_text(encoding="utf-8").splitlines()) == [Xk, X, Yk, Y]


def test_a_file_the_store_commits_cannot_shadow_the_drivers_imports(tmp_path):
    # git runs the driver from the repo root, and `python -m` puts the cwd first on sys.path
    r = _diverge(tmp_path, [row(X, body="x1")],
                 [row(Xk2, body="x1"), row(X, body="up", supersedes=Xk2)],
                 [row(Xk, body="x1"), row(X, body="mine", supersedes=Xk)],
                 extra={"pathlib.py": "raise SystemExit(3)\n"})
    done = _git_run(r, "merge", "--no-edit", "other")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(ids((r / "notes.jsonl").read_text(encoding="utf-8").splitlines())) == 4


@pytest.mark.parametrize("backend", [[], ["--apply"]], ids=["merge", "apply"])
def test_a_rebase_keeps_the_head_of_the_branch_it_rebases_onto(tmp_path, backend):
    # git hands a rebase's driver the sides the other way round
    r = _diverge(tmp_path, [row(X, body="x1")],
                 [row(Xk2, body="x1"), row(X, body="upstream", supersedes=Xk2)],
                 [row(Xk, body="x1"), row(X, body="local", supersedes=Xk)])
    done = _git_run(r, "rebase", *backend, "other")
    assert done.returncode == 0, done.stdout + done.stderr
    got = {d["id"]: d for d in map(json.loads, (r / "notes.jsonl").read_text(
        encoding="utf-8").splitlines())}
    assert got[X]["body"] == "upstream"
    (new,) = [i for i, d in got.items() if d["body"] == "local"]
    assert new != X and new[:-3] == X[:-3] and got[new]["supersedes"] == Xk
    assert f"symbion: both sides edited {X}: the upstream's keeps the id, yours is now {new}" \
        in done.stderr


# reapply: a clone's pending changes put back on the file a pull's merge left.
Xk3 = "20261026-100003-000006-f05"


def heads_of(lines):
    rows = [json.loads(ln) for ln in lines]
    later = {d.get("supersedes") for d in rows}
    return {d["id"]: d["body"] for d in rows if d["id"] not in later}


def test_reapply_continues_the_entry_the_merge_split_on_the_side_that_edited_it():
    # Both sides committed an edit of X, so the merge split it: ours went to a new id.
    # A pending edit made on top of our committed head continues that entry.
    base = [row(X, body="x1")]
    head = [row(Xk, body="x1"), row(X, body="ours1", supersedes=Xk)]
    upstream = [row(Xk2, body="x1"), row(X, body="theirs", supersedes=Xk2)]
    merged, ((kept, new),) = merge.merge_notes(base, head, upstream)
    working = [row(Xk, body="x1"), row(Xk3, body="ours1", supersedes=Xk),
               row(X, body="ours2", supersedes=Xk3)]
    lines, splits = merge.reapply(head, working, merged, {kept: new})
    assert splits == [] and len(set(ids(lines))) == len(lines) == 5
    assert heads_of(lines) == {X: "theirs", new: "ours2"}     # no second head on ours1
    assert json.loads(lines[-1])["supersedes"] == Xk3
    assert ids(lines) == [Xk, Xk2, X, Xk3, new]


def test_reapply_splits_a_pending_edit_the_upstream_also_edited_and_upstream_keeps_the_id():
    head = [row(X, body="x1"), row(Y)]
    working = [row(Xk, body="x1"), row(Y), row(X, body="mine", supersedes=Xk)]
    merged = [row(Xk2, body="x1"), row(Y), row(X, body="theirs", supersedes=Xk2)]
    lines, splits = merge.reapply(head, working, merged, {})
    (kept, new), = splits
    assert kept == X and new not in (X, Xk, Xk2, Y)
    assert heads_of(lines) == {X: "theirs", new: "mine", Y: f"body of {Y}"}
    assert ids(lines) == [Xk2, Y, X, Xk, new]


def test_reapply_puts_a_pending_add_after_the_merged_lines_as_written():
    odd = "not json at all"
    head, working = [row(X)], [row(X), row(Y), odd]
    merged = [row(X), row(Xk2)]
    assert merge.reapply(head, working, merged, {}) == ([row(X), row(Xk2), row(Y), odd], [])


def test_reapply_keeps_once_a_line_both_sides_rewrote_alike():
    # The same sed over the file on both sides: one line, not two heads on one id.
    x1 = row(X, kind="todo")
    x2 = x1.replace("todo", "task")
    said = []
    assert merge.reapply([x1], [x2], [x2], {}, dropped=said) == ([x2], [])
    assert said == []
