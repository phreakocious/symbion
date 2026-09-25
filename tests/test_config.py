import subprocess
from pathlib import Path
import pytest
from symbion import config


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "myproj"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "f.txt").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return r


def test_project_root_is_the_checkout(repo):
    assert config.project_root(cwd=repo) == repo


def test_project_root_from_worktree_is_the_main_checkout(repo, tmp_path):
    wt = tmp_path / "myproj-wt1"
    _git(repo, "worktree", "add", "-q", "-d", str(wt), "HEAD")
    # The whole point: a worktree must NOT get its own store.
    assert config.project_root(cwd=wt) == repo


def test_work_root_is_the_checkout(repo):
    assert config.work_root(cwd=repo) == repo


def test_work_root_from_worktree_is_the_worktree_itself(repo, tmp_path):
    """The opposite of project_root: HEAD/branch/dirty are per-worktree, so
    the root used to resolve them must be the CURRENT checkout, not the
    main one."""
    wt = tmp_path / "myproj-wt1"
    _git(repo, "worktree", "add", "-q", "-d", str(wt), "HEAD")
    assert config.work_root(cwd=wt) == wt
    assert config.work_root(cwd=wt) != config.project_root(cwd=wt)


def test_store_dir_is_a_sibling_named_after_the_project(repo, monkeypatch):
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    assert config.store_dir(repo) == repo.parent / "myproj-notes"


def test_load_with_no_config_file_returns_defaults(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    cfg = config.load(store, project_root=repo)
    assert cfg.catalogs == {}
    assert cfg.default_branch == "main"
    assert cfg.provenance_command is None
    assert cfg.command_timeout == 30
    # The store is recorded on the Config so run_configured can hand a
    # nested symbion the same one; nothing else knows it.
    assert cfg.store == store.resolve()


def test_load_reads_catalogs_and_renames(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    (store / "symbion.toml").write_text(
        '[catalogs]\nfile = "git ls-files"\n\n[renames]\nfile = "git"\n')
    cfg = config.load(store, project_root=repo)
    assert cfg.catalogs == {"file": "git ls-files"}
    assert cfg.renames == {"file": "git"}


def test_load_reads_command_timeout(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    (store / "symbion.toml").write_text("command_timeout = 5\n")
    cfg = config.load(store, project_root=repo)
    assert cfg.command_timeout == 5


# ---- work_root: separate from project_root, per-worktree by definition ----

def test_config_defaults_work_root_to_project_root_when_constructed_directly():
    """A Config built without naming work_root (every existing fixture in
    this suite does this) must behave exactly as before: the two roots
    coincide."""
    cfg = config.Config(project_root=Path("/x"))
    assert cfg.work_root == Path("/x")


def test_load_defaults_work_root_to_the_passed_in_work_root(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    wt = tmp_path / "myproj-wt1"
    _git(repo, "worktree", "add", "-q", "-d", str(wt), "HEAD")
    cfg = config.load(store, project_root=repo, work_root=wt)
    assert cfg.project_root == repo
    assert cfg.work_root == wt


def test_load_without_a_work_root_argument_falls_back_to_project_root(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    cfg = config.load(store, project_root=repo)
    assert cfg.work_root == repo


def test_explicit_project_root_in_toml_also_pins_work_root(repo, tmp_path):
    """Precedence: pinning `project_root` by hand in symbion.toml means
    'operate on this tree' -- it pins work_root too, even though a
    different (correct, current-worktree) work_root was passed in
    programmatically."""
    store = tmp_path / "myproj-notes"
    store.mkdir()
    wt = tmp_path / "myproj-wt1"
    _git(repo, "worktree", "add", "-q", "-d", str(wt), "HEAD")
    (store / "symbion.toml").write_text(f'project_root = "{repo}"\n')
    cfg = config.load(store, project_root=repo, work_root=wt)
    assert cfg.project_root == repo
    assert cfg.work_root == repo, "explicit project_root must pin work_root"


def test_explicit_work_root_in_toml_overrides_the_pin(repo, tmp_path):
    """An explicit `work_root` in symbion.toml always wins, even over an
    explicit `project_root` pin -- someone who names both roots by hand
    means exactly what they wrote."""
    store = tmp_path / "myproj-notes"
    store.mkdir()
    wt = tmp_path / "myproj-wt1"
    _git(repo, "worktree", "add", "-q", "-d", str(wt), "HEAD")
    (store / "symbion.toml").write_text(
        f'project_root = "{repo}"\nwork_root = "{wt}"\n')
    cfg = config.load(store, project_root=repo)
    assert cfg.project_root == repo
    assert cfg.work_root == wt


def test_load_reads_resolvers(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    (store / "symbion.toml").write_text(
        '[catalogs]\nnum = "echo a"\n\n[resolvers]\nnum = "head -1"\n')
    cfg = config.load(store, project_root=repo)
    assert cfg.resolvers == {"num": "head -1"}


def test_load_without_resolvers_is_empty(repo, tmp_path):
    store = tmp_path / "myproj-notes"
    store.mkdir()
    (store / "symbion.toml").write_text('[catalogs]\nnum = "echo a"\n')
    assert config.load(store, project_root=repo).resolvers == {}
    assert config.Config(project_root=repo).resolvers == {}


def test_load_refuses_a_resolver_whose_type_has_no_catalog(repo, tmp_path):
    """The catalog declares the type; a resolver alone is a typo or a missing
    line. The matching key on the same file must still load."""
    store = tmp_path / "myproj-notes"
    store.mkdir()
    (store / "symbion.toml").write_text(
        '[catalogs]\nnum = "echo a"\n\n[resolvers]\nother = "head -1"\n')
    with pytest.raises(ValueError) as e:
        config.load(store, project_root=repo)
    assert "other" in str(e.value)
    assert str(store / "symbion.toml") in str(e.value)
    (store / "symbion.toml").write_text(
        '[catalogs]\nnum = "echo a"\n\n[resolvers]\nnum = "head -1"\n')
    assert config.load(store, project_root=repo).resolvers == {"num": "head -1"}


def test_load_refuses_a_resolver_on_a_builtin_type(repo, tmp_path):
    """A `[resolvers]` key on a built-in type (`commit`, `item`, `project`,
    `arc`) loads silently today and never runs: `canonical` returns before
    ever consulting `cfg.resolvers` for those types. Refused at load, same as
    an orphan key, even when the same key is ALSO declared under [catalogs]."""
    store = tmp_path / "myproj-notes"
    store.mkdir()
    (store / "symbion.toml").write_text(
        '[catalogs]\ncommit = "echo x"\n\n[resolvers]\ncommit = "head -1"\n')
    with pytest.raises(ValueError) as e:
        config.load(store, project_root=repo)
    assert "commit" in str(e.value)
    assert str(store / "symbion.toml") in str(e.value)
    (store / "symbion.toml").write_text(
        '[catalogs]\nnum = "echo a"\n\n[resolvers]\nnum = "head -1"\n')
    assert config.load(store, project_root=repo).resolvers == {"num": "head -1"}


# ---- .symbion: the store pointer ----
# Measured 2026-09-11 in a throwaway repo: rename the repo DIRECTORY and the
# store is not lost, it is FORKED. `symbion summary` prints nothing and exits
# 0 -- which the README's own table reads as "no store yet" -- and the next
# `symbion add` creates a second store beside the first and prints an ordinary
# id. Two histories, no warning. A pointer beside the project is the durable
# fix; SYMBION_DIR is per-invocation and nothing in the repo remembers it.

def test_store_dir_reads_the_pointer(repo, monkeypatch):
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    (repo / ".symbion").write_text("../elsewhere\n")
    assert config.store_dir(repo) == (repo.parent / "elsewhere").resolve()


def test_pointer_may_be_absolute(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    (repo / ".symbion").write_text(f"{tmp_path / 'abs-store'}\n")
    assert config.store_dir(repo) == (tmp_path / "abs-store").resolve()


def test_pointer_expands_a_tilde(repo, monkeypatch):
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    (repo / ".symbion").write_text("~/some-store\n")
    assert config.store_dir(repo) == (Path.home() / "some-store").resolve()


def test_env_beats_the_pointer(repo, tmp_path, monkeypatch):
    """SYMBION_DIR stays the most specific step: a one-off invocation against
    another store must not be overridden by a file in the tree."""
    (repo / ".symbion").write_text("../elsewhere\n")
    monkeypatch.setenv("SYMBION_DIR", str(tmp_path / "env-store"))
    assert config.store_dir(repo) == (tmp_path / "env-store").resolve()


def test_a_blank_pointer_falls_through_to_the_default(repo, monkeypatch):
    """The falsifiable direction. An empty file must NOT resolve to the repo
    root itself (`root / ""` is `root`), which would make the project its own
    store and write notes.jsonl into the tracked tree."""
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    (repo / ".symbion").write_text("\n   \n")
    assert config.store_dir(repo) == repo.parent / "myproj-notes"


def test_pointer_takes_the_first_non_blank_line(repo, monkeypatch):
    """Same rule as the resolver protocol's stdout, so there is one convention
    to remember -- and a file with a trailing comment line still works."""
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    (repo / ".symbion").write_text("\n../elsewhere\n# why: renamed 2026-09-11\n")
    assert config.store_dir(repo) == (repo.parent / "elsewhere").resolve()


def test_no_pointer_is_still_the_sibling_default(repo, monkeypatch):
    """The other direction of the blank-pointer test: absent means absent, so
    stores adopted before this existed need no migration."""
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    assert not (repo / ".symbion").exists()
    assert config.store_dir(repo) == repo.parent / "myproj-notes"


# ---- store_owner: the project a named store belongs to ----
# The inverse of the sibling rule, so `--dir <store>` from inside ANOTHER repo
# resolves in the store's project rather than the cwd's (measured 2026-09-23:
# from another repo, the store's `file` catalog ran in the wrong tree and came
# back empty).
# Every shape that must NOT claim a project, one case each.

def test_a_sibling_store_is_owned_by_its_project(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBION_DIR", str(tmp_path / "unrelated"))    # env is set aside
    assert config.store_owner(tmp_path / "myproj-notes") == repo


def test_a_pointer_naming_the_sibling_keeps_the_claim(repo, tmp_path):
    (repo / ".symbion").write_text("../myproj-notes\n")
    assert config.store_owner(tmp_path / "myproj-notes") == repo


@pytest.mark.parametrize("shape", ["no -notes suffix", "no sibling", "plain dir",
                                   "subdir of a repo", "linked worktree", "pointer elsewhere"])
def test_a_store_owner_is_none_unless_the_project_names_that_store(repo, tmp_path, shape):
    s = tmp_path / "myproj-notes"
    if shape == "no -notes suffix":
        s = tmp_path / "myproj"
    elif shape == "no sibling":
        s = tmp_path / "gone-notes"
    elif shape == "plain dir":
        (tmp_path / "plain").mkdir()
        s = tmp_path / "plain-notes"
    elif shape == "subdir of a repo":              # git walks up to myproj
        (repo / "sub").mkdir()
        s = repo / "sub-notes"
    elif shape == "linked worktree":               # its store is myproj's
        _git(repo, "worktree", "add", "-q", "-d", str(tmp_path / "wt"), "HEAD")
        s = tmp_path / "wt-notes"
    elif shape == "pointer elsewhere":
        (repo / ".symbion").write_text("../other-notes\n")
    assert config.store_owner(s) is None
