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
    (repo / ".git" / "objects" / mid[:2] / mid[2:]).unlink()
    assert gitref.check_state(cfg, prov) == ("unverifiable", None)


def test_an_older_git_that_exits_1_with_an_error_still_reads_unverifiable(repo, monkeypatch):
    """The test above runs whatever git is installed. git 2.39 (macOS's own)
    exits 1, not 128, when it cannot read the history, and says `error:` on
    stderr; read as "no", the missing object showed `diverged` again (found
    running the suite from the sdist on the system git, 2026-09-29). This
    replays that contract on the same broken repo: real git, its 128 turned
    into 1 with the stderr kept. A warning on a real "no" stays a "no"."""
    cfg = Config(project_root=repo)
    stamp, mid = _run(repo, "rev-parse", "HEAD~1"), _run(repo, "rev-parse", "HEAD")
    (repo / "f2").write_text("x")
    _run(repo, "add", "-A")
    _run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c2")
    real = gitref._git

    def old_git(c, *args):
        r = real(c, *args)
        if args[:2] == ("merge-base", "--is-ancestor") and r.returncode == 128:
            return subprocess.CompletedProcess(r.args, 1, r.stdout, r.stderr)
        return r
    monkeypatch.setattr(gitref, "_git", old_git)
    (repo / ".git" / "objects" / mid[:2] / mid[2:]).unlink()
    assert gitref.check_state(cfg, {"sha": stamp, "dirty": False}) == ("unverifiable", None)

    def warns(c, *args):
        r = real(c, *args)
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(r.args, 1, "", "warning: unable to access x\n")
        return r
    monkeypatch.setattr(gitref, "_git", warns)          # both directions answer "no"
    assert gitref.check_state(cfg, {"sha": stamp, "dirty": False}) == ("diverged", None)

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
