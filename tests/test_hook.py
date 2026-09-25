import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from symbion import store

HOOK = Path(__file__).resolve().parents[1] / "src/symbion/data/skill/session_start.sh"

# Resolved once, via the test runner's own PATH, and invoked by absolute path.
# subprocess.run(["bash", ...], env={"PATH": ...}) resolves argv[0] itself
# through the CHILD's PATH (os.get_exec_path(env)) -- so a PATH that is empty
# or wrong (as test_hook_exits_0_when_symbion_is_not_installed needs, to prove
# symbion is genuinely unreachable) makes bash itself unresolvable and raises
# FileNotFoundError in the parent before the hook ever runs. An absolute path
# has a dirname, so Python skips PATH search for the interpreter while the
# hook's own `command -v symbion` still runs inside the broken env below.
BASH = shutil.which("bash") or "/bin/bash"

# The dir holding the `symbion` console script this suite itself runs under
# (installed editable into this venv) -- NOT from shutil.which("symbion"),
# which returns None whenever the calling shell hasn't activated the venv
# (true of this very test run). sys.executable, UNRESOLVED, is always the
# venv's own python -- .resolve() would follow the venv symlink straight
# through to the real system interpreter's bin dir, which has no `symbion`.
SYMBION_BIN = str(Path(sys.executable).parent)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo -- config.project_root() shells out to
    `git worktree list --porcelain` and needs one, or symbion itself exits
    nonzero with 'not a git repository' before ever reaching the store."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "f.txt").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return r


def test_hook_prints_the_zero_line_for_an_empty_but_existing_store(git_repo, tmp_path):
    """symbion IS resolvable, cwd IS a git repo, and the store DOES exist
    (just empty) -- proves this actually reached `symbion summary` and ran
    its real code path, rather than exiting early at `command -v symbion`
    like the sibling test below. A vacuous version of this test would pass
    on an early exit too, since both print nothing to stderr and return 0."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin",
                            "SYMBION_DIR": str(store_dir)})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "bug 0" in r.stdout


def test_hook_names_the_absent_store(git_repo, tmp_path):
    """Amended 2026-09-20: `init` creates the store and installs
    the hook together, so the hook running against an absent store is a
    pointer to nowhere, a renamed repo or a wrong SYMBION_DIR -- never a
    first session -- and silence there read as "no store yet" twice in the
    field while the next `add` forked a second store. Exit 0 and empty
    stderr still hold: the hook never fails a session. The sibling test
    below keeps the genuinely quiet direction (not installed, no store)."""
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin",
                            "SYMBION_DIR": str(tmp_path / "nope")})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "no store at" in r.stdout and str(tmp_path / "nope") in r.stdout
    assert "bug 0" not in r.stdout, "an absent store must not print the zero-counts line"


def test_hook_is_silent_in_a_project_that_never_adopted_symbion(git_repo):
    """User-level since 2026-09-24, the hook runs in EVERY project. No store,
    no `.symbion` pointer and no SYMBION_DIR is a project that never adopted
    symbion, and it must hear nothing -- the tests around this one keep the
    loud directions (a named store that is absent, a store with rows)."""
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin"})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_hook_names_a_pointer_to_a_store_that_is_not_there(git_repo):
    (git_repo / ".symbion").write_text("../gone-notes\n")
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin"})
    assert r.returncode == 0 and "no store at" in r.stdout and "gone-notes" in r.stdout


def test_hook_exits_0_and_prints_nothing_when_symbion_is_not_installed_and_no_store(tmp_path):
    """The quiet direction of the not-installed case: no sibling store, so
    this is a project that never adopted symbion, and the hook must say
    nothing -- the loud test below covers the other direction."""
    r = subprocess.run([BASH, str(HOOK)], cwd=tmp_path,
                       capture_output=True, text=True,
                       env={"PATH": "/nonexistent"})
    assert r.returncode == 0
    assert r.stderr == ""
    assert r.stdout == ""


def test_hook_names_the_missing_binary_when_a_sibling_store_exists(git_repo, tmp_path):
    """The loud direction. A project whose sibling `<name>-notes` store exists
    but whose `symbion` is off PATH is not a project that never adopted
    symbion -- silence there hides exactly the failure the hook exists to
    report (measured 2026-09-04: this repo's own read loop was dead for a
    session because the venv script was never put on PATH). With PATH set to
    nowhere the hook can use only bash builtins to find the store."""
    store_dir = tmp_path / "proj-notes"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": "/nonexistent"})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "not on PATH" in r.stdout


def test_hook_summarizes_the_project_dir_not_the_cwd(tmp_path):
    """`root` used to be assigned INSIDE the not-installed branch, so the
    installed path never saw it: the hook checked CLAUDE_PROJECT_DIR's store
    for existence and then summarized whatever project the cwd happened to be.
    Two stores here, and only the project dir's has the bug -- reading the
    cwd's gives 0 and passes every other assertion in this file."""
    other, proj = tmp_path / "other", tmp_path / "proj"
    for r in (other, proj):
        r.mkdir()
        _git(r, "init", "-q")
        (r / "f.txt").write_text("x")
        _git(r, "add", "-A")
        _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        store.ensure_store(tmp_path / f"{r.name}-notes")
    store.add(tmp_path / "proj-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")

    r = subprocess.run([BASH, str(HOOK)], cwd=other, capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin",
                            "CLAUDE_PROJECT_DIR": str(proj)})
    assert r.returncode == 0 and r.stderr == ""
    assert "bug 1" in r.stdout, f"summarized the cwd, not the project: {r.stdout!r}"


def test_hook_still_uses_the_cwd_when_no_project_dir_is_set(tmp_path):
    """The other direction of the same `root`: with CLAUDE_PROJECT_DIR unset it
    must stay the cwd, or the fix above would break every ordinary invocation."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / "f.txt").write_text("x")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    store.add(tmp_path / "proj-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")

    r = subprocess.run([BASH, str(HOOK)], cwd=proj, capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin"})
    assert r.returncode == 0 and r.stderr == ""
    assert "bug 1" in r.stdout


def test_hook_prints_open_heads_and_not_the_schema(git_repo, tmp_path):
    """The kinds table changes only when symbion.toml does, and SKILL.md
    already embeds it, so an agent with the skill loaded paid for it twice
    every session. The open heads are what a session cannot re-derive
    without knowing to ask. Both directions: the row is there, the table
    is not."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text('[kinds]\nanomaly = { status = true }\n')
    store.add(store_dir, kind="anomaly", target={"type": "project", "name": None},
              body="latency drifts 2 ms", status="open")
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin",
                            "SYMBION_DIR": str(store_dir)})
    assert r.returncode == 0 and r.stderr == ""
    lines = r.stdout.splitlines()
    assert lines[0].startswith("symbion: open outside arcs: anomaly 1")
    assert any("latency drifts 2 ms" in l for l in lines[1:]), "the open head itself"
    assert not any(l.startswith("kinds  (symbion.toml [kinds])") for l in lines)


def test_hook_finds_a_pointed_store_with_no_symbion_on_path(git_repo, tmp_path):
    """The bash twin of config.store_dir's pointer. The store is NOT the
    sibling the repo name implies, so without reading `.symbion` the hook
    goes silent -- which reads as "never adopted" and is the exact failure
    this branch exists to report."""
    store_dir = tmp_path / "elsewhere-store"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    (git_repo / ".symbion").write_text("../elsewhere-store\n")
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert "not on PATH" in r.stdout
    assert "elsewhere-store" in r.stdout


def test_hook_is_silent_on_the_same_tree_without_the_pointer(git_repo, tmp_path):
    """The other direction: identical tree and identical store, pointer
    removed. Without this the test above passes on a hook that prints the
    message unconditionally."""
    store_dir = tmp_path / "elsewhere-store"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    assert not (git_repo / ".symbion").exists()
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout == ""


def test_hook_ignores_a_blank_pointer(git_repo, tmp_path):
    """A blank pointer must not resolve to the repo root. If it did, a repo
    carrying its own notes.jsonl would satisfy the -e test and the hook would
    claim a store that is really the project tree."""
    (git_repo / ".symbion").write_text("\n  \n")
    (git_repo / "notes.jsonl").write_text("")
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout == ""


def test_hook_names_a_malformed_kinds_table_instead_of_reading_as_no_store(git_repo, tmp_path):
    """`symbion summary 2>/dev/null` swallowed every error, so a store whose
    symbion.toml carries a broken [kinds] table started the session exactly
    like a project that never adopted symbion. The error goes to STDOUT: a
    SessionStart hook's stdout is what reaches the agent, and exit 0 with
    empty stderr still holds -- the hook never fails a session."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text('[kinds]\nnote = { status = "yes" }\n')
    r = subprocess.run([BASH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={"PATH": f"{SYMBION_BIN}:/usr/bin:/bin",
                            "SYMBION_DIR": str(store_dir)})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "[kinds]" in r.stdout and "'note'" in r.stdout, f"silent: {r.stdout!r}"
