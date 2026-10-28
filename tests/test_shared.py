"""Shared stores: two people's agents on one store, a clone per side."""
import contextlib
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from symbion import api, cli, gitref, merge, store, summary
from symbion.config import Config


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def sym(clone, *args) -> int:
    return cli.main(["--dir", str(clone), *args])


def last_line(path) -> str:
    return Path(path).read_text(encoding="utf-8").splitlines()[-1]


def lines(path) -> list:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def sides(tmp_path, monkeypatch):
    """(a, b): two clones of one shared store, user.name A and B, a pushed
    origin between them."""
    monkeypatch.setenv("CLAUDECODE", "1")
    # conftest pins git's identity by env, which beats a clone's config.
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k)
    origin, a, b = tmp_path / "origin.git", tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    store.ensure_store(a)
    (a / "symbion.toml").write_text("shared = true\n", encoding="utf-8")
    git(a, "config", "user.name", "A")
    git(a, "config", "user.email", "a@a")
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "start")
    git(a, "remote", "add", "origin", str(origin))
    git(a, "push", "-q", "-u", "origin", "HEAD")
    subprocess.run(["git", "clone", "-q", str(origin), str(b)], check=True,
                   capture_output=True)
    git(b, "config", "user.name", "B")
    git(b, "config", "user.email", "b@b")
    return a, b


def test_a_write_in_a_shared_store_stamps_the_clones_side(sides):
    a, b = sides
    assert sym(a, "add", "--kind", "task", "--type", "project", "--body", "x") == 0
    assert json.loads(last_line(a / "notes.jsonl"))["side"] == "A"


def test_an_edit_stamps_the_editors_side_and_keeps_the_kept_versions(sides):
    a, b = sides
    assert sym(a, "add", "--kind", "task", "--type", "project", "--body", "x") == 0
    rid = json.loads(last_line(a / "notes.jsonl"))["id"]
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "a's row")
    git(a, "push", "-q")
    git(b, "pull", "-q", "--ff-only")
    assert sym(b, "supersede", rid, "--body", "y") == 0
    kept, head = lines(b / "notes.jsonl")
    assert (head["id"], head["side"], head["supersedes"]) == (rid, "B", kept["id"])
    assert kept["side"] == "A"


def test_an_arc_carries_its_creators_side(sides):
    a, b = sides
    assert sym(b, "arc", "create", "--name", "x", "--scope", "item") == 0
    assert json.loads(last_line(b / "arcs.jsonl"))["side"] == "B"


def test_a_seeded_row_carries_the_clones_side(sides):
    a, b = sides
    assert sym(b, "arc", "create", "--name", "x", "--scope", "item") == 0
    aid = json.loads(last_line(b / "arcs.jsonl"))["id"]
    assert sym(b, "arc", "seed", aid, "--scope", "item", "--name", "n") == 0
    assert json.loads(last_line(b / "notes.jsonl"))["side"] == "B"


def test_a_side_a_row_brings_is_overridden_in_a_shared_store(sides):
    a, b = sides
    [n] = store.add_many(b, [{"kind": "note", "target": {"type": "project", "name": None},
                              "side": "A"}])
    assert n.side == "B"


def test_a_store_without_shared_writes_no_side_key(tmp_store):
    assert sym(tmp_store, "add", "--kind", "task", "--type", "project", "--body", "x") == 0
    assert sym(tmp_store, "arc", "create", "--name", "x", "--scope", "item") == 0
    assert '"side"' not in (tmp_store / "notes.jsonl").read_text(encoding="utf-8")
    assert '"side"' not in (tmp_store / "arcs.jsonl").read_text(encoding="utf-8")


def test_an_edit_in_a_store_without_shared_keeps_the_side_it_inherited(tmp_store):
    """A store switched back off keeps what its rows hold: no stored byte moves."""
    row = {"id": "1", "kind": "note", "target": {"type": "project", "name": None},
           "created_at": "2026-01-01T00:00:00+00:00", "author": "u", "body": "x", "side": "A"}
    (tmp_store / "notes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert sym(tmp_store, "supersede", "1", "--body", "y") == 0
    assert lines(tmp_store / "notes.jsonl")[-1]["side"] == "A"


def test_a_shared_write_with_no_git_user_name_is_refused_by_name(sides, capsys):
    a, b = sides
    git(b, "config", "--unset", "user.name")
    before = (b / "notes.jsonl").read_bytes()
    assert sym(b, "add", "--kind", "task", "--type", "project", "--body", "x") != 0
    assert "config user.name NAME" in capsys.readouterr().err
    assert (b / "notes.jsonl").read_bytes() == before


def wrap(clone, gist) -> None:
    assert sym(clone, "commit", "--wrap", "-m", gist) == 0


def test_a_shared_store_commit_is_authored_by_the_clone(sides):
    a, b = sides
    assert sym(b, "add", "--kind", "task", "--type", "project", "--body", "x") == 0
    assert sym(b, "commit") == 0
    assert git(b, "log", "-1", "--format=%an <%ae>") == "B <b@b>"


def test_a_wrap_in_a_shared_store_carries_its_side(sides):
    a, b = sides
    wrap(b, "from B")
    assert git(b, "log", "-1", "--format=%(trailers:key=Side,valueonly)") == "B"
    keys = git(b, "log", "-1", "--format=%(trailers:only,keyonly)").split()
    assert [k.rstrip(":") for k in keys] == ["Wrap", "Side"]


def test_the_last_wrap_is_the_readers_side(sides):
    a, b = sides
    cfg = Config(project_root=a)
    wrap(a, "from A")
    git(a, "push", "-q")
    git(b, "pull", "-q", "--ff-only")
    wrap(b, "from B")
    git(b, "push", "-q")
    git(a, "pull", "-q", "--ff-only")
    assert gitref.last_wrap(a, cfg, side="A")["gist"] == "from A"
    assert gitref.last_wrap(a, cfg, side="B")["gist"] == "from B"
    assert gitref.last_wrap(a, cfg, side=None)["gist"] == "from B"
    assert gitref.last_wrap(a, cfg)["gist"] == "from B"


def test_a_wrap_with_no_side_is_every_sides_last_wrap(sides):
    a, b = sides
    cfg = Config(project_root=a)
    wrap(b, "from B")
    git(b, "commit", "--allow-empty", "-q", "-m", "older symbion", "-m", "Wrap: ada")
    for side in ("A", "B"):
        assert gitref.last_wrap(b, cfg, side=side)["gist"] == "older symbion"


def test_a_side_with_a_space_and_an_accent_round_trips_through_the_trailer(sides):
    a, b = sides
    cfg = Config(project_root=b)
    git(b, "config", "user.name", "Ana María")
    wrap(b, "from Ana")
    assert gitref.last_wrap(b, cfg, side="Ana María")["gist"] == "from Ana"
    assert gitref.last_wrap(b, cfg, side="Ana Maria") is None


def test_a_sides_name_reads_back_composed(sides):
    a, b = sides
    composed = "Ana María"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    # By file: git on macOS composes its own argv, so `config user.name` would not
    # leave the decomposed form on disk.
    with (b / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write(f"[user]\n\tname = {decomposed}\n")
    assert store.shared_side(b) == composed


def test_wraps_since_counts_each_sides_wraps(sides, monkeypatch):
    a, b = sides
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T00:00:00+00:00")
    wrap(a, "one")
    git(a, "commit", "--allow-empty", "-q", "-m", "older symbion", "-m", "Wrap: ada")
    monkeypatch.setenv("SYMBION_NOW", "2026-01-12T00:00:00+00:00")
    wrap(a, "two")
    wrap(a, "three")
    git(a, "push", "-q")
    git(b, "pull", "-q", "--ff-only")
    wrap(b, "four")
    assert gitref.wraps_since(b, None) == {"A": 3, "B": 1}
    assert gitref.wraps_since(b, "2026-01-11T00:00:00+00:00") == {"A": 2, "B": 1}
    assert gitref.wraps_since(b, "2026-02-01T00:00:00+00:00") == {}


def test_an_unshared_store_still_commits_as_git_name(tmp_store, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k)
    assert sym(tmp_store, "add", "--kind", "task", "--type", "project", "--body", "x") == 0
    wrap(tmp_store, "the day")
    assert git(tmp_store, "log", "-1", "--format=%an") == "symbion-notes"
    assert git(tmp_store, "log", "-1", "--format=%(trailers:key=Side,valueonly)") == ""


def summary_json(clone, capsys) -> dict:
    capsys.readouterr()
    assert sym(clone, "summary", "--json") == 0
    return json.loads(capsys.readouterr().out)


def test_the_summary_shows_the_readers_sides_wrap(sides, capsys):
    a, b = sides
    cfg = Config(project_root=a)
    wrap(a, "from A")
    git(a, "push", "-q")
    git(b, "pull", "-q", "--ff-only")
    wrap(b, "from B")
    git(b, "push", "-q")
    git(a, "pull", "-q", "--ff-only")
    assert summary.summary(a, cfg, side="A")["wrap"]["gist"] == "from A"
    assert summary.summary(a, cfg, side="B")["wrap"]["gist"] == "from B"
    assert summary_json(a, capsys)["wrap"]["gist"] == "from A"
    assert summary_json(b, capsys)["wrap"]["gist"] == "from B"


def test_the_summary_survives_a_clone_with_no_user_name(sides, capsys):
    a, b = sides
    wrap(b, "from B")
    git(b, "config", "--unset", "user.name")
    assert summary_json(b, capsys)["wrap"]["gist"] == "from B"


def add(clone, body) -> str:
    assert sym(clone, "add", "--kind", "task", "--type", "project", "--body", body) == 0
    return json.loads(last_line(clone / "notes.jsonl"))["id"]


def publish(clone, body) -> str:
    rid = add(clone, body)
    assert sym(clone, "commit", "--all") == 0
    git(clone, "push", "-q")
    return rid


def pending(clone) -> set:
    return {r["id"] for r in store.pending_rows(clone)}


def commit_here(clone, monkeypatch, *args) -> int:
    """`commit` that stays on this disk: it pulls and pushes in a shared
    store, and a test that needs a commit origin lacks must not run that."""
    with monkeypatch.context() as m:
        m.setattr(cli, "_pull_then_push", lambda ctx: None)
        return sym(clone, "commit", *args)


def test_pull_brings_the_other_sides_rows(sides, capsys):
    a, b = sides
    rid = publish(a, "from A")
    capsys.readouterr()
    assert sym(b, "pull") == 0
    out = capsys.readouterr().out
    assert "pulled 1 commit" in out and "older symbion" not in out
    assert not (b / ".git" / "symbion-pull").exists()            # the copy set aside is gone
    assert sym(b, "list", "--json") == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert (row["id"], row["side"]) == (rid, "A")


def test_pull_merges_concurrent_appends_without_a_conflict(sides):
    a, b = sides
    ra = publish(a, "from A")
    rb = add(b, "from B")
    assert sym(b, "commit") == 0
    assert sym(b, "pull") == 0
    ids = [r["id"] for r in lines(b / "notes.jsonl")]
    assert sorted(ids) == sorted([ra, rb])
    assert len(git(b, "log", "-1", "--format=%P").split()) == 2      # a merge commit
    assert pending(b) == set()


def test_a_pull_in_a_clone_whose_git_writes_crlf_merges_and_keeps_lf(sides):
    """Git for Windows sets core.autocrlf=true, so a clone checks notes.jsonl
    out with CRLF, and every pull there failed: git refused the merge over
    local changes to notes.jsonl (a Windows VM, 2026-10-28): git calls a file
    whose size differs from its checkout changed, unread."""
    a, b = sides
    git(b, "config", "core.autocrlf", "true")
    publish(a, "first")
    git(b, "pull", "-q")                           # git's own checkout, as a fresh clone's
    assert b"\r\n" in (b / "notes.jsonl").read_bytes()
    ra = publish(a, "from A")
    rb = add(b, "from B")
    assert sym(b, "pull") == 0
    assert {ra, rb} <= {r["id"] for r in lines(b / "notes.jsonl")}
    assert pending(b) == {rb}
    assert sym(b, "commit") == 0
    assert "\r" not in git(b, "show", "HEAD:notes.jsonl")


def test_another_sessions_pending_rows_survive_a_pull_and_stay_its_own(sides, monkeypatch):
    a, b = sides
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    p = add(b, "pending in S1")
    ra = publish(a, "from A")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    assert sym(b, "pull") == 0
    assert [r["id"] for r in lines(b / "notes.jsonl")] == [ra, p]
    assert pending(b) == {p}
    assert sym(b, "commit") == 1                       # S2 wrote none of it
    assert pending(b) == {p}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "commit") == 0
    assert pending(b) == set() and p in git(b, "show", "HEAD:notes.jsonl")


def test_a_pending_edit_the_pull_splits_stays_its_sessions(sides, monkeypatch, capsys):
    a, b = sides
    x = publish(a, "x1")
    assert sym(b, "pull") == 0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "supersede", x, "--body", "from B") == 0
    assert sym(a, "supersede", x, "--body", "from A") == 0
    assert sym(a, "commit", "--all") == 0
    git(a, "push", "-q")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    capsys.readouterr()
    assert sym(b, "pull") == 0
    out = capsys.readouterr().out
    rows = lines(b / "notes.jsonl")
    assert [r["body"] for r in rows if r["id"] == x] == ["from A"]
    (new,) = [r["id"] for r in rows if r["body"] == "from B"]
    assert f"both sides edited {x}: the upstream's keeps the id, yours is now {new}" in out
    assert sym(b, "commit") == 1                       # S2's: none
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "commit") == 0
    assert pending(b) == set()


def test_a_pending_edit_of_an_entry_the_merge_split_continues_that_entry(sides, monkeypatch):
    a, b = sides
    x = publish(a, "x1")
    assert sym(b, "pull") == 0
    assert sym(b, "supersede", x, "--body", "B1") == 0
    assert commit_here(b, monkeypatch, "--all") == 0    # committed, not pushed
    assert sym(a, "supersede", x, "--body", "A1") == 0
    assert sym(a, "commit", "--all") == 0
    git(a, "push", "-q")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "supersede", x, "--body", "B2") == 0  # pending, on B's committed head
    assert sym(b, "pull") == 0
    rows = lines(b / "notes.jsonl")
    assert len({r["id"] for r in rows}) == len(rows)
    later = {r.get("supersedes") for r in rows}
    heads = {r["body"]: r["id"] for r in rows if r["id"] not in later}
    assert sorted(heads) == ["A1", "B2"] and heads["A1"] == x
    assert sym(b, "commit") == 0                       # S1's edit, under its new id
    assert pending(b) == set()


def test_a_pull_over_a_hand_edited_symbion_toml_restores_every_file(sides, capsys):
    a, b = sides
    publish(a, "from A")
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    (b / "symbion.toml").write_text("shared = true\n# from B\n", encoding="utf-8")
    add(b, "pending")
    assert sym(b, "arc", "create", "--name", "x", "--scope", "item") == 0
    files = ("notes.jsonl", "arcs.jsonl", "symbion.toml")
    before = {f: (b / f).read_bytes() for f in files}
    head, status = git(b, "rev-parse", "HEAD"), git(b, "status", "--porcelain")
    capsys.readouterr()
    assert sym(b, "pull") == 1
    assert "changes not committed to: symbion.toml" in capsys.readouterr().err
    assert {f: (b / f).read_bytes() for f in files} == before
    assert (git(b, "rev-parse", "HEAD"), git(b, "status", "--porcelain")) == (head, status)


def test_a_pull_that_stops_on_a_conflict_restores_every_file(sides, capsys):
    a, b = sides
    publish(a, "from A")
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    (b / "symbion.toml").write_text("shared = true\n# from B\n", encoding="utf-8")
    git(b, "commit", "-qam", "toml from B")
    add(b, "pending")
    assert sym(b, "arc", "create", "--name", "x", "--scope", "item") == 0
    files = ("notes.jsonl", "arcs.jsonl", "symbion.toml")
    before = {f: (b / f).read_bytes() for f in files}
    head, status = git(b, "rev-parse", "HEAD"), git(b, "status", "--porcelain")
    capsys.readouterr()
    assert sym(b, "pull") == 1
    err = capsys.readouterr().err
    assert "git cannot merge: symbion.toml" in err
    # Every pending entry is this caller's: no --all, which takes other sessions' rows.
    assert f"{b.as_posix()} commit\n" in err and "--all" not in err, err
    assert {f: (b / f).read_bytes() for f in files} == before
    assert (git(b, "rev-parse", "HEAD"), git(b, "status", "--porcelain")) == (head, status)
    assert not (b / ".git" / "symbion-pull").exists()
    assert subprocess.run(["git", "-C", str(b), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode != 0


def test_a_pull_that_changes_symbion_toml_names_the_commit(sides, capsys):
    a, b = sides
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    sha = git(a, "rev-parse", "--short", "HEAD")
    capsys.readouterr()
    assert sym(b, "pull") == 0
    out = capsys.readouterr().out
    assert "symbion.toml changed" in out and sha in out
    (cmd,) = [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]
    shown = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    assert shown.returncode == 0 and "+# from A" in shown.stdout, cmd


def test_a_pull_flags_a_commit_by_an_older_symbion(sides, capsys):
    a, b = sides
    add(a, "from an older symbion")
    git(a, "add", "-A")
    git(a, "-c", "user.name=symbion-notes", "-c", "user.email=s@s", "commit", "-qm", "old")
    git(a, "push", "-q")
    capsys.readouterr()
    assert sym(b, "pull") == 0
    assert "older symbion" in capsys.readouterr().out


def test_a_pull_says_each_value_it_dropped(sides, capsys):
    a, b = sides
    assert sym(a, "arc", "create", "--name", "x", "--scope", "item") == 0
    aid = json.loads(last_line(a / "arcs.jsonl"))["id"]
    assert sym(a, "commit", "--all") == 0
    git(a, "push", "-q")
    assert sym(b, "pull") == 0
    assert sym(b, "arc", "rename", aid, "from B") == 0          # pending
    assert sym(a, "arc", "rename", aid, "from A") == 0
    assert sym(a, "commit", "--all") == 0
    git(a, "push", "-q")
    capsys.readouterr()
    assert sym(b, "pull") == 0
    out = capsys.readouterr().out
    assert "kept the upstream's name" in out and "from B" in out
    assert json.loads(last_line(b / "arcs.jsonl"))["name"] == "from A"


def test_a_pull_ends_the_undo_level(sides, capsys):
    a, b = sides
    rid = add(b, "mine")
    publish(a, "from A")
    assert sym(b, "pull") == 0
    capsys.readouterr()
    assert sym(b, "undo", rid) == 1
    assert "nothing to undo" in capsys.readouterr().err


def test_pull_waits_for_the_store_lock(sides):
    a, b = sides
    rid = publish(a, "from A")
    held, released = threading.Event(), []

    def hold():
        with store._lock(b, undoable=False):
            held.set()
            time.sleep(0.5)
            released.append(time.monotonic())
    t = threading.Thread(target=hold)
    t.start()
    held.wait()
    assert sym(b, "pull") == 0
    done = time.monotonic()
    t.join()
    assert released and done > released[0]
    assert rid in (b / "notes.jsonl").read_text(encoding="utf-8")


def test_pull_in_a_shared_store_with_no_origin_says_so(tmp_store, capsys):
    (tmp_store / "symbion.toml").write_text("shared = true\n", encoding="utf-8")
    git(tmp_store, "config", "user.name", "A")
    assert sym(tmp_store, "pull") == 1
    assert "no remote" in capsys.readouterr().err


def test_pull_in_a_store_that_is_not_shared_is_refused(tmp_store, capsys):
    assert sym(tmp_store, "pull") == 1
    assert "not shared" in capsys.readouterr().err


def test_fetch_behind_and_fetched_age(sides):
    a, b = sides
    assert gitref.fetched_age(b) is None
    publish(a, "from A")
    assert gitref.behind(b) == 0                        # nothing fetched yet
    assert gitref.fetch(b, timeout=10) is None
    assert gitref.behind(b) == 1 and 0 <= gitref.fetched_age(b) < 60
    git(b, "remote", "set-url", "origin", str(b.parent / "nowhere.git"))
    said = gitref.fetch(b, timeout=10)
    assert said and "\n" not in said
    git(b, "branch", "--unset-upstream")
    assert gitref.behind(b) is None


def _state(clone):
    return ({f: (clone / f).read_bytes() for f in ("notes.jsonl", "arcs.jsonl")},
            git(clone, "rev-parse", "HEAD"), git(clone, "status", "--porcelain"))


def test_a_merge_git_stops_with_no_conflict_changes_nothing(sides, capsys, monkeypatch):
    a, b = sides
    publish(a, "from A")
    add(b, "from B")
    assert commit_here(b, monkeypatch) == 0            # so the merge is no fast-forward
    add(b, "pending")
    assert sym(b, "arc", "create", "--name", "x", "--scope", "item") == 0
    hook = b / ".git" / "hooks" / "pre-merge-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'hook: no merges' >&2\nexit 1\n")
    hook.chmod(0o755)
    was = _state(b)
    capsys.readouterr()
    assert sym(b, "pull") == 1
    err = capsys.readouterr().err
    assert "git refused the merge" in err and "hook: no merges" in err
    assert _state(b) == was


@pytest.mark.parametrize("where", ["reapply", "_forget_undo"])
def test_a_pull_cut_off_after_its_merge_puts_everything_back(sides, monkeypatch, where):
    # Before the pending rows go back on, and after: the merge is taken back either way.
    a, b = sides
    publish(a, "from A")
    add(b, "pending")
    assert sym(b, "arc", "create", "--name", "x", "--scope", "item") == 0
    was = _state(b)

    class Cut(Exception):
        pass

    def cut(*args, **kw):
        raise Cut
    monkeypatch.setattr(merge if where == "reapply" else store, where, cut)
    with pytest.raises(Cut):
        sym(b, "pull")
    assert _state(b) == was


def test_a_pull_leaves_a_merge_someone_stopped_in(sides, capsys, monkeypatch):
    a, b = sides
    publish(a, "from A")
    add(b, "from B")
    assert commit_here(b, monkeypatch) == 0
    git(b, "fetch", "-q")
    # By hand and with no driver yet, as git merges without symbion: both appended.
    assert subprocess.run(["git", "-C", str(b), "merge", "origin/HEAD"],
                          capture_output=True).returncode != 0
    merging = git(b, "rev-parse", "MERGE_HEAD")
    capsys.readouterr()
    assert sym(b, "pull") == 1
    assert "middle of a merge" in capsys.readouterr().err
    assert git(b, "rev-parse", "MERGE_HEAD") == merging


@pytest.mark.skipif(os.name != "posix", reason="a process group is POSIX's; Windows kills git alone")
def test_a_fetch_past_its_timeout_stops_the_remotes_children_too(sides, tmp_path, monkeypatch):
    a, b = sides
    pidfile = tmp_path / "child.pid"
    ssh = tmp_path / "slow-ssh"
    # An ssh that leaves a child holding git's stderr, as a prompting ssh does.
    ssh.write_text('#!/bin/sh\nsleep 30 &\necho $! > "$PIDFILE"\nwait\n', encoding="utf-8")
    ssh.chmod(0o755)
    for k, v in (("GIT_SSH_COMMAND", str(ssh)), ("GIT_SSH_VARIANT", "simple"),
                 ("PIDFILE", str(pidfile))):
        monkeypatch.setenv(k, v)
    git(b, "remote", "set-url", "origin", "ssh://nohost/x.git")
    started = time.monotonic()
    said = gitref.fetch(b, timeout=0.5)
    try:
        assert "took over 0.5s" in said and time.monotonic() - started < 5
        pid = int(pidfile.read_text(encoding="utf-8"))

        def alive():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True
        until = time.monotonic() + 3
        while alive() and time.monotonic() < until:
            time.sleep(0.05)
        assert not alive()
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pidfile.read_text(encoding="utf-8")), signal.SIGKILL)


def test_a_timeout_on_windows_stops_the_whole_process_tree(monkeypatch):
    # kill() stops git alone there, and git's child (ssh.exe) holds the pipe open
    class Windows:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)
    monkeypatch.setattr(gitref, "os", Windows())
    calls = []

    def run(argv, *args, **kw):
        calls.append((argv, kw))
        raise FileNotFoundError(argv[0])             # a taskkill that fails is ignored
    monkeypatch.setattr(subprocess, "run", run)
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        gitref.run_timed([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    assert time.monotonic() - began < 5
    ((argv, kw),) = calls
    assert argv[0].endswith(os.path.join("System32", "taskkill.exe")), argv[0]
    assert argv[1:4] == ["/F", "/T", "/PID"] and int(argv[4]) != os.getpid()
    assert kw["timeout"] <= 5


def _killed_pull(clone, at):
    """`pull` in a child that dies with no cleanup at `at`, as a kill leaves it:
    `reapply`, after the merge made its commit; `abort`, with git's merge
    stopped; `_keep_sessions` or `drop`, with the rows written back and the
    copy whole; `tombstone`, part way through deleting the copy."""
    code = f"""
import os, shutil, subprocess, sys
from symbion import cli, merge, store
real, rmtree, replace = subprocess.run, shutil.rmtree, os.replace
def run(cmd, *a, **k):
    if {at!r} == "abort" and "--abort" in cmd:
        os._exit(9)
    return real(cmd, *a, **k)
def gone(path, *a, **k):
    if {at!r} == "tombstone" and str(path).endswith(("symbion-pull", "symbion-pull.gone")) \\
            and os.path.isdir(path):
        os.unlink(os.path.join(path, "notes.jsonl"))
        os._exit(9)
    return rmtree(path, *a, **k)
def moved(src, dst):
    if {at!r} == "drop" and str(src).endswith("symbion-pull"):
        os._exit(9)
    return replace(src, dst)
subprocess.run, shutil.rmtree, os.replace = run, gone, moved
if {at!r} == "reapply":
    merge.reapply = lambda *a, **k: os._exit(9)
if {at!r} == "_keep_sessions":
    store._keep_sessions = lambda *a, **k: os._exit(9)
sys.exit(cli.main(["--dir", {str(clone)!r}, "pull"]))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 9, r.stdout + r.stderr


@pytest.mark.parametrize("via", ["pull", "merge_upstream"])
def test_a_pull_killed_after_its_merge_is_put_back_by_the_next(sides, monkeypatch, capsys, via):
    a, b = sides
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    p = add(b, "pending in S1")
    ra = publish(a, "from A")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    head = git(b, "rev-parse", "HEAD")
    _killed_pull(b, "reapply")
    aside = b / ".git" / "symbion-pull"
    assert aside.is_dir() and git(b, "rev-parse", "HEAD") != head    # merged, then killed
    assert p not in (b / "notes.jsonl").read_text(encoding="utf-8")
    capsys.readouterr()
    if via == "pull":                    # the upstream is merged already: nothing to fetch
        assert sym(b, "pull") == 0
        assert "cut off" in capsys.readouterr().out
    else:
        up = git(b, "rev-parse", "--symbolic-full-name", "@{u}")
        assert "cut off" in store.merge_upstream(b, up)["recovered"]
    assert not aside.exists()
    assert [r["id"] for r in lines(b / "notes.jsonl")] == [ra, p]
    assert pending(b) == {p}
    capsys.readouterr()
    assert sym(b, "undo", p) == 1                      # the put-back ends the undo level
    assert "nothing to undo" in capsys.readouterr().err
    assert sym(b, "commit") == 1                       # S2's: none
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "commit") == 0 and pending(b) == set()


@pytest.mark.parametrize("origin", ["up", "down"])
def test_a_pull_killed_after_merging_a_changed_symbion_toml_still_names_it(sides, capsys, origin):
    a, b = sides
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    sha = git(a, "rev-parse", "--short", "HEAD")
    _killed_pull(b, "reapply")                         # merged, then killed before it said so
    if origin == "down":                               # the next fetch fails: said all the same
        git(b, "remote", "set-url", "origin", str(b.parent / "nowhere.git"))
    capsys.readouterr()
    assert sym(b, "pull") == (0 if origin == "up" else 1)
    out = capsys.readouterr().out
    assert "cut off" in out and f"symbion.toml changed in {sha} by A" in out, out
    (cmd,) = [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]
    shown = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    assert shown.returncode == 0 and "+# from A" in shown.stdout, cmd


def test_session_start_says_what_a_killed_pull_brought_when_the_lock_skips_its_merge(
        sides, capsys, monkeypatch):
    a, b = sides
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    sha = git(a, "rev-parse", "--short", "HEAD")
    _killed_pull(b, "reapply")
    publish(a, "after the kill")                       # so session start's pull has a merge

    def busy(*args, **kw):
        raise store.Busy
    monkeypatch.setattr(store, "merge_upstream", busy)
    text, line = hook(b, capsys)
    assert "cut off" in text and f"symbion.toml changed in {sha} by A" in text, text
    assert "skipped" in text and f"symbion.toml changed in {sha} by A" in line, (text, line)
    assert not (b / ".git" / "symbion-pull").exists()


def test_a_row_a_killed_pull_splits_on_its_way_back_stays_its_sessions(sides, monkeypatch, capsys):
    a, b = sides
    x = publish(a, "x1")
    assert sym(b, "pull") == 0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "supersede", x, "--body", "from B") == 0
    assert sym(a, "supersede", x, "--body", "from A") == 0
    assert sym(a, "commit", "--all") == 0
    git(a, "push", "-q")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    _killed_pull(b, "reapply")
    capsys.readouterr()
    assert sym(b, "pull") == 0
    out = capsys.readouterr().out
    rows = lines(b / "notes.jsonl")
    (new,) = [r["id"] for r in rows if r["body"] == "from B"]
    assert "cut off" in out and f"yours is now {new}" in out
    assert [r["body"] for r in rows if r["id"] == x] == ["from A"]
    assert sym(b, "commit") == 1                       # S2's: none
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "commit") == 0 and pending(b) == set()


def _both_commit(a, b, name):
    """`name` committed differently on each side, A's pushed: a merge stops on it."""
    for clone in (a, b):
        (clone / name).parent.mkdir(exist_ok=True)
        (clone / name).write_text(f"shared = true\n# from {clone.name}\n", encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", f"{name} from {clone.name}")
    git(a, "push", "-q")


def test_a_pull_leaves_a_merge_a_killed_pull_stopped_to_whoever_finishes_it(
        sides, monkeypatch, capsys):
    a, b = sides
    publish(a, "from A")
    _both_commit(a, b, "docs/plan.md")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    p = add(b, "pending in S1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    _killed_pull(b, "abort")                           # dies with git's merge stopped
    aside = b / ".git" / "symbion-pull"
    # A person picks up the stopped merge and resolves it, staged, not committed.
    (b / "docs" / "plan.md").write_text("resolved by hand\n", encoding="utf-8")
    git(b, "add", "docs/plan.md")
    merging = git(b, "rev-parse", "MERGE_HEAD")
    capsys.readouterr()
    assert sym(b, "pull") == 1                         # as a session start's pull runs
    err = capsys.readouterr().err
    assert "middle of a merge" in err and "a cut-off pull set aside come back" in err, err
    assert (b / "docs" / "plan.md").read_text(encoding="utf-8") == "resolved by hand\n"
    assert git(b, "show", ":docs/plan.md") == "resolved by hand"
    assert git(b, "rev-parse", "MERGE_HEAD") == merging and aside.is_dir()
    git(b, "commit", "-q", "--no-edit")                # the person finishes it
    capsys.readouterr()
    assert sym(b, "pull") == 0
    assert "cut off" in capsys.readouterr().out
    assert not aside.exists() and pending(b) == {p}
    assert git(b, "show", "HEAD:docs/plan.md") == "resolved by hand"
    assert sym(b, "commit") == 1                       # S2's: none
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "commit") == 0 and pending(b) == set()


def test_a_pull_killed_in_a_merge_stopped_on_symbion_toml_says_how_to_go_on(sides, capsys):
    a, b = sides
    publish(a, "from A")
    _both_commit(a, b, "symbion.toml")
    p = add(b, "pending")
    _killed_pull(b, "abort")                           # the file now holds git's markers
    capsys.readouterr()
    assert sym(b, "list") == 1                         # every command stops at the file
    err = capsys.readouterr().err
    assert "could not parse" in err and "finish it" in err and "puts back" in err, err
    git(b, "merge", "--abort")
    assert sym(b, "pull") == 1                         # the conflict stands
    assert "cut off" in capsys.readouterr().out
    assert pending(b) == {p}


@pytest.mark.parametrize("at", ["_keep_sessions", "drop"])
def test_a_pull_killed_once_the_rows_are_back_neither_loses_nor_doubles_them(
        sides, monkeypatch, at):
    a, b = sides
    x = publish(a, "x1")
    assert sym(b, "pull") == 0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "supersede", x, "--body", "from B") == 0   # split by the pull below
    assert sym(a, "supersede", x, "--body", "from A") == 0
    assert sym(a, "commit", "--all") == 0
    git(a, "push", "-q")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S2")
    _killed_pull(b, at)
    assert sym(b, "pull") == 0
    rows = lines(b / "notes.jsonl")
    assert [r["body"] for r in rows].count("from B") == 1
    assert len({r["id"] for r in rows}) == len(rows)
    assert sym(b, "commit") == 1                       # S2's: none
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S1")
    assert sym(b, "commit") == 0 and pending(b) == set()


def test_an_upstream_rename_of_a_file_changed_here_is_named_from_plumbing(sides, capsys):
    a, b = sides
    (a / "docs").mkdir(exist_ok=True)
    (a / "docs" / "a.md").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "doc")
    git(a, "push", "-q")
    assert sym(b, "pull") == 0
    git(a, "mv", "docs/a.md", "docs/b.md")
    git(a, "commit", "-qm", "rename")
    git(a, "push", "-q")
    (b / "docs" / "a.md").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")
    add(b, "pending")
    was = _state(b)
    capsys.readouterr()
    assert sym(b, "pull") == 1
    assert "changes not committed to: docs/a.md" in capsys.readouterr().err
    assert _state(b) == was


@pytest.mark.parametrize("fails", ["reset", "abort"])
def test_a_rollback_git_refuses_keeps_the_rows_set_aside_and_says_so(
        sides, monkeypatch, capsys, fails):
    a, b = sides
    publish(a, "from A")
    if fails == "abort":
        _both_commit(a, b, "docs/plan.md")             # the merge stops; its rollback aborts it
    p = add(b, "pending")
    aside = b / ".git" / "symbion-pull"
    real = subprocess.run

    def run(cmd, *args, **kw):
        if ("--abort" in cmd) if fails == "abort" else ("reset" in cmd and "--merge" in cmd):
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: simulated\n")
        return real(cmd, *args, **kw)

    def cut(*args, **kw):
        raise KeyError("cut")
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", run)
        if fails == "reset":
            m.setattr(merge, "reapply", cut)           # after the merge made its commit
        capsys.readouterr()
        assert sym(b, "pull") == 1
    err = capsys.readouterr().err
    assert "fatal: simulated" in err and "this pull set aside come back" in err, err
    assert aside.is_dir() and p not in (b / "notes.jsonl").read_text(encoding="utf-8")
    if fails == "abort":
        git(b, "merge", "--abort")                     # one of the two ways the error names
    assert sym(b, "pull") == (0 if fails == "reset" else 1)
    assert not aside.exists() and pending(b) == {p}


@pytest.mark.parametrize("deleted", [False, True], ids=["untracked", "and-a-staged-deletion"])
def test_the_commands_a_refused_pull_prints_run_as_printed(sides, capsys, deleted):
    a, b = sides
    if deleted:                                        # b deletes what the upstream edits
        (a / "docs").mkdir(exist_ok=True)
        (a / "docs" / "x.md").write_text("x\n", encoding="utf-8")
        git(a, "add", "-A")
        git(a, "commit", "-qm", "x")
        git(a, "push", "-q")
        assert sym(b, "pull") == 0
        (a / "docs" / "x.md").write_text("x, edited\n", encoding="utf-8")
        git(b, "rm", "-q", "docs/x.md")
    for clone in (a, b):                               # b's copy is untracked, in the way
        (clone / "docs").mkdir(exist_ok=True)
        (clone / "docs" / "new.md").write_text("same\n", encoding="utf-8")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "doc")
    git(a, "push", "-q")
    p = add(b, "pending")
    capsys.readouterr()
    assert sym(b, "pull") == 1
    err = capsys.readouterr().err
    cmds = [shlex.split(ln) for ln in err.splitlines() if ln.startswith("  ")]
    assert cmds and cmds[-1][0] == "symbion" and cmds[-1][-1] == "pull", err
    assert [c[4:] for c in cmds if c[3:4] == ["add"]] == [["--", "docs/new.md"]], err
    for argv in cmds[:-1]:
        done = subprocess.run(argv, capture_output=True, text=True)
        assert done.returncode == 0, (argv, done.stderr)
    assert cli.main(cmds[-1][1:]) == (1 if deleted else 0)
    if deleted:                                        # the merge's own conflict, now
        assert "git cannot merge: docs/x.md" in capsys.readouterr().err
    assert pending(b) == {p}


def test_a_pull_killed_while_it_deletes_its_copy_puts_nothing_back(sides, capsys):
    a, b = sides
    old = publish(b, "committed in B")
    git(a, "pull", "-q")
    ra = publish(a, "from A")
    p = add(b, "pending")
    _killed_pull(b, "tombstone")
    assert not (b / ".git" / "symbion-pull").exists()
    capsys.readouterr()
    assert sym(b, "pull") == 0
    assert "cut off" not in capsys.readouterr().out
    assert [r["id"] for r in lines(b / "notes.jsonl")] == [old, ra, p]
    assert pending(b) == {p}
    publish(a, "later")
    assert sym(b, "pull") == 0                         # its copy clears the one left
    assert not (b / ".git" / "symbion-pull.gone").exists()


@pytest.mark.parametrize("lost", ["notes.jsonl", "HEAD", "holds", "the commit HEAD names"])
def test_a_copy_that_is_not_whole_is_named_and_nothing_is_put_back(sides, capsys, lost):
    a, b = sides
    publish(b, "committed in B")
    git(a, "pull", "-q")
    publish(a, "from A")
    add(b, "pending")
    _killed_pull(b, "reapply")
    aside = b / ".git" / "symbion-pull"
    if lost == "the commit HEAD names":                # by hand, or a disk's damage
        (aside / "HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    else:
        (aside / lost).unlink()
    was = _state(b)
    capsys.readouterr()
    assert sym(b, "pull") == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "not whole" in err and str(aside) in err, err
    assert _state(b) == was and aside.is_dir()


def hook(clone, capsys) -> tuple[str, str]:
    """`summary --hook` in a clone: the text the agent reads, and the person's line."""
    capsys.readouterr()
    assert sym(clone, "summary", "--hook") == 0
    out = json.loads(capsys.readouterr().out)
    return out["hookSpecificOutput"]["additionalContext"], out["systemMessage"]


def test_session_start_pulls_in_a_shared_store(sides, capsys):
    a, b = sides
    rid = publish(a, "from A")
    text, line = hook(b, capsys)
    assert text.splitlines()[0].startswith("pulled 1 commit from origin/"), text
    assert "open outside arcs" in text and line.startswith("symbion") and "pulled" not in line
    assert rid in (b / "notes.jsonl").read_text(encoding="utf-8")
    assert git(a, "rev-parse", "HEAD") in git(b, "log", "--format=%H").split()


def test_session_start_says_nothing_when_origin_has_nothing_new(sides, capsys):
    a, b = sides
    publish(a, "from A")
    hook(b, capsys)
    text, _ = hook(b, capsys)
    assert text.startswith("symbion") and "pulled" not in text, text


def test_session_start_with_origin_gone_still_prints_the_summary(sides, capsys):
    a, b = sides
    # gone from its URL: Windows refuses to delete git's read-only object files
    (b.parent / "origin.git").rename(b.parent / "gone.git")
    began = time.monotonic()
    text, line = hook(b, capsys)
    # A fetch that fails at once never waits out its timeout; a loaded box takes a second or two.
    assert time.monotonic() - began < cli.HOOK_FETCH_TIMEOUT
    first, _, rest = text.partition("\n")
    assert first.startswith("pull: git fetch origin failed"), text
    assert first.endswith("; the summary below reads the local store"), first
    assert rest.startswith("symbion") and line.startswith("symbion"), (rest, line)


def test_session_start_with_no_git_user_name_still_prints(sides, capsys):
    a, b = sides
    git(b, "config", "--unset", "user.name")
    text, _ = hook(b, capsys)
    assert "config user.name" in text.splitlines()[0] and "open outside arcs" in text


@pytest.mark.parametrize("raised, said", [(RuntimeError("boom"), "boom"),
                                           (OSError(), "OSError")])   # no message: its type
def test_session_start_survives_a_pull_that_raises(sides, capsys, monkeypatch, raised, said):
    a, b = sides

    def boom(*args, **kw):
        raise raised
    monkeypatch.setattr(cli.api, "pull", boom)
    text, _ = hook(b, capsys)
    assert text.splitlines()[0] == f"pull: {said}; the summary below reads the local store"
    assert "open outside arcs" in text


def test_session_start_shows_the_person_a_changed_symbion_toml(sides, capsys):
    a, b = sides
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    sha = git(a, "rev-parse", "--short", "HEAD")
    text, line = hook(b, capsys)
    told = [ln for ln in text.splitlines() if "symbion.toml" in ln]    # the notice, its command
    assert len(told) == 2 and sha in told[0], text
    assert line.splitlines()[:2] == told and line.splitlines()[2].startswith("symbion"), line


def test_session_start_shows_the_person_an_older_client(sides, capsys, monkeypatch):
    a, b = sides
    monkeypatch.setattr(cli.api, "pull", lambda ctx, **kw: api.Pulled(
        merged=1, upstream="origin/main", old_head="0123456789abcdef", old_client=True))
    _, line = hook(b, capsys)
    first, rest = line.split("\n")
    assert first.startswith("a clone runs an older symbion") and rest.startswith("symbion"), line


def _holding_the_lock(clone, seconds):
    """(thread, release): a thread that holds the store's lock, as another
    symbion command does, for `seconds` or until `release` is set. It has
    the lock once this returns."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with store._lock(clone, undoable=False):
            held.set()
            release.wait(seconds)
    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(10)
    return t, release


def test_session_start_skips_a_pull_another_command_holds_the_store_for(sides, capsys,
                                                                         monkeypatch):
    a, b = sides
    ra = publish(a, "from A")
    assert cli.HOOK_LOCK_WAIT + cli.HOOK_FETCH_TIMEOUT < cli.HOOK_TIMEOUT
    monkeypatch.setattr(cli, "HOOK_LOCK_WAIT", 0.5)
    t, release = _holding_the_lock(b, cli.HOOK_TIMEOUT + 1)   # a pull that waits it out is late
    try:
        began = time.monotonic()
        text, line = hook(b, capsys)
        took = time.monotonic() - began
    finally:
        release.set()
        t.join()
    first, _, rest = text.partition("\n")
    assert first == (f"pull: skipped: another symbion command holds the store; pull once it "
                     f"is done: symbion --dir {b.as_posix()} pull"), text
    assert rest.startswith("symbion") and line.startswith("symbion"), (rest, line)
    assert took < cli.HOOK_TIMEOUT and ra not in (b / "notes.jsonl").read_text(encoding="utf-8")


def test_pull_waits_on_the_lock_another_command_holds(sides, capsys, monkeypatch):
    a, b = sides
    ra = publish(a, "from A")
    monkeypatch.setattr(cli, "HOOK_LOCK_WAIT", 0.1)      # session start's wait, not pull's
    t, _ = _holding_the_lock(b, 1)
    try:
        assert sym(b, "pull") == 0
    finally:
        t.join()
    assert ra in (b / "notes.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize("argv", [("commit",), ("commit", "--wrap", "-m", "the handoff")])
def test_commit_in_a_shared_store_pulls_then_pushes(sides, capsys, argv):
    """A wrap's commit too, as wrap.md says."""
    a, b = sides
    ra = publish(a, "from A")
    rb = add(b, "from B")
    capsys.readouterr()
    assert sym(b, *argv) == 0
    out = capsys.readouterr().out
    assert "pulled 1 commit" in out and "pushed 2 commits to origin" in out, out
    assert gitref.unpushed(b) == 0
    held = git(b.parent / "origin.git", "show", f"{git(b, 'branch', '--show-current')}:notes.jsonl")
    assert ra in held and rb in held


def test_a_rejected_push_says_so_with_the_retry(sides, capsys):
    a, b = sides
    refuse = b.parent / "origin.git" / "hooks" / "pre-receive"
    refuse.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    refuse.chmod(0o755)
    add(b, "x")
    capsys.readouterr()
    assert sym(b, "commit") == 0
    out = capsys.readouterr().out
    assert "push failed" in out and f"symbion --dir {b.as_posix()} push" in out, out
    assert gitref.unpushed(b) == 1


def test_commit_with_a_conflicting_pull_commits_and_holds_the_push(sides, capsys):
    a, b = sides
    (a / "symbion.toml").write_text("shared = true\n# from A\n", encoding="utf-8")
    git(a, "commit", "-qam", "toml from A")
    git(a, "push", "-q")
    (b / "symbion.toml").write_text("shared = true\n# from B\n", encoding="utf-8")
    rb = add(b, "a row")
    capsys.readouterr()
    assert sym(b, "commit") == 0
    out = capsys.readouterr().out
    assert rb in git(b, "show", "HEAD:notes.jsonl") and gitref.unpushed(b) >= 1
    assert "symbion.toml" in out and "push waits on the merge" in out, out
    held = git(b.parent / "origin.git", "show", f"{git(b, 'branch', '--show-current')}:notes.jsonl")
    assert rb not in held


def test_a_write_says_when_origin_moved_and_never_merges(sides, capsys):
    a, b = sides
    assert gitref.fetch(b, timeout=10) is None
    fetch_head = b / ".git" / "FETCH_HEAD"
    old = time.time() - 700
    os.utime(fetch_head, (old, old))
    publish(a, "from A")
    head = git(b, "rev-parse", "HEAD")
    capsys.readouterr()
    add(b, "from B")
    err = capsys.readouterr().err
    assert f"origin has 1 new store commit: symbion --dir {b.as_posix()} pull\n" in err, err
    assert git(b, "rev-parse", "HEAD") == head


def test_a_write_skips_the_fetch_inside_the_interval(sides, monkeypatch, capsys):
    a, b = sides
    assert gitref.fetch(b, timeout=10) is None
    publish(a, "from A")
    called = []
    monkeypatch.setattr(gitref, "fetch", lambda *args, **kw: called.append(args))
    capsys.readouterr()
    add(b, "from B")
    assert called == [] and "origin has" not in capsys.readouterr().err


def test_a_write_survives_a_check_that_raises(sides, monkeypatch, capsys):
    a, b = sides

    def boom(*args, **kw):
        raise OSError("boom")
    monkeypatch.setattr(gitref, "fetch", boom)
    capsys.readouterr()
    add(b, "from B")
    assert "origin has" not in capsys.readouterr().err


def test_an_unshared_store_never_fetches(tmp_store, monkeypatch, capsys):
    origin = tmp_store.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(tmp_store, "remote", "add", "origin", str(origin))
    called = []
    monkeypatch.setattr(gitref, "fetch", lambda *args, **kw: called.append(args) or "refused")
    capsys.readouterr()
    assert sym(tmp_store, "summary", "--hook") == 0
    assert "pull:" not in capsys.readouterr().out
    add(tmp_store, "x")
    assert sym(tmp_store, "commit") == 0
    out = capsys.readouterr().out
    assert called == [] and "1 commit not on origin: symbion" in out and "pushed" not in out


def test_session_start_cuts_the_sync_block_at_the_cap(sides, capsys, monkeypatch):
    a, b = sides
    pairs = [(f"k{i}", f"n{i}") for i in range(8)]
    monkeypatch.setattr(cli.api, "pull", lambda ctx, **kw: api.Pulled(
        merged=1, upstream="origin/main", splits=pairs))
    text, _ = hook(b, capsys)
    said = text.splitlines()
    assert said[:cli.SYNC_CAP] == [merge.said_split(*x) for x in pairs[:cli.SYNC_CAP]]
    assert said[cli.SYNC_CAP] == "+3 more not shown"
    assert said[cli.SYNC_CAP + 1] == "pulled 1 commit from origin/main"      # not a split: never cut
    assert said[cli.SYNC_CAP + 2].startswith("symbion")


def test_the_cap_never_hides_the_toml_notice_or_the_older_client_line(sides, capsys, monkeypatch):
    a, b = sides
    monkeypatch.setattr(cli.api, "pull", lambda ctx, **kw: api.Pulled(
        merged=1, upstream="origin/main", old_head="0123456789abcdef", old_client=True,
        toml_changed="abc1234 by B", splits=[(f"k{i}", f"n{i}") for i in range(6)]))
    text, _ = hook(b, capsys)
    said = text.splitlines()
    assert said[cli.SYNC_CAP] == "+1 more not shown"
    assert "pulled 1 commit from origin/main" in said
    assert "symbion.toml changed in abc1234 by B: it names commands symbion runs, so read it:" in said
    assert f"  git -C {shlex.quote(str(b))} diff 0123456789ab HEAD -- symbion.toml" in said
    assert any(ln.startswith("a clone runs an older symbion") for ln in said)


def test_session_start_cuts_only_the_splits_of_a_pull_that_stopped(sides, capsys, monkeypatch):
    a, b = sides
    monkeypatch.setattr(cli.api, "pull", lambda ctx, **kw: api.Pulled(
        conflicts=["symbion.toml"], upstream="origin/main",
        splits=[(f"k{i}", f"n{i}") for i in range(8)]))
    text, _ = hook(b, capsys)
    said = text.splitlines()
    assert said[cli.SYNC_CAP] == "+3 more not shown"
    assert f"  git -C {shlex.quote(str(b))} commit" in said       # the last command that goes on


def test_the_by_hand_merge_quotes_the_upstream_for_the_shell(sides, capsys, monkeypatch):
    a, b = sides
    monkeypatch.setattr(cli.api, "pull", lambda ctx, **kw: api.Pulled(
        conflicts=["symbion.toml"], upstream="origin/x;y"))      # a branch name may hold `;`
    capsys.readouterr()
    assert sym(b, "pull") == 1
    assert f"  git -C {shlex.quote(str(b))} merge 'origin/x;y'\n" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="a process group is POSIX's; Windows kills git alone")
def test_session_start_stops_a_hung_fetch_before_the_hook_is_stopped(sides, tmp_path, capsys):
    a, b = sides
    slow = tmp_path / "slow-upload-pack"
    slow.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    slow.chmod(0o755)
    git(b, "config", "remote.origin.uploadpack", str(slow))
    assert cli.HOOK_FETCH_TIMEOUT < cli.HOOK_TIMEOUT
    began = time.monotonic()
    text, line = hook(b, capsys)
    assert time.monotonic() - began < cli.HOOK_TIMEOUT
    first, _, rest = text.partition("\n")
    assert first == (f"pull: git fetch origin took over {cli.HOOK_FETCH_TIMEOUT:g}s and was "
                     f"stopped; the summary below reads the local store")
    assert rest.startswith("symbion") and line.startswith("symbion"), (rest, line)


def test_a_failed_fetch_starts_the_interval_too(sides, monkeypatch):
    a, b = sides
    calls = []
    # A fetch that fails before git truncates FETCH_HEAD leaves it as it was
    # (git 2.55 truncates it first, so a real failure would not show this).
    monkeypatch.setattr(gitref, "fetch", lambda *args, **kw: calls.append(args) or "git fetch origin failed: offline")
    assert gitref.fetched_age(b) is None
    add(b, "one")
    add(b, "two")
    assert len(calls) == 1


def test_the_fetch_age_is_the_newer_of_the_fetch_and_the_last_try(sides):
    a, b = sides
    assert gitref.fetch(b, timeout=10) is None
    gitref.stamp_fetch(b)
    fetch_head, tried = b / ".git" / "FETCH_HEAD", b / ".git" / "symbion-fetched"
    old = time.time() - 700
    os.utime(tried, (old, old))
    assert gitref.fetched_age(b) < 60                  # the fetch is newer
    os.utime(tried, None)
    os.utime(fetch_head, (old, old))
    assert gitref.fetched_age(b) < 60                  # the try is newer
    os.utime(tried, (old, old))
    assert 600 < gitref.fetched_age(b) < 800


def _child_pid(pidfile) -> int:
    """The pid a hook wrote, waited for: git may be slow to start it."""
    until = time.monotonic() + 5
    while time.monotonic() < until:
        with contextlib.suppress(FileNotFoundError, ValueError):
            return int(pidfile.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError(f"{pidfile} was never written")


def _stopped(pid) -> bool:
    until = time.monotonic() + 3
    while time.monotonic() < until:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="a process group is POSIX's; Windows kills git alone")
def test_a_commit_whose_push_hangs_stops_it_and_names_the_retry(sides, tmp_path, monkeypatch, capsys):
    a, b = sides
    pidfile = tmp_path / "child.pid"
    slow = b / ".git" / "hooks" / "pre-push"
    slow.parent.mkdir(exist_ok=True)
    # A hook that leaves a child holding git's pipes, as a prompting ssh does.
    slow.write_text(f'#!/bin/sh\nsleep 60 &\necho $! > "{pidfile}"\nwait\n', encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setattr(cli, "PUSH_TIMEOUT", 3)                # time enough for a loaded box to start the hook
    add(b, "x")
    capsys.readouterr()
    started = time.monotonic()
    try:
        assert sym(b, "commit") == 0
        out = capsys.readouterr().out
        assert time.monotonic() - started < 30                # the hook sleeps 60
        assert "push took over 3s" in out and f"symbion --dir {b.as_posix()} push" in out, out
        assert gitref.unpushed(b) == 1
        assert _stopped(_child_pid(pidfile))
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pidfile.read_text(encoding="utf-8")), signal.SIGKILL)


def test_the_push_verb_has_no_timeout(sides, monkeypatch):
    a, b = sides
    add(b, "x")
    assert commit_here(b, monkeypatch) == 0
    called = []
    real = gitref.run_timed
    monkeypatch.setattr(gitref, "run_timed", lambda *args, **kw: called.append(args) or real(*args, **kw))
    assert sym(b, "push") == 0
    assert called == [] and gitref.unpushed(b) == 0


def test_a_push_origin_moved_under_names_the_pull_that_mends_it(sides, capsys, monkeypatch):
    a, b = sides
    ra = add(a, "from A")
    assert commit_here(a, monkeypatch, "--all") == 0
    # Origin moves between b's pull and b's push: a's push runs in b's pre-push hook.
    hook_file = b / ".git" / "hooks" / "pre-push"
    hook_file.parent.mkdir(exist_ok=True)
    hook_file.write_text(f'#!/bin/sh\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\n'
                         f'git -C "{a.as_posix()}" push -q\n', encoding="utf-8")
    hook_file.chmod(0o755)
    rb = add(b, "from B")
    capsys.readouterr()
    assert sym(b, "commit") == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("push failed"))
    cmds = [shlex.split(c) for c in line.split("; retry: ", 1)[1].split(", then ")]
    assert [(c[0], c[-1]) for c in cmds] == [("symbion", "pull"), ("symbion", "push")], line
    assert gitref.unpushed(b) == 1
    for argv in cmds:                                          # as printed
        assert cli.main(argv[1:]) == 0, argv
    assert gitref.unpushed(b) == 0
    held = git(b.parent / "origin.git", "show", f"{git(b, 'branch', '--show-current')}:notes.jsonl")
    assert ra in held and rb in held


def test_a_commit_whose_pull_raises_is_made_and_exits_zero(sides, capsys, monkeypatch):
    a, b = sides

    def boom(*args, **kw):
        raise RuntimeError("boom\nsecond line")
    monkeypatch.setattr(cli.api, "pull", boom)
    rb = add(b, "from B")
    capsys.readouterr()
    assert sym(b, "commit") == 0
    out = capsys.readouterr().out
    assert rb in git(b, "show", "HEAD:notes.jsonl") and gitref.unpushed(b) == 1
    assert f"not pushed: boom second line; the push waits on symbion --dir {b.as_posix()} pull" in out, out


def test_the_other_sides_open_rows_stay_out_of_session_start(sides, capsys):
    a, b = sides
    add(a, "a's own task")
    assert sym(a, "commit") == 0
    before = summary_json(a, capsys)
    assert sym(b, "pull") == 0
    rid = add(b, "b's task")
    assert sym(b, "commit") == 0
    text, _ = hook(a, capsys)
    assert rid in (a / "notes.jsonl").read_text(encoding="utf-8"), "the hook pulled it"
    assert "b's task" not in text and rid not in text, text
    after = summary_json(a, capsys)
    assert rid not in json.dumps(after)
    assert (after["open"], after["open_in_arcs"]) == (before["open"], before["open_in_arcs"])


def test_your_row_the_other_side_amended_waits_on_you(sides, capsys):
    a, b = sides
    rid = add(a, "a's question")
    assert sym(a, "commit") == 0
    assert sym(b, "pull") == 0
    assert sym(b, "supersede", rid, "--append", "--body", "b's reply") == 0
    assert sym(b, "commit") == 0
    assert sym(a, "pull") == 0
    d = summary_json(a, capsys)
    [w] = d["waiting"]
    assert (w["id"], "from" in w, w["last"]) == (rid, False, "claude (B)")
    assert rid not in {h["id"] for h in d["heads"]}


def test_the_nudge_counts_the_other_sides_rows_and_wraps_since_your_wrap(sides, capsys,
                                                                         monkeypatch):
    a, b = sides
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:00:00+00:00")
    add(b, "before a's wrap")
    wrap(b, "b's first")
    monkeypatch.setenv("SYMBION_NOW", "2026-01-12T12:00:00+00:00")
    wrap(a, "a's handoff")
    monkeypatch.setenv("SYMBION_NOW", "2026-01-14T12:00:00+00:00")
    assert sym(b, "pull") == 0
    one = add(b, "one")
    add(b, "two")
    assert sym(b, "commit") == 0
    # Two more versions of one entry: the line counts what its command lists, entries.
    assert sym(b, "supersede", one, "--append", "--body", "more") == 0
    assert sym(b, "commit") == 0
    assert sym(b, "supersede", one, "--append", "--body", "still more") == 0
    wrap(b, "b's second")
    assert [r["body"].startswith("one") for r in lines(b / "notes.jsonl")].count(True) == 3
    add(a, "a's own, after its wrap")
    text, _ = hook(a, capsys)
    assert (f"  other side, B: 2 rows and 1 wrap since your last wrap: "
            f"symbion --dir {a.as_posix()} list --side B --since wrap") in text.splitlines(), text
    assert summary_json(a, capsys)["others"] == [
        {"side": "B", "rows": 2, "wraps": 1, "since": "2026-01-12T12:00:00+00:00"}]


def test_the_nudge_quotes_a_side_with_a_space(sides, capsys):
    a, b = sides
    git(b, "config", "user.name", "Ana María")
    add(b, "from Ana")
    assert sym(b, "commit") == 0
    text, _ = hook(a, capsys)
    assert (f"  other side, Ana María: 1 row since the store began: "
            f"symbion --dir {a.as_posix()} list --side 'Ana María'") in text.splitlines(), text


def test_the_other_sides_arc_shows_only_while_your_side_has_an_open_box_in_it(sides, capsys):
    """Each arc listed counts both sides' boxes. Another side's arc lists while
    a box of yours in it is open: `N open in arcs` counts that box."""
    a, b = sides

    def arc(clone, name) -> str:
        assert sym(clone, "arc", "create", "--name", name, "--scope", "item") == 0
        return json.loads(last_line(clone / "arcs.jsonl"))["id"]

    def seed(clone, aid, name) -> str:
        assert sym(clone, "arc", "seed", aid, "--scope", "item", "--name", name) == 0
        return json.loads(last_line(clone / "notes.jsonl"))["id"]
    ours = arc(a, "alpha")
    assert sym(a, "commit", "--all") == 0
    assert sym(b, "pull") == 0
    theirs, quiet = arc(b, "beta"), arc(b, "gamma")
    seed(b, ours, "b-in-alpha")
    seed(b, ours, "b2-in-alpha")
    seed(b, theirs, "b-in-beta")
    seed(b, quiet, "b-in-gamma")
    assert sym(b, "commit", "--all") == 0
    assert sym(a, "pull") == 0
    seed(a, ours, "a-in-alpha")
    mine = seed(a, theirs, "a-in-beta")
    d = summary_json(a, capsys)
    assert [(r["id"], r["done"], r["total"]) for r in d["arcs"]] == [(ours, 0, 3), (theirs, 0, 2)]
    assert d["open_in_arcs"] == 2, "a's two boxes, in either side's arc"
    assert sum(d["open"].values()) == 0 and d["heads"] == []
    assert sym(a, "resolve", mine) == 0
    d = summary_json(a, capsys)
    assert [(r["id"], r["done"], r["total"]) for r in d["arcs"]] == [(ours, 0, 3)]
    assert d["open_in_arcs"] == 1


def test_a_row_with_no_side_reads_on_both_sides(sides, capsys):
    a, b = sides
    toml = a / "symbion.toml"
    toml.write_text("", encoding="utf-8")
    rid = add(a, "from before the switch")
    toml.write_text("shared = true\n", encoding="utf-8")
    assert "side" not in json.loads(last_line(a / "notes.jsonl"))
    assert sym(a, "commit") == 0
    assert sym(b, "pull") == 0
    for clone in (a, b):
        assert rid in {h["id"] for h in summary_json(clone, capsys)["heads"]}, clone


def test_an_unshared_summary_has_no_sync_or_other_side_lines(tmp_store, capsys):
    """A store switched back off keeps its rows' sides, and reads them all as its own."""
    row = {"id": "20260110-120000-000000-abc", "kind": "task",
           "target": {"type": "project", "name": None}, "created_at": "2026-01-10T12:00:00+00:00",
           "author": "human", "body": "written on side B", "status": "open", "side": "B"}
    (tmp_store / "notes.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    d = summary_json(tmp_store, capsys)
    assert (d["sync"], d["others"], d["others_elided"]) == ([], [], 0)
    assert [(w["id"], w["from"]) for w in d["waiting"]] == [(row["id"], "human")]
    text, _ = hook(tmp_store, capsys)
    assert "other side," not in text and "written on side B" in text, text


def head_line(out, rid) -> str:
    return next(ln for ln in out.splitlines() if rid in ln.split())


def written_line(out, rid) -> str:
    ls = out.splitlines()
    return ls[ls.index(head_line(out, rid)) + 1]


def test_news_after_a_write_leaves_out_the_other_side(sides, capsys, monkeypatch):
    """The first write after a pull printed the other side's rows as news."""
    a, b = sides
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:00:00+00:00")
    publish(a, "a's first")
    assert sym(b, "pull") == 0
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:05:00+00:00")
    for body in ("b one", "b two", "b three"):
        assert sym(b, "add", "--kind", "task", "--type", "project", "--body", body,
                   "--author", "codex") == 0
    assert sym(b, "commit", "--all") == 0
    assert sym(a, "pull") == 0
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:10:00+00:00")
    capsys.readouterr()
    add(a, "a's second")
    err = capsys.readouterr().err
    assert "written by someone else" not in err and "b one" not in err, err


def test_the_other_sides_agent_does_not_move_the_news_cutoff(sides, capsys, monkeypatch):
    """Both sides' agents are `claude`: B's write read as this reader's own,
    and its time hid what the user wrote on this side before it."""
    a, b = sides
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:00:00+00:00")
    add(a, "a's first")
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:02:00+00:00")
    assert sym(a, "add", "--kind", "task", "--type", "project", "--body", "from the user",
               "--author", "human") == 0
    assert sym(a, "commit", "--all") == 0
    assert sym(b, "pull") == 0
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:05:00+00:00")
    add(b, "b's agent")
    assert sym(b, "commit") == 0
    assert sym(a, "pull") == 0
    monkeypatch.setenv("SYMBION_NOW", "2026-01-10T12:10:00+00:00")
    capsys.readouterr()
    add(a, "a's second")
    err = capsys.readouterr().err
    assert "from the user" in err and "b's agent" not in err, err


@pytest.fixture
def both(sides, capsys):
    """(a, b, mine, theirs): a's row and b's, both in a's clone."""
    a, b = sides
    mine = add(a, "a's task")
    assert sym(a, "commit") == 0
    assert sym(b, "pull") == 0
    theirs = add(b, "b's task")
    assert sym(b, "commit") == 0
    assert sym(a, "pull") == 0
    capsys.readouterr()
    return a, b, mine, theirs


@pytest.mark.parametrize("args", [("list", "--full"), ("context",)])
def test_a_query_labels_the_other_sides_rows(both, capsys, args):
    a, b, mine, theirs = both
    assert sym(a, *args) == 0
    out = capsys.readouterr().out
    assert written_line(out, theirs).endswith(" by claude (B)"), out
    assert written_line(out, mine).endswith(" by claude"), out


def test_a_terminal_labels_the_other_sides_rows_too(both, capsys, monkeypatch):
    a, b, mine, theirs = both
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert sym(a, "list", "--full") == 0
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert f"{theirs} · written" in out and " by claude (B)" in out, out
    assert out.count(" by claude (B)") == 1 and out.count(" by claude") == 2, out


def test_a_terminals_short_list_marks_the_other_sides_rows(both, capsys, monkeypatch):
    # It prints no author, so a row's side is said on its line
    a, b, mine, theirs = both
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert sym(a, "list") == 0
    screen = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).splitlines()
    (row_b,) = [ln for ln in screen if ln.startswith(theirs[-10:])]
    (row_a,) = [ln for ln in screen if ln.startswith(mine[-10:])]
    assert "  side B" in row_b and "side" not in row_a, screen


def test_show_labels_the_other_sides_row(both, capsys):
    a, b, mine, theirs = both
    assert sym(a, "show", theirs) == 0
    assert written_line(capsys.readouterr().out, theirs).endswith(" by claude (B)")


def test_list_side_keeps_one_side(both, capsys):
    a, b, mine, theirs = both
    for side, rid in (("B", theirs), ("A", mine)):
        assert sym(a, "list", "--side", side, "--json") == 0
        assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [rid]


def test_list_side_names_itself_in_the_header(both, capsys):
    a, b, mine, theirs = both
    assert sym(a, "list", "--side", "B") == 0
    assert "--side B" in capsys.readouterr().out.splitlines()[0]


def test_list_side_counts_superseded_rows_of_that_side_only(both, capsys):
    """Rows of the other side were counted as superseded: `+1 superseded (--all)`
    on a store where nothing was."""
    a, b, mine, theirs = both
    assert sym(a, "list", "--side", "B") == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "superseded" not in header, header
    assert sym(a, "resolve", mine, "--body", "done") == 0     # a's row has an older version
    capsys.readouterr()
    assert sym(a, "list", "--side", "B", "--json") == 0
    assert capsys.readouterr().err == ""
    assert sym(a, "list", "--side", "B") == 0
    assert "superseded" not in capsys.readouterr().out.splitlines()[0]
    assert sym(b, "supersede", theirs, "--body", "now this") == 0     # b's has one too
    assert sym(b, "commit") == 0
    assert sym(a, "pull") == 0
    capsys.readouterr()
    assert sym(a, "list", "--side", "B") == 0
    assert "+1 superseded (--all)" in capsys.readouterr().out.splitlines()[0]
    assert sym(a, "list", "--side", "B", "--grep", "b's task", "--json") == 0
    assert "1 entry matches only in an older version (--all)" in capsys.readouterr().err


def test_list_side_in_an_unshared_store_is_refused(tmp_store, capsys):
    assert sym(tmp_store, "list", "--side", "B") == 2
    assert "--side needs shared = true in symbion.toml" in capsys.readouterr().err


def test_the_agent_line_gains_side_at_its_end(both, capsys):
    a, b, mine, theirs = both
    assert sym(a, "list") == 0
    seen = head_line(capsys.readouterr().out, theirs)
    assert sym(b, "list") == 0
    own = head_line(capsys.readouterr().out, theirs)
    assert "side=" not in own and seen == own + " side=B", (seen, own)
    assert sym(a, "list") == 0
    assert "side=" not in head_line(capsys.readouterr().out, mine)


def test_a_read_with_no_git_user_name_still_prints(both, capsys):
    """Only a write refuses a clone with no user.name, and it names the fix."""
    a, b, mine, theirs = both
    git(a, "config", "--unset", "user.name")
    for args in (("list",), ("context",), ("show", mine), ("list", "--json")):
        assert sym(a, *args) == 0, args
        assert capsys.readouterr().out, args


def test_the_agent_line_quotes_a_side_with_a_space(sides, capsys):
    a, b = sides
    git(b, "config", "user.name", "Ana María")
    rid = add(b, "from Ana")
    assert sym(b, "commit") == 0
    assert sym(a, "pull") == 0
    capsys.readouterr()
    assert sym(a, "list") == 0
    assert shlex.split(head_line(capsys.readouterr().out, rid))[-1] == "side=Ana María"


def test_json_keeps_the_stored_author(both, capsys):
    a, b, mine, theirs = both
    assert sym(a, "list", "--json") == 0
    row = next(r for r in json.loads(capsys.readouterr().out) if r["id"] == theirs)
    assert (row["author"], row["raised_by"], row["side"]) == ("claude", "claude", "B")
    assert sym(a, "context", "--json") == 0
    row = next(r for r in json.loads(capsys.readouterr().out)["notes"] if r["id"] == theirs)
    assert (row["author"], row["raised_by"], row["side"]) == ("claude", "claude", "B")


def test_the_other_sides_gotcha_still_fires_with_its_side(sides, monkeypatch, capsys):
    a, b = sides
    store.add(b, kind="note", target={"type": "file", "name": "src/serve.py"}, author="claude",
              tags=["gotcha"], body="Start a scratch serve first.")
    assert sym(b, "commit", "--all") == 0
    assert sym(a, "pull") == 0
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "s1"}')))
    assert sym(a, "summary", "--hook") == 0
    (line,) = (Path.home() / ".cache" / "symbion" / "gotchas" / "s1").read_text(
        encoding="utf-8").splitlines()
    assert line.split("\t")[1] == "src/serve.py"
    assert "Start a scratch serve first." in line and "by claude (B)" in line, line


def wrap_at(clone, when, monkeypatch) -> None:
    monkeypatch.setenv("SYMBION_NOW", when)
    wrap(clone, f"wrap at {when}")


def test_list_since_wrap_is_the_readers_sides_wrap(sides, capsys, monkeypatch):
    """The other side's newer wrap narrowed the window and dropped this side's rows."""
    a, b = sides
    wrap_at(a, "2026-01-10T12:00:00+00:00", monkeypatch)
    monkeypatch.setenv("SYMBION_NOW", "2026-01-11T12:00:00+00:00")
    mine = add(a, "a's, after its wrap")
    assert sym(a, "commit") == 0
    assert sym(b, "pull") == 0
    wrap_at(b, "2026-01-12T12:00:00+00:00", monkeypatch)
    assert sym(a, "pull") == 0
    capsys.readouterr()
    assert sym(a, "list", "--since", "wrap", "--json") == 0
    assert mine in {r["id"] for r in json.loads(capsys.readouterr().out)}


def test_the_nudges_command_lists_the_rows_it_counted(sides, capsys, monkeypatch):
    a, b = sides
    wrap_at(a, "2026-01-10T12:00:00+00:00", monkeypatch)
    assert sym(b, "pull") == 0
    monkeypatch.setenv("SYMBION_NOW", "2026-01-11T12:00:00+00:00")
    add(b, "before b's wrap")
    add(b, "also before")
    assert sym(b, "commit") == 0
    wrap_at(b, "2026-01-12T12:00:00+00:00", monkeypatch)
    monkeypatch.setenv("SYMBION_NOW", "2026-01-13T12:00:00+00:00")
    add(b, "after b's wrap")
    assert sym(b, "commit") == 0
    text, _ = hook(a, capsys)
    line = next(ln for ln in text.splitlines() if ln.startswith("  other side, B:"))
    counted = int(line.split(": ")[1].split()[0])
    cmd = shlex.split(line.split("since your last wrap: ")[1])
    assert cmd[:3] == ["symbion", "--dir", a.as_posix()] and counted == 3, line
    capsys.readouterr()
    assert cli.main([*cmd[1:], "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == counted
