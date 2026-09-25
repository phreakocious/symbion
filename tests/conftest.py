import importlib.util
import subprocess

import pytest

# NiceGUI's `user` fixture lives in a plugin that is not auto-registered.
# Conditional so a checkout without the [gui] extra still collects: the gui
# tests importorskip themselves, but a missing pytest_plugins entry is a
# collection error no importorskip can reach.
pytest_plugins = (["nicegui.testing.user_plugin"]
                  if importlib.util.find_spec("nicegui") else [])


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """`init` links the skill under ~/.claude and `summary` reads it there:
    without this every init test would write into the real home, and every
    summary test would read whatever that home holds. Git identity is pinned
    for the same reason: a fresh HOME has no ~/.gitconfig. The ceiling stops
    git's repo search at pytest's temp root: with TMPDIR inside a repo, an
    "outside any git repo" test otherwise found that repo, and `init` wrote a
    `.symbion` into its root."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp()))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


@pytest.fixture(scope="session")
def _neutral_repo(tmp_path_factory):
    r = tmp_path_factory.mktemp("cwd")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "c0")
    return r


@pytest.fixture(autouse=True)
def _isolated_cwd(_neutral_repo, monkeypatch):
    """A store named by --dir resolves its catalogs and provenance in the cwd's
    repo. Without this, every test that does not ask for `repo` ran them in
    whatever checkout pytest was launched from, and from an unpacked sdist,
    which is no repo, 14 of them failed. One empty repo shared by the session:
    a test that writes into its cwd or commits asks for `repo` instead."""
    monkeypatch.chdir(_neutral_repo)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo, chdir'd into so config.project_root() resolves here
    (it shells to `git worktree list --porcelain` in the real cwd)."""
    r = tmp_path / "p"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c0")
    monkeypatch.chdir(r)
    return r
