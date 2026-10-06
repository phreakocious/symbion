import os
import subprocess
import pytest
from symbion import catalog, config, gitref
from symbion import kinds as K
from symbion.config import Config


def _run(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "p"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    for i in range(2):
        (r / f"f{i}").write_text("x")
        _run(r, "add", "-A")
        _run(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"c{i}")
    return r


def test_symbolic_refs_freeze_to_a_full_object_id(repo):
    cfg = Config(project_root=repo)
    head = _run(repo, "rev-parse", "HEAD")
    assert gitref.canonical_commit(cfg, "HEAD") == head
    assert gitref.canonical_commit(cfg, "main") == head
    assert gitref.canonical_commit(cfg, head[:7]) == head


def test_a_tree_id_is_rejected_not_stored_as_a_commit(repo):
    """Plain `rev-parse --verify` accepts HEAD^{tree} and returns a tree id,
    which would be stored as a commit and never match anything."""
    cfg = Config(project_root=repo)
    tree = _run(repo, "rev-parse", "HEAD^{tree}")
    assert gitref.canonical_commit(cfg, tree) == tree, "miss falls back to input"
    assert gitref.canonical_commit(cfg, "HEAD^{tree}") == "HEAD^{tree}"


def test_clean_check_at_head_is_current(repo):
    cfg = Config(project_root=repo)
    prov = gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])
    assert prov["dirty"] is False
    assert gitref.check_state(cfg, prov) == ("current", 0)


def test_dirty_check_at_head_is_never_current(repo):
    """The dirty case SATISFIES sha == HEAD, so without precedence it renders
    `current` — vouching for a verification against a tree state that was
    never committed."""
    (repo / "scratch").write_text("uncommitted")
    cfg = Config(project_root=repo)
    prov = gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])
    assert prov["dirty"] is True
    state, _ = gitref.check_state(cfg, prov)
    assert state == "unverifiable"


def test_no_stamp_reads_unstamped_and_a_stamp_without_a_commit_unverifiable(repo):
    """A row from a store that predates stamping has no provenance: nothing
    about it is suspect, it is only unplaced against the tree. A stamp that
    names no commit (a provenance command's own object) still claims a run
    symbion cannot place."""
    cfg = Config(project_root=repo)
    assert gitref.check_state(cfg, None) == ("unstamped", None)
    assert gitref.check_state(cfg, {}) == ("unstamped", None)
    assert gitref.check_state(cfg, {"runs": 3}) == ("unverifiable", None)


def test_a_stamp_is_a_commit_id_never_a_ref(repo):
    """An unborn branch once stamped the literal `HEAD`, and `cat-file -e`
    resolves a ref as readily as a sha, so the row read `behind 0` for good.
    An abbreviated id (a configured provenance command may print one) read
    `behind 0` at HEAD for the same reason: it never equalled the full id."""
    cfg = Config(project_root=repo)
    for ref in ("HEAD", "main"):
        assert gitref.check_state(cfg, {"sha": ref, "dirty": False}) == ("unverifiable", None)
    head = _run(repo, "rev-parse", "HEAD")
    assert gitref.check_state(cfg, {"sha": head[:12], "dirty": False}) == ("current", 0)


def test_staleness_distance_is_exactly_one_after_one_commit(repo):
    """Asserting `> 0` after fifty commits passes on any non-zero
    implementation and catches no off-by-one."""
    cfg = Config(project_root=repo)
    prov = gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])
    (repo / "next").write_text("y")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c2")
    assert gitref.check_state(cfg, prov) == ("behind", 1)


def test_missing_commit_is_unverifiable(repo):
    cfg = Config(project_root=repo)
    assert gitref.check_state(cfg, {"sha": "0" * 40, "dirty": False}) \
        == ("unverifiable", None)


def test_a_stamp_sha_that_is_not_a_string_is_unverifiable(repo):
    """A provenance command's object is the project's schema, stored as
    printed: `{"sha": 123}` made the hex check raise TypeError, and every
    `list` showing that row printed a traceback."""
    cfg = Config(project_root=repo)
    for sha in (123, 1.5, True, ["abc1234"], {"a": 1}):
        assert gitref.check_state(cfg, {"sha": sha}) == ("unverifiable", None), sha


def test_commit_that_exists_but_is_not_an_ancestor_of_head_is_diverged(repo):
    """A commit reachable in the repo (so cat-file -e succeeds) with neither
    it nor HEAD an ancestor of the other — a branch tip never merged back,
    beside a main that moved on — is a distinct line of development, not
    `unverifiable`'s rev-parse failure, not `current`, and not `ahead`.

    BOTH sides need their own commit. A topic branch off a main that has not
    moved is `ahead` by every definition git has, and the fixture that left
    main where it was asserted `diverged` about it — passing on the code that
    collapsed the two states."""
    cfg = Config(project_root=repo)
    _run(repo, "checkout", "-q", "-b", "topic")
    (repo / "topic_only").write_text("z")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "topic-commit")
    topic_sha = _run(repo, "rev-parse", "HEAD")
    _run(repo, "checkout", "-q", "main")
    (repo / "main_only").write_text("m")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "main-commit")
    assert gitref.check_state(cfg, {"sha": topic_sha, "dirty": False}) == ("diverged", None)


def test_a_stamp_this_checkout_has_not_reached_is_ahead_not_diverged(repo):
    """The stamp is a DESCENDANT of HEAD: the same line of development, read
    from a checkout that lags it. `merge-base --is-ancestor sha HEAD` fails
    there too, so it read `diverged` — "another line of development" about
    the one line. Measured 2026-09-28: a project keeping `main` in its own
    worktree stamped a dated baseline there, and every checkout behind it
    read that baseline as diverged."""
    cfg = Config(project_root=repo)
    behind = _run(repo, "rev-parse", "HEAD")
    for i in (2, 3):
        (repo / f"f{i}").write_text("x")
        _run(repo, "add", "-A")
        _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"c{i}")
    tip = _run(repo, "rev-parse", "HEAD")
    _run(repo, "checkout", "-q", behind)
    assert gitref.check_state(cfg, {"sha": tip, "dirty": False}) == ("ahead", 2)


def test_a_git_error_reads_unverifiable_not_diverged(repo):
    """`merge-base --is-ancestor` exits 1 for "not an ancestor" and 128 when
    git cannot answer. Reading every nonzero exit as "not an ancestor" showed
    a commit object missing between the stamp and HEAD as `diverged`, another
    line of development, about the one line (2026-09-29)."""
    cfg = Config(project_root=repo)
    stamp, mid = _run(repo, "rev-parse", "HEAD~1"), _run(repo, "rev-parse", "HEAD")
    (repo / "f2").write_text("x")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c2")
    prov = {"sha": stamp, "dirty": False}
    assert gitref.check_state(cfg, prov) == ("behind", 2)      # the path is reached
    obj = repo / ".git" / "objects" / mid[:2] / mid[2:]
    obj.chmod(0o644)                  # git writes it read-only; Windows refuses that unlink
    obj.unlink()
    # This process keeps what git answered: the two commits still relate so.
    assert gitref.check_state(cfg, prov) == ("behind", 2)
    gitref._RELATIONS.clear()                                  # a fresh process
    assert gitref.check_state(cfg, prov) == ("unverifiable", None)


def test_rows_read_against_one_head_ask_git_once_per_stamp(repo, monkeypatch):
    """A GUI page of 75 check badges ran about 93 git calls, three quarters
    of its render (2026-10-01): four a row, HEAD among them every time. Read
    against one HEAD, each distinct stamp costs one call, and a second read
    at that HEAD costs none."""
    cfg = Config(project_root=repo)
    first = _run(repo, "rev-parse", "HEAD")
    for i in (2, 3):
        (repo / f"f{i}").write_text("x")
        _run(repo, "add", "-A")
        _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"c{i}")
    second = _run(repo, "rev-parse", "HEAD~1")
    calls = []
    real = gitref._git
    monkeypatch.setattr(gitref, "_git", lambda c, *a: calls.append(a[0]) or real(c, *a))
    provs = [{"sha": s, "dirty": False} for s in (first, second, first, second, first)]
    head = gitref.head_sha(cfg)
    assert [gitref.check_state(cfg, p, head) for p in provs] == \
        [("behind", 2), ("behind", 1), ("behind", 2), ("behind", 1), ("behind", 2)]
    assert calls == ["rev-parse", "rev-list", "rev-list"], calls
    calls.clear()
    assert gitref.check_state(cfg, provs[0], gitref.head_sha(cfg)) == ("behind", 2)
    assert calls == ["rev-parse"], calls


def test_a_catalog_miss_says_when_the_path_is_on_the_default_branch(repo, capsys):
    """A catalog runs in the checkout you are in, so a row about a file only
    the default branch holds missed the catalog and was taken as typed, with
    only the generic note — indistinguishable from a typo (2026-09-28).

    The other two shapes stay quiet, and they are why the probe tests the
    worktree too: a path that IS here but that a FILTERING catalog
    (`git ls-files '*.py'`) leaves out is not missing from this worktree, and
    a name on no branch at all is an ordinary miss."""
    base = _run(repo, "rev-parse", "HEAD")
    (repo / "only_main.py").write_text("x")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "main-only")
    _run(repo, "checkout", "-q", "-b", "wip", base)
    assert not (repo / "only_main.py").exists()

    cfg = Config(project_root=repo, catalogs={"file": "true"})
    assert gitref.only_on_default_branch(cfg, "only_main.py") == "main"
    assert gitref.only_on_default_branch(cfg, "f0") is None, "in this worktree"
    assert gitref.only_on_default_branch(cfg, "nowhere.py") is None, "on no branch"

    catalog.match(cfg, "file", "only_main.py", ["f0", "f1"])
    err = capsys.readouterr().err
    assert "taken as typed; it is on main, not in this worktree" in err, err
    catalog.match(cfg, "file", "nowhere.py", ["f0", "f1"])
    err = capsys.readouterr().err
    assert "taken as typed\n" in err and "worktree" not in err, err


def test_provenance_is_stamped_only_for_verdict_kinds(repo):
    cfg = Config(project_root=repo)
    assert gitref.provenance_stamp(cfg, K.Kind()) is None
    assert gitref.provenance_stamp(cfg, K.Kind(status=True)) is None
    assert gitref.provenance_stamp(cfg, K.Kind(status=True, verdict=True))["sha"]


def test_provenance_override_runs_in_project_root(repo):
    """The override runs with cwd=project_root: a relative-path read only
    resolves if the command's cwd is set correctly."""
    (repo / "marker.json").write_text('{"sha": "deadbeef", "dirty": false}')
    cfg = Config(project_root=repo, provenance_command="cat marker.json")
    assert gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"]) == {"sha": "deadbeef", "dirty": False}


@pytest.mark.parametrize("printed", ["not-json", "", "[]", '"a string"'])
def test_a_provenance_override_that_prints_no_object_refuses(repo, printed):
    """Output that was not JSON stamped `provenance: null` at exit 0, and the
    row read `unverifiable` for good; a JSON non-object was stored as-is. An
    object with no "sha" is a project's own schema (design spec): kept."""
    cfg = Config(project_root=repo, provenance_command=f"echo '{printed}'")
    with pytest.raises(ValueError, match=r"provenance command .* printed .*not a JSON object"):
        gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])
    cfg = Config(project_root=repo, provenance_command="""echo '{"schema": 7}'""")
    assert gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"]) == {"schema": 7}


def test_provenance_override_timeout_raises_catalog_error(repo):
    """Proves the override is routed through catalog.run_configured and not a
    bare subprocess.run: a bare subprocess.run on the override has no
    timeout at all, so it would hang for the full 5s and never raise;
    run_configured's bounded timeout is what turns the hang into
    CatalogError."""
    cfg = Config(project_root=repo, provenance_command="sleep 5", command_timeout=1)
    with pytest.raises(catalog.CatalogError):
        gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])


def test_branch_commits_returns_commits_since_default_branch(repo):
    cfg = Config(project_root=repo, default_branch="main")
    _run(repo, "checkout", "-q", "-b", "topic")
    (repo / "topic_file").write_text("z")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "topic1")
    topic_head = _run(repo, "rev-parse", "HEAD")
    assert gitref.branch_commits(cfg, "topic") == {topic_head}


@pytest.mark.parametrize("ref, base", [("no-such-ref", "main"), ("topic", "master")])
def test_branch_commits_refuses_a_ref_git_cannot_read(repo, ref, base):
    """A failed rev-list returned an empty set, so `context --branch
    no-such-ref`, or any branch in a repo whose default is not the
    configured `main`, read `0 notes` at exit 0 (doc claims audit,
    2026-09-27). The error names the range and where the base comes from."""
    _run(repo, "branch", "topic")
    cfg = Config(project_root=repo, default_branch=base)
    with pytest.raises(ValueError, match=rf"git cannot list {base}\.\.{ref}: .*--since"):
        gitref.branch_commits(cfg, ref)
    assert gitref.branch_commits(cfg, "topic", since="topic") == set(), "a real empty range"


def test_subjects_maps_sha_to_subject_line(repo):
    cfg = Config(project_root=repo)
    head = _run(repo, "rev-parse", "HEAD")
    assert gitref.subjects(cfg, [head]) == {head: "c1"}


def test_subjects_batch_with_one_missing_sha_still_resolves_the_valid_ones(repo):
    """A batch with one unresolvable sha (rebased away, squashed, shallow
    clone) must not blank the subjects of the other, valid shas in the same
    call — `git log --no-walk` without --ignore-missing exits 128 with EMPTY
    stdout on a batch containing any bad sha, which would silently return {}
    for the whole batch."""
    cfg = Config(project_root=repo)
    head = _run(repo, "rev-parse", "HEAD")
    parent = _run(repo, "rev-parse", "HEAD~1")
    bogus = "0" * 40
    result = gitref.subjects(cfg, [head, bogus, parent])
    assert result == {head: "c1", parent: "c0"}


def test_uncommitted_counts_appended_rows_exactly(tmp_path):
    """`git status --porcelain` cannot do this: 200 appended notes are still
    one changed path."""
    from symbion import store
    store.ensure_store(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-q", "-m", "base"], check=True)
    for i in range(3):
        store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    assert gitref.uncommitted(tmp_path) == (3, False)


def test_uncommitted_is_zero_on_a_clean_store(tmp_path):
    """The direction that catches a probe stuck saying 'dirty'."""
    from symbion import store
    store.ensure_store(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-q", "-m", "base"], check=True)
    assert gitref.uncommitted(tmp_path) == (0, False)


def test_a_stash_is_not_an_unpushed_commit(tmp_path):
    """`--all` counts refs/stash: one stash read as 2 unpushed, and the GUI's
    push button stayed after every push, which sent nothing."""
    from symbion import store
    store.ensure_store(tmp_path)
    bare = tmp_path / "origin.git"
    git = lambda *a, cwd=tmp_path: subprocess.run(                     # noqa: E731
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
        check=True, capture_output=True)
    git("init", "-q", "--bare", str(bare))
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("remote", "add", "origin", str(bare))
    git("push", "-q", "origin", "HEAD")
    store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    git("stash", "-q")
    assert gitref.unpushed(tmp_path) == 0


def test_uncommitted_counts_untracked_notes_file(tmp_path):
    """Before any commit exists, notes.jsonl is untracked — `git diff`
    ignores untracked files entirely, so the count must come from the whole
    line count, not the numstat diff."""
    from symbion import store
    store.ensure_store(tmp_path)
    for i in range(3):
        store.add(tmp_path, kind="note", target={"type": "project", "name": None})
    assert gitref.uncommitted(tmp_path) == (3, False)


# ---- linked worktree: HEAD/branch/dirty are per-worktree, project_root isn't ----
#
# Every fixture above creates a plain repo where the current worktree IS the
# main worktree, so project_root and "the checkout HEAD actually belongs to"
# are the same path and this class of bug is invisible to them. These add a
# SECOND, linked worktree with a commit and a dirty file the main checkout
# does not have, so project_root (still correctly the main checkout) and the
# root that ref/state resolution needs (the linked checkout) provably differ.
#
# Measured 2026-09-04 from a real linked worktree of this repo: `symbion add
# --kind check --type commit --name HEAD` stored the MAIN checkout's HEAD
# while the linked worktree's actual HEAD differed.

@pytest.fixture
def linked(repo):
    """A linked worktree of `repo`, branched off HEAD, carrying one commit
    `repo` does not have."""
    wt = repo.parent / "wt1"
    _run(repo, "worktree", "add", "-q", "-b", "topic", str(wt), "HEAD")
    (wt / "topic_only").write_text("z")
    _run(wt, "add", "-A")
    _run(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "topic-commit")
    return wt


def test_canonical_commit_resolves_head_in_the_current_worktree_not_main(repo, linked):
    """HEAD is per-worktree. gitref must resolve it against the worktree the
    caller is actually in, not the main checkout that `project_root`
    (correctly) always names."""
    main_head = _run(repo, "rev-parse", "HEAD")
    linked_head = _run(linked, "rev-parse", "HEAD")
    assert linked_head != main_head, "fixture must diverge, or this proves nothing"
    cfg = Config(project_root=repo, work_root=linked)
    assert gitref.canonical_commit(cfg, "HEAD") == linked_head


def test_provenance_stamp_records_the_current_worktrees_sha_and_dirtiness(repo, linked):
    """`dirty` is the single worst field to get wrong here: a stamp reading
    `dirty: false` while the actual tree is dirty vouches for a verification
    that never happened. Dirtying only the linked worktree and asserting the
    main checkout stays clean proves the flag was read from the linked tree,
    not a shared/global one."""
    linked_head = _run(linked, "rev-parse", "HEAD")
    cfg = Config(project_root=repo, work_root=linked)
    prov = gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])
    assert prov["sha"] == linked_head
    assert prov["dirty"] is False

    (linked / "scratch").write_text("uncommitted")
    dirty_prov = gitref.provenance_stamp(cfg, K.DEFAULT_KINDS["check"])
    assert dirty_prov["dirty"] is True
    assert _run(repo, "status", "--porcelain") == "", \
        "main checkout must stay clean -- otherwise this isn't testing the linked tree"


def test_store_path_still_derives_from_the_main_worktree(repo, linked, monkeypatch):
    """This must not regress: the store is a sibling of the MAIN checkout
    even when resolved from inside a linked worktree. This is what stops the
    work_root fix from breaking the thing it's built on top of."""
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    root = config.project_root(cwd=linked)
    assert root == repo
    assert config.store_dir(root) == repo.parent / f"{repo.name}-notes"


# ---- commits_since: has a row's file changed since the row saw it ----
def _commit_at(repo, msg, when, *extra):
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-am", msg, *extra],
                   env={**os.environ, **env}, check=True, capture_output=True)


def _file_row(cfg, name, created_at, stamp=True):
    from symbion import store
    blob = gitref.target_blob(cfg, "file", name) if stamp else None
    return store.Note(id=f"r-{created_at}", kind="note", created_at=created_at, author="t",
                      body="", target=store.Target(type="file", name=name), target_blob=blob)


def _since(cfg, row):
    return gitref.commits_since(cfg, [row])[row.id]


def test_target_blob_is_the_blob_git_would_commit_and_only_for_a_file_here(repo):
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files", "host": "true"})
    (repo / "f0").write_text("edited")
    assert gitref.target_blob(cfg, "file", "f0") == _run(repo, "hash-object", "f0")
    assert gitref.target_blob(cfg, "item", "f0") is None, "an item is no path"
    assert gitref.target_blob(cfg, "file", "gone.py") is None
    assert gitref.target_blob(cfg, "host", "/etc/hosts") is None, "outside the checkout"


def test_the_commit_that_carries_what_the_row_saw_is_not_counted(repo):
    """The row is written over an uncommitted edit. By date, the commit that
    carries that edit counts against the row; anchored on the stamped
    content, it is the row's own starting point."""
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    (repo / "f0").write_text("edited")
    row = _file_row(cfg, "f0", "2030-01-04T00:00:00+00:00")
    _commit_at(repo, "carries the edit", "2030-01-05T00:00:00+00:00")
    assert _since(cfg, row) == 0
    (repo / "f0").write_text("later")
    _commit_at(repo, "later", "2030-01-06T00:00:00+00:00")
    assert _since(cfg, row) == 1


def test_a_merged_commit_dated_before_the_row_counts_and_the_merge_does_not(repo):
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    (repo / "f0").write_text("a\nb\nc\nd\ne\n")
    _commit_at(repo, "base", "2030-01-01T00:00:00+00:00")
    _run(repo, "checkout", "-q", "-b", "side")
    (repo / "f0").write_text("a\nb\nc\nd\nE\n")
    _commit_at(repo, "side", "2030-01-02T00:00:00+00:00")
    _run(repo, "checkout", "-q", "main")
    row = _file_row(cfg, "f0", "2030-01-03T00:00:00+00:00")
    (repo / "f0").write_text("A\nb\nc\nd\ne\n")   # both sides change f0, so the merge's
    _commit_at(repo, "main", "2030-01-03T12:00:00+00:00")   # file matches neither parent
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "merge", "-q", "--no-ff", "--no-edit", "side"], check=True,
                   capture_output=True, env={**os.environ,
                                             "GIT_COMMITTER_DATE": "2030-01-04T00:00:00+00:00"})
    assert _since(cfg, row) == 2, "main's commit and side's; by date, side's is before the row"


def test_without_a_commit_holding_the_stamp_the_count_falls_back_to_dates(repo):
    """An unstamped row (written before stamping), and a stamped one whose
    content was edited again before any commit took it."""
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    (repo / "f0").write_text("seen")
    stamped = _file_row(cfg, "f0", "2030-01-03T00:00:00+00:00")
    unstamped = _file_row(cfg, "f0", "2030-01-03T00:00:00+00:00", stamp=False)
    (repo / "f0").write_text("edited again")
    _commit_at(repo, "after", "2030-01-04T00:00:00+00:00")
    assert _since(cfg, stamped) == 1
    assert _since(cfg, unstamped) == 1
    assert _since(cfg, _file_row(cfg, "f1", "2030-01-03T00:00:00+00:00", stamp=False)) == 0


def test_commits_since_is_none_where_the_target_is_no_file_here(repo):
    from symbion import store
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    item = store.Note(id="i", kind="note", created_at="2030-01-01T00:00:00+00:00",
                      author="t", body="", target=store.Target(type="item", name="f0"))
    gone = _file_row(cfg, "gone.py", "2030-01-01T00:00:00+00:00")
    assert gitref.commits_since(cfg, [item, gone]) == {"i": None, gone.id: None}


def test_a_row_written_on_a_branch_counts_mains_commits_once_merged(repo):
    """The row is written on a side branch, and read on main after the merge.
    Its anchor and main's commit are on two lines: which one a log lists
    first says nothing about ancestry, so the count must not come from order."""
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    (repo / "f0").write_text("a\nb\nc\nd\ne\n")
    _commit_at(repo, "base", "2030-01-01T00:00:00+00:00")
    _run(repo, "checkout", "-q", "-b", "side")
    (repo / "f0").write_text("a\nb\nc\nd\nE\n")
    _commit_at(repo, "side", "2030-01-03T00:00:00+00:00")
    row = _file_row(cfg, "f0", "2030-01-03T01:00:00+00:00")
    _run(repo, "checkout", "-q", "main")
    (repo / "f0").write_text("A\nb\nc\nd\ne\n")
    _commit_at(repo, "main", "2030-01-02T00:00:00+00:00")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "merge", "-q", "--no-ff", "--no-edit", "side"], check=True,
                   capture_output=True, env={**os.environ,
                                             "GIT_COMMITTER_DATE": "2030-01-04T00:00:00+00:00"})
    assert _since(cfg, row) == 1, "main's commit; side's is the anchor"


def test_a_linear_history_counts_without_a_rev_list(repo, monkeypatch):
    """Each stamped row's anchor took a `rev-list`: a page of rows on busy
    files paid one git call per row. With no merge above the anchor, the
    walk's own order is the answer."""
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    row = _file_row(cfg, "f0", "2030-01-01T00:00:00+00:00")
    for i in range(3):
        (repo / "f0").write_text(f"v{i}")
        _commit_at(repo, f"v{i}", f"2030-01-0{i + 2}T00:00:00+00:00")
    seen = []
    real = gitref._git
    monkeypatch.setattr(gitref, "_git", lambda cfg, *a, **k: seen.append(a[0]) or real(cfg, *a, **k))
    assert _since(cfg, row) == 3
    assert "rev-list" not in seen, seen


def test_an_ancestor_dated_after_the_anchor_is_not_counted(repo):
    """Committer dates need not follow ancestry (a skewed clock, a rebase):
    here a merged branch's commit is dated after the row, yet is an ancestor
    of the commit the row saw. By date it counts; by ancestry it does not."""
    cfg = Config(project_root=repo, catalogs={"file": "git ls-files"})
    (repo / "f0").write_text("a\nb\nc\nd\ne\n")
    _commit_at(repo, "base", "2030-01-01T00:00:00+00:00")
    _run(repo, "checkout", "-q", "-b", "side")
    (repo / "f0").write_text("a\nb\nc\nd\nE\n")
    _commit_at(repo, "skewed", "2030-01-10T00:00:00+00:00")
    _run(repo, "checkout", "-q", "main")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "merge", "-q", "--no-ff", "--no-edit", "side"], check=True,
                   capture_output=True, env={**os.environ,
                                             "GIT_COMMITTER_DATE": "2030-01-02T00:00:00+00:00"})
    (repo / "f0").write_text("A\nb\nc\nd\nE\n")
    _commit_at(repo, "seen", "2030-01-03T00:00:00+00:00")
    row = _file_row(cfg, "f0", "2030-01-03T12:00:00+00:00")
    (repo / "f0").write_text("A\nB\nc\nd\nE\n")
    _commit_at(repo, "after", "2030-01-04T00:00:00+00:00")
    assert _since(cfg, row) == 1, "only `after`"
