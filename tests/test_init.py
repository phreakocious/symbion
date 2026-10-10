"""`symbion init` creates the store and links the agent-facing skill once per
user, from package data. Per-project copies drifted within the hour
(2026-09-04), and went stale on every edit (2026-09-23/24); a link into the
install cannot drift."""
import json
import os
import re
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest
from symbion import cli, store

REPO = Path(__file__).resolve().parents[1]
DATA = resources.files("symbion") / "data"


def run(*args, store_dir):
    return cli.main(["--dir", str(store_dir), *args])


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "p"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c0")
    monkeypatch.chdir(r)
    return r


SKILL_DIR = DATA / "skill"


def _home() -> Path:
    return Path(os.environ["HOME"])       # conftest isolates it per test


def _summary(tmp_path, capsys):
    run("summary", store_dir=tmp_path / "store")
    text = capsys.readouterr().out
    run("summary", "--json", store_dir=tmp_path / "store")
    return text, json.loads(capsys.readouterr().out)


def test_init_links_the_user_skill_and_writes_nothing_into_the_project(repo, tmp_path, capsys):
    """Per-project copies re-staled every adopter on every SKILL.md edit
    (2026-09-23/24). init links the package's skill dir once per user, and a
    link into the install is current by construction."""
    assert run("init", "--yes", store_dir=tmp_path / "store") == 0
    link = _home() / ".claude/skills/symbion"
    assert link.resolve() == Path(str(SKILL_DIR)).resolve()  # Windows may use a junction
    assert (link / "SKILL.md").read_text() == (SKILL_DIR / "SKILL.md").read_text()
    assert not (repo / ".claude").exists() and not (repo / "hooks").exists()
    settings = json.loads((_home() / ".claude/settings.json").read_text())
    cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == 'sh "$HOME/.claude/skills/symbion/session_start.sh"'
    out = capsys.readouterr().out
    assert f"linked {link}" in out and f"wrote {_home() / '.claude/settings.json'}" in out


def test_init_rerun_keeps_the_link_the_registration_and_the_toml(repo, tmp_path, capsys):
    store = tmp_path / "store"
    run("init", "--yes", store_dir=store)
    toml = store / "symbion.toml"
    toml.write_text(toml.read_text() + "\n# customized\n")
    capsys.readouterr()
    assert run("init", "--yes", store_dir=store) == 0
    out = capsys.readouterr().out
    assert "kept" in out and "linked" not in out and "SessionStart" not in out, out
    assert "# customized" in toml.read_text()


@pytest.mark.skipif(os.name != "nt", reason="a junction is Windows'")
def test_init_links_by_junction_where_windows_refuses_a_symlink(repo, tmp_path, capsys,
                                                               monkeypatch):
    """A symlink on Windows needs Developer Mode or an elevated shell; a
    person's own terminal has neither. An elevated ssh session never sees the
    refusal, so it is forced here, as WinError 1314."""
    def refuse(*a, **kw):
        raise OSError(22, "A required privilege is not held by the client", None, 1314)
    monkeypatch.setattr(Path, "symlink_to", refuse)
    store = tmp_path / "store"
    assert run("init", "--yes", store_dir=store) == 0
    link = _home() / ".claude/skills/symbion"
    assert not link.is_symlink() and link.resolve() == Path(str(SKILL_DIR)).resolve()
    assert (link / "SKILL.md").read_text() == (SKILL_DIR / "SKILL.md").read_text()
    capsys.readouterr()
    assert run("init", "--yes", store_dir=store) == 0
    out = capsys.readouterr().out
    assert "kept" in out and "linked" not in out and "left alone" not in out, out


def test_init_writes_a_store_readme_once(repo, tmp_path, capsys):
    """The owner, 2026-10-05: a store should say what it is. A person who
    opens the store's repo, on a forge or a disk, met bare JSONL. A re-run
    keeps the file, edited or not."""
    s = tmp_path / "store"
    assert run("init", "--yes", store_dir=s) == 0
    text = (s / "README.md").read_text()
    assert f"`{repo.name}`" in text and "https://github.com/phreakocious/symbion" in text
    assert "banner.png" in text and "notes.jsonl" in text and "symbion summary" in text
    (s / "README.md").write_text("ours\n")
    capsys.readouterr()
    assert run("init", "--yes", store_dir=s) == 0
    assert (s / "README.md").read_text() == "ours\n"
    assert f"wrote {s / 'README.md'}" not in capsys.readouterr().out


def test_init_leaves_a_foreign_skill_dir_alone(repo, tmp_path, capsys):
    mine = _home() / ".claude/skills/symbion"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine")
    assert run("init", "--yes", store_dir=tmp_path / "store") == 0
    assert (mine / "SKILL.md").read_text() == "mine" and not mine.is_symlink()
    assert "left alone" in capsys.readouterr().out


def test_init_prints_the_hook_block_instead_of_merging_user_settings(repo, tmp_path, capsys):
    settings = _home() / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {"allow": []}}\n')
    assert run("init", "--yes", store_dir=tmp_path / "store") == 0
    assert settings.read_text() == '{"permissions": {"allow": []}}\n'
    out = capsys.readouterr().out
    assert "SessionStart" in out and "session_start.sh" in out


def test_init_labels_the_pointer_by_what_git_will_take(repo, tmp_path, capsys):
    (repo / ".gitignore").write_text(".claude/\n")
    assert run("init", "--yes", store_dir=repo.parent / "other-notes") == 0
    assert "-> ../other-notes (git: not ignored)" in capsys.readouterr().out


def test_init_and_summary_name_per_project_copies_an_older_init_left(repo, tmp_path, capsys):
    """The migration signal: every adopter holds copies from before the
    user-level link, and a copy beside the link is a second, staler skill and
    a second hook. Both directions; a project's own hook is not ours."""
    store_dir = tmp_path / "store"
    (repo / "hooks").mkdir()
    (repo / "hooks/session_start.sh").write_text("echo my own hook\n")
    run("init", "--yes", store_dir=store_dir)
    run("add", "note", "--target", "project", store_dir=store_dir)
    capsys.readouterr()
    text, d = _summary(tmp_path, capsys)
    assert d["leftovers"] == [] and "per-project" not in text, text

    (repo / "hooks/session_start.sh").write_text("symbion summary 2>&1 || true\n")
    (repo / ".claude/skills/symbion").mkdir(parents=True)
    (repo / ".claude/settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"command": "$CLAUDE_PROJECT_DIR/hooks/session_start.sh"}]}]}}))
    text, d = _summary(tmp_path, capsys)
    assert d["leftovers"] == [".claude/skills/symbion", "hooks/session_start.sh",
                              ".claude/settings.json SessionStart entry"], d["leftovers"]
    assert ("  3 per-project symbion copies from an older init: .claude/skills/symbion, "
            "hooks/session_start.sh, .claude/settings.json SessionStart entry -- the "
            "user-level skill replaces them; remove them") in text.splitlines(), text
    run("init", "--yes", store_dir=store_dir)
    assert "per-project symbion copies" in capsys.readouterr().out


def test_summary_points_at_the_user_skill_before_a_write(repo, tmp_path, capsys):
    """After bootstrap, agents learned symbion from `--help` and guesses:
    later sessions rarely loaded SKILL.md or ran `schema` (2026-09-24). The
    hook prints summary, so the pointer
    reaches every session; it names the skill only where it is linked."""
    store.ensure_store(tmp_path / "store")
    run("add", "note", "--target", "project", store_dir=tmp_path / "store")
    text, d = _summary(tmp_path, capsys)        # an empty store has FIRST_CONTACT instead
    assert "SKILL.md" not in text and d["skill"] is None
    run("init", "--yes", store_dir=tmp_path / "store")
    capsys.readouterr()
    text, d = _summary(tmp_path, capsys)
    assert d["skill"] == "~/.claude/skills/symbion/SKILL.md"
    assert text.splitlines()[-1] == ("  before a write: load the symbion skill "
                                     "(~/.claude/skills/symbion/SKILL.md); "
                                     "`symbion schema` lists this store's kinds"), text


import tomllib

from symbion import kinds as K


def test_init_writes_the_default_kinds_table_that_round_trips(repo, tmp_path):
    store = tmp_path / "store"
    assert run("init", "--yes", store_dir=store) == 0
    d = tomllib.loads((store / "symbion.toml").read_text())
    assert K.parse_kinds(d["kinds"]) == K.DEFAULT_KINDS
    assert K.is_declared(store) is True


def test_skill_embeds_the_schema_command_and_carries_no_kinds_table():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert "!`symbion schema`" in text
    assert "| `check` |" not in text, "the static kinds table is gone; the command is the table"
    assert "resolve <id> [--result" in text or "--result" in text


def test_init_starter_toml_carries_a_resolvers_block_that_parses(repo, tmp_path):
    store = tmp_path / "store"
    assert run("init", "--yes", store_dir=store) == 0
    text = (store / "symbion.toml").read_text()
    assert "[resolvers]" in text and "pipefail" in text and "fragment" in text
    assert tomllib.loads(text).get("resolvers", {}) == {}


def test_a_file_target_on_a_new_store_names_the_lines_that_declare_it(repo, tmp_path, capsys):
    """A new store refused `--target file:…` with "catalog types are declared
    in … under [catalogs]", and the README leads with file notes (2026-10-09).
    The refusal names the commented lines; uncommenting exactly those makes a
    note on any file land, not only a `.py` one."""
    store = tmp_path / "store"
    assert run("init", "--yes", store_dir=store) == 0
    capsys.readouterr()
    (repo / "main.rs").write_text("x")
    assert run("add", "note", "--target", "file:main.rs", store_dir=store) == 2
    err = capsys.readouterr().err
    m = re.search(r"uncomment `file =` in \S+: line (\d+) under \[catalogs\], "
                  r"line (\d+) under \[renames\]", err)
    assert m, err
    toml = store / "symbion.toml"
    lines = toml.read_text().splitlines()
    for n in m.groups():
        lines[int(n) - 1] = lines[int(n) - 1].removeprefix("# ")
    toml.write_text("\n".join(lines) + "\n")
    assert run("add", "note", "--target", "file:main.rs", store_dir=store) == 0
    assert "taken as typed" not in capsys.readouterr().err


@pytest.mark.parametrize("term, tty, says", [
    pytest.param("xterm", True, True, marks=pytest.mark.skipif(
        os.name == "nt", reason="rich reads a piped Windows console as legacy, which "
                                "takes no ANSI codes, so term prints no colour")),
    ("xterm-256color", True, False), ("xterm", False, False)])
def test_init_at_a_16_colour_terminal_names_the_fix(repo, tmp_path, term, tty, says):
    """At 16 colours the palette falls back to plainer colours, and the
    terminal usually does more than its TERM says: init, run by a person,
    names the fix. An agent has no tty, so it hears nothing (the owner,
    2026-10-06)."""
    env = {k: v for k, v in os.environ.items() if k not in ("COLORTERM", "NO_COLOR")}
    env.update(TERM=term, FORCE_COLOR="1", HOME=str(tmp_path))
    script = ("import sys; sys.stdout.isatty = lambda: True\n" if tty else "") + \
        "from symbion import cli; cli.main(['init'])"
    r = subprocess.run([sys.executable, "-c", script], cwd=repo, env=env,
                       capture_output=True, text=True, encoding="utf-8")
    assert ("set COLORTERM=truecolor, or a TERM ending in -256color" in r.stdout) is says, \
        r.stdout + r.stderr


def test_skill_documents_resolvers_next_to_catalogs():
    assert "`catalogs.md`" in (SKILL_DIR / "SKILL.md").read_text(), "the pointer an agent follows"
    text = (SKILL_DIR / "catalogs.md").read_text()
    assert "[resolvers]" in text
    assert "exit 2" in text.lower(), "the ambiguity contract is the part an agent has to know"
    assert "fragment" in text, "the numeric-catalog warning"


# ---- .symbion: init records a non-default store ----

def test_init_writes_no_pointer_for_the_default_store(repo, tmp_path):
    """The must-not-change direction: every project adopted before the
    pointer existed keeps a clean tree and needs no migration."""
    default = repo.parent / f"{repo.name}-notes"
    assert run("init", "--yes", store_dir=default) == 0
    assert not (repo / ".symbion").exists()


def test_init_records_a_non_default_store_as_a_relative_pointer(repo, tmp_path, capsys):
    """`symbion --dir ../other-notes init` is otherwise forgotten the moment
    the process exits: nothing in the repo remembers which store it used, so
    the next bare command silently starts a second one."""
    assert run("init", "--yes", store_dir=repo.parent / "other-notes") == 0
    assert (repo / ".symbion").read_text().strip() == "../other-notes"
    assert ".symbion" in capsys.readouterr().out
    from symbion import config
    assert config.store_dir(repo) == (repo.parent / "other-notes").resolve()


def test_init_records_a_store_outside_the_parent_as_an_absolute_pointer(repo, tmp_path):
    far = tmp_path / "far" / "away-notes"
    far.parent.mkdir()
    assert run("init", "--yes", store_dir=far) == 0
    assert (repo / ".symbion").read_text().strip() == far.resolve().as_posix()


def test_init_rerun_keeps_an_unchanged_pointer_quiet(repo, tmp_path, capsys):
    assert run("init", "--yes", store_dir=repo.parent / "other-notes") == 0
    capsys.readouterr()
    assert run("init", "--yes", store_dir=repo.parent / "other-notes") == 0
    assert ".symbion" not in capsys.readouterr().out
    assert (repo / ".symbion").read_text().strip() == "../other-notes"


def test_init_on_the_default_store_reports_a_pointer_that_disagrees(repo, tmp_path, capsys):
    """Reached with --dir or SYMBION_DIR naming the default while the tree
    points elsewhere. Deleting someone's pointer is not init's call, so it
    says so and changes nothing -- silence here would leave two stores and
    no clue which the next command uses."""
    (repo / ".symbion").write_text("../other-notes\n")
    assert run("init", "--yes", store_dir=repo.parent / f"{repo.name}-notes") == 0
    out = capsys.readouterr().out
    assert ".symbion" in out and "other-notes" in out
    assert (repo / ".symbion").read_text().strip() == "../other-notes"


@pytest.mark.parametrize("form", ["relative", "absolute"])
def test_init_on_the_default_store_is_quiet_about_a_pointer_that_names_it(repo, tmp_path, capsys, form):
    """The pointer is compared by where it RESOLVES, not by existing.
    Measured 2026-09-22 in two adopting projects: `.symbion` read
    `../<name>-notes`, the default store itself, and a re-run of init told
    the owner to delete it as stale."""
    default = repo.parent / f"{repo.name}-notes"
    value = f"../{default.name}" if form == "relative" else str(default.resolve())
    (repo / ".symbion").write_text(value + "\n")
    assert run("init", "--yes", store_dir=default) == 0
    assert ".symbion" not in capsys.readouterr().out
    assert (repo / ".symbion").read_text().strip() == value


def test_init_under_symbion_dir_does_not_repoint_the_project(repo, tmp_path, monkeypatch, capsys):
    """SYMBION_DIR is per-invocation and often set by a wrapper; a pointer is
    durable. Measured 2026-09-11: `SYMBION_DIR=<scratch> symbion init`, run
    with cwd in a real project to test something unrelated, pointed that
    project at the scratch store -- and a bare command there then reads an
    empty store, which prints nothing and reads as "no store here".

    The contrast is the point: the SAME store named with `--dir` still writes
    the pointer, because that is typed on purpose."""
    scratch = tmp_path / "scratch-notes"
    monkeypatch.setenv("SYMBION_DIR", str(scratch))
    assert cli.main(["init", "--yes"]) == 0
    assert not (repo / ".symbion").exists(), "the env var must not become durable"
    out = capsys.readouterr().out
    assert "SYMBION_DIR" in out, "silent in effect is how this went unnoticed; say why no pointer"

    assert run("init", "--yes", store_dir=scratch) == 0
    assert (repo / ".symbion").read_text().strip() == "../scratch-notes"


@pytest.mark.parametrize("how", ["pointer", "default"])
def test_init_leaves_a_project_that_reads_a_store_on_it(repo, tmp_path, capsys, how):
    """From an adopter, 2026-10-02: `--dir <scratch> init`, run inside a
    project to try a `[kinds]` change, repointed the project's tracked
    `.symbion` at the scratch store. Deleting the scratch store then broke
    every command there. `--dir` means "that store, leave mine alone" for
    every other verb. So init creates the store it names, and changes which
    store the project reads only with --repoint. Both ways a project reads
    a store: a pointer, and the default sibling with no pointer."""
    if how == "pointer":
        current = repo.parent / "shared-notes"
        (repo / ".symbion").write_text("../shared-notes\n# why: shared with a sibling\n")
    else:
        current = repo.parent / f"{repo.name}-notes"
    store.ensure_store(current)
    before = (repo / ".symbion").read_text() if how == "pointer" else None
    scratch = tmp_path / "scratch-notes"
    assert run("init", "--yes", store_dir=scratch) == 0
    assert store.exists(scratch), "the store it names is what was asked for"
    assert ((repo / ".symbion").read_text() if how == "pointer" else None) == before
    out = capsys.readouterr().out
    assert f"this project reads {current}" in out and "--repoint" in out, out

    assert run("init", "--yes", "--repoint", store_dir=scratch) == 0
    assert (repo / ".symbion").read_text().strip() == "../scratch-notes"
    assert f"no longer read from here" in capsys.readouterr().out


def test_init_without_yes_lists_each_change_and_makes_none(repo, tmp_path, capsys):
    """The owner's call, 2026-10-01: init is installed from pipx, so whoever
    runs it has not read its source, and two of its writes are instruction
    surfaces every Claude Code session reads. A run without --yes names each
    path and what it would do to it, writes nothing, and exits 1, so a run
    whose output was thrown away still fails."""
    s = repo.parent / "other-notes"
    assert run("init", store_dir=s) == 1
    out = capsys.readouterr().out
    link, settings = _home() / ".claude/skills/symbion", _home() / ".claude/settings.json"
    for line in (f"will create {s}", f"will write {s / 'symbion.toml'}",
                 f"will write {s / 'README.md'}",
                 f"will link {link} -> ", f"will write {settings}",
                 f"will write {repo / '.symbion'} -> ../other-notes"):
        assert line in out, out
    assert out.splitlines()[-1] == "nothing written: re-run with --yes to make these changes"
    assert not s.exists() and not (_home() / ".claude").exists()
    assert not (repo / ".symbion").exists()


def test_init_without_yes_on_a_set_up_project_has_nothing_to_change(repo, tmp_path, capsys):
    """A re-run checks the setup. When every line is a keep, there is nothing
    for --yes to do, so it says so and exits 0."""
    s = tmp_path / "store"
    assert run("init", "--yes", store_dir=s) == 0
    capsys.readouterr()
    assert run("init", store_dir=s) == 0
    out = capsys.readouterr().out
    assert "will keep" in out and out.splitlines()[-1] == "nothing to change", out
    assert not [ln for ln in out.splitlines() if ln.startswith("will ")
                and not ln.startswith("will keep")], out


def _bare_push(store_dir):
    return subprocess.run(["git", "-C", str(store_dir), "push"],
                          capture_output=True, text=True)


def test_init_sets_the_upstream_so_a_bare_push_works(repo, tmp_path, capsys):
    """`git push origin --all` sets no upstream, so a later bare `git push`
    did nothing and its error was invisible in a `| tail -1` (2026-09-22).
    Both directions: the bare push fails BEFORE init, or this proves
    nothing; it succeeds after, and the unpushed count reads 0."""
    from symbion import gitref, store
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    _git(store_dir, "remote", "add", "origin", str(bare))
    (store_dir / "notes.jsonl").write_text("")
    _git(store_dir, "add", "-A")
    _git(store_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c0")
    assert _bare_push(store_dir).returncode != 0, "no upstream yet: a bare push must fail here"
    assert gitref.unpushed(store_dir) == 1

    assert run("init", store_dir=store_dir) == 1
    assert "will set upstream origin/" in capsys.readouterr().out
    assert _bare_push(store_dir).returncode != 0, "a run without --yes sets nothing"
    assert run("init", "--yes", store_dir=store_dir) == 0
    r = _bare_push(store_dir)
    assert r.returncode == 0, r.stderr
    assert gitref.unpushed(store_dir) == 0


def test_init_leaves_a_store_without_a_remote_alone(repo, tmp_path):
    from symbion import gitref
    store_dir = tmp_path / "store"
    assert run("init", "--yes", store_dir=store_dir) == 0
    assert gitref.unpushed(store_dir) is None
    cfg = subprocess.run(["git", "-C", str(store_dir), "config", "--get-regexp", r"branch\..*\.remote"],
                         capture_output=True, text=True)
    assert cfg.stdout == "", cfg.stdout
