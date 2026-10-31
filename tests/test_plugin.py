"""The Claude Code plugin (spec 2026-10-30): a thin plugin whose stub skill and
shims reach the installed package, so the skill always matches its CLI."""
import json
import os
import re
import subprocess
import tomllib
from importlib import resources
from pathlib import Path

import pytest
from symbion import cli, store

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
START = PLUGIN / "hooks" / "session_start.sh"
GOTCHAS = PLUGIN / "hooks" / "gotchas.sh"
SKILL_DIR = Path(str(resources.files("symbion") / "data" / "skill")).resolve()
NO_STORE = ("This project has no symbion store yet: /symbion:setup makes one, and "
            "symbion schema lists its kinds after that.")
TO_SETUP = ("symbion: this project has a symbion store, but the symbion CLI is not "
            "installed or does not run: run /symbion:setup.\n")


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


def test_skill_path_names_the_package_skill_dir(repo, capsys):
    assert cli.main(["skill", "--path"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == SKILL_DIR.as_posix()
    assert "\\" not in out


def test_skill_inlines_the_schema_and_names_the_files_dir(repo, tmp_path, capsys):
    """Text that Claude Code injects is not expanded again (the spike), so the
    skill's own `!`symbion schema`` would reach the agent as a literal."""
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    assert cli.main(["--dir", str(s), "schema"]) == 0
    schema = capsys.readouterr().out
    assert cli.main(["--dir", str(s), "skill"]) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first == (f"symbion's skill files (adoption.md, catalogs.md, mechanics.md, "
                     f"wrap.md) are in {SKILL_DIR.as_posix()}.")
    assert "!`symbion schema`" not in out and "\n---\n" not in out and "name: symbion" not in out
    assert schema.strip() in out
    last = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").rstrip().splitlines()[-1]
    assert out.rstrip().endswith(last)


def test_skill_without_a_store_says_how_to_make_one(repo, capsys):
    assert cli.main(["skill"]) == 0
    out = capsys.readouterr().out
    assert NO_STORE in out and "!`symbion schema`" not in out


def test_skill_outside_a_git_repo_still_prints(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert cli.main(["skill"]) == 0
    assert NO_STORE in capsys.readouterr().out
    assert cli.main(["skill", "--path"]) == 0
    assert capsys.readouterr().out.strip() == SKILL_DIR.as_posix()


def _description(skill_md: Path) -> str:
    # A double-quoted YAML scalar with no YAML-only escapes reads as JSON.
    line = next(l for l in skill_md.read_text(encoding="utf-8").splitlines()
                if l.startswith("description:"))
    return json.loads(line.split(":", 1)[1].strip())


def test_plugin_version_matches_the_package():
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    for agent in (".claude-plugin", ".antigravity-plugin"):
        manifest = json.loads((PLUGIN / agent / "plugin.json").read_text())
        assert manifest["name"] == "symbion", agent
        assert manifest["version"] == re.sub(r"\.dev\d+$", "", version), agent


def test_marketplace_lists_the_plugin_dir():
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "symbion" and market["owner"] == {"name": "phreakocious"}
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("symbion", "./plugin")]


def test_plugin_hooks_run_the_shims():
    start = {"matcher": "startup|resume|clear|compact",
             "hooks": [{"type": "command",
                        "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/session_start.sh"',
                        "timeout": 10, "statusMessage": "Reading symbion entries..."}]}
    gotcha = {"matcher": "Read|Edit|Write|NotebookEdit|Bash",
              "hooks": [{"type": "command",
                         "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/gotchas.sh"', "timeout": 5}]}
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert hooks == {"hooks": {"SessionStart": [start], "PostToolUse": [gotcha],
                               "PostToolUseFailure": [gotcha]}}


def test_agy_reads_its_own_hooks_file_beside_claude_codes():
    """agy reads hooks.json at the plugin root, in its own format: Claude
    Code's nested form loads there as one hook named `hooks`, which agy
    rejects. It runs the hook in the plugin's dir (agy 1.4.12, 2026-10-30)."""
    hooks = json.loads((PLUGIN / "hooks.json").read_text())
    assert hooks == {"symbion": {"SessionStart": [
        {"type": "command", "command": "sh hooks/session_start.sh antigravity", "timeout": 10}]}}
    assert cli.AGY_PLUGIN_INSTALL.endswith("/tree/main/plugin")


def test_stub_skill_triggers_as_the_full_skill_does():
    stub = PLUGIN / "skills" / "symbion" / "SKILL.md"
    assert _description(SKILL_DIR / "SKILL.md") in _description(stub)
    assert "plugin was installed" in _description(stub)
    (line,) = re.findall(r"!`([^`]*)`", stub.read_text(encoding="utf-8"))
    # its error is all the agent sees of a failure: with stderr dropped, a
    # broken symbion.toml read as "not installed" (the final review)
    assert line.startswith("symbion skill ") and "|| echo" in line and "2>/dev/null" not in line


# The shims run with a PATH of /usr/bin:/bin and, where a test wants one, a fake
# `symbion` that logs each call and points `skill --path` at a fake skill dir.

def _env(tmp_path, xdg=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home), "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}
    if xdg:
        env["XDG_CACHE_HOME"] = str(xdg)
    return env


def _cache(env) -> Path:
    return Path(env.get("XDG_CACHE_HOME") or Path(env["HOME"]) / ".cache") / "symbion" / "skill_dir"


def _skill(d: Path) -> Path:
    d.mkdir(parents=True)
    (d / "session_start.sh").write_text("echo PKG-START\ncat\n")
    (d / "gotchas.sh").write_text("cat\necho PKG-GOTCHAS\n")
    return d


def _cli(tmp_path, skill: Path | None, broken=False) -> Path:
    """The fake symbion; returns its call log."""
    log = tmp_path / "symbion.log"
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    answer = "exit 1" if broken or skill is None else f"echo '{skill}'; exit 0"
    (b / "symbion").write_text(f'#!/bin/sh\necho "$*" >> \'{log}\'\n'
                               f'[ "$1 $2" = "skill --path" ] && {{ {answer}; }}\nexit 1\n')
    (b / "symbion").chmod(0o755)
    return log


def _project(parent: Path) -> Path:
    p = parent / "p"
    p.mkdir(parents=True)
    _git(p, "init", "-q", "-b", "main")
    return p


def _run(script, project, env, stdin=""):
    return subprocess.run(["sh", str(script)], cwd=project, input=stdin, text=True,
                          capture_output=True, env={**env, "CLAUDE_PROJECT_DIR": str(project)})


def test_start_shim_is_silent_where_nothing_adopted_symbion(tmp_path):
    r = _run(START, _project(tmp_path), _env(tmp_path))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_start_shim_sends_an_adopted_project_to_setup(tmp_path):
    env = _env(tmp_path)
    sibling = _project(tmp_path / "a")
    (tmp_path / "a" / "p-notes").mkdir()
    (tmp_path / "a" / "p-notes" / "notes.jsonl").write_text("")
    pointer = _project(tmp_path / "b")
    (pointer / ".symbion").write_text("../elsewhere-notes\n")
    named = _project(tmp_path / "c")
    for project, extra in ((sibling, {}), (pointer, {}), (named, {"SYMBION_DIR": "/nowhere"})):
        r = _run(START, project, {**env, **extra})
        assert (r.returncode, r.stdout) == (0, TO_SETUP), project


def test_start_shim_finds_the_package_and_caches_it(tmp_path):
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    log = _cli(tmp_path, skill)
    r = _run(START, _project(tmp_path), env, stdin='{"session_id": "s1"}\n')
    assert (r.returncode, r.stdout) == (0, 'PKG-START\n{"session_id": "s1"}\n')
    assert _cache(env).read_text() == f"{skill}\n"
    assert log.read_text().splitlines() == ["skill --path"]


def test_start_shim_follows_the_cli_not_the_cache(tmp_path):
    """Each session start asks symbion (the gotcha hook never does), so the hook
    runs the scripts of the CLI on PATH, not of another install the cache named."""
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    other = _skill(tmp_path / "other" / "skill")
    (other / "session_start.sh").write_text("echo OTHER-START\n")
    log = _cli(tmp_path, skill)
    _cache(env).parent.mkdir(parents=True)
    _cache(env).write_text(f"{other}\n", newline="\n")
    r = _run(START, _project(tmp_path), env)
    assert r.stdout == "PKG-START\n" and log.read_text().splitlines() == ["skill --path"]
    assert _cache(env).read_text() == f"{skill}\n"


def test_start_shim_treats_a_broken_symbion_as_missing_with_a_warm_cache(tmp_path):
    """A pipx venv whose Python went away keeps its site-packages: the cache
    still names a real dir, and the hook ran that dir's script, which ran the
    broken CLI ('bad interpreter'). The final review found it, 2026-10-30."""
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    _cli(tmp_path, None, broken=True)
    _cache(env).parent.mkdir(parents=True)
    _cache(env).write_text(f"{skill}\n", newline="\n")
    (tmp_path / "a" / "p-notes").mkdir(parents=True)
    (tmp_path / "a" / "p-notes" / "notes.jsonl").write_text("")
    r = _run(START, _project(tmp_path / "a"), env)
    assert (r.returncode, r.stdout) == (0, TO_SETUP)


def test_start_shim_finds_the_package_again_when_the_cache_is_stale(tmp_path):
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    log = _cli(tmp_path, skill)
    _cache(env).parent.mkdir(parents=True)
    _cache(env).write_text(f"{tmp_path / 'gone'}\n", newline="\n")
    r = _run(START, _project(tmp_path), env)
    assert r.stdout == "PKG-START\n" and log.read_text().splitlines() == ["skill --path"]
    assert _cache(env).read_text() == f"{skill}\n"


def test_start_shim_treats_a_broken_symbion_as_missing(tmp_path):
    env = _env(tmp_path)
    _cli(tmp_path, None, broken=True)
    r = _run(START, _project(tmp_path / "a"), env)
    assert (r.returncode, r.stdout) == (0, "")
    (tmp_path / "b" / "p-notes").mkdir(parents=True)
    (tmp_path / "b" / "p-notes" / "notes.jsonl").write_text("")
    r = _run(START, _project(tmp_path / "b"), env)
    assert (r.returncode, r.stdout) == (0, TO_SETUP)
    assert not _cache(env).exists()


# shortcut: session_start.sh reads workspacePaths with sed, which takes no `\`; untested what
# agy sends on Windows. A Windows run of agy settles it, then parse in Python if it must.
AGY_PATHS = pytest.mark.skipif(os.name == "nt", reason="agy on Windows is untested")


def _run_agy(tmp_path, env, stdin):
    """As agy runs the hook: in the plugin's dir, the project named on stdin."""
    where = tmp_path / "plugin-copy"
    where.mkdir(exist_ok=True)
    return subprocess.run(["sh", str(START), "antigravity"], cwd=where, input=stdin, text=True,
                          capture_output=True, env=env)


@AGY_PATHS
def test_start_shim_under_agy_takes_the_project_from_its_stdin(tmp_path):
    spaced = tmp_path / "with space"
    env, skill = _env(spaced), _skill(spaced / "pkg" / "skill")
    (skill / "session_start.sh").write_text('echo "PKG-START $SYMBION_PROJECT_DIR"\ncat\n')
    _cli(spaced, skill)
    project = _project(spaced)
    for seps in ((", ", ": "), (",", ":")):   # protojson may space its output, or not
        call = json.dumps({"conversationId": "c1", "workspacePaths": [str(project)]},
                          separators=seps)
        r = _run_agy(spaced, env, call)
        assert (r.returncode, r.stdout) == (0, f"PKG-START {project}\n{call}\n"), seps


def test_start_shim_under_agy_without_a_workspace_says_nothing(tmp_path):
    env = _env(tmp_path)
    log = _cli(tmp_path, _skill(tmp_path / "pkg" / "skill"))
    r = _run_agy(tmp_path, env, '{"conversationId": "c1", "workspacePaths": []}')
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "") and not log.exists()


@AGY_PATHS
def test_start_shim_under_agy_speaks_json_when_the_cli_does_not_run(tmp_path):
    """agy reads only JSON, and has no /symbion:setup to send anyone to."""
    env = _env(tmp_path)
    _cli(tmp_path, None, broken=True)
    project = _project(tmp_path / "a")
    (tmp_path / "a" / "p-notes").mkdir()
    (tmp_path / "a" / "p-notes" / "notes.jsonl").write_text("")
    r = _run_agy(tmp_path, env, json.dumps({"workspacePaths": [str(project)]}))
    (step,) = json.loads(r.stdout)["injectSteps"]
    said = step["systemMessage"]["systemMessage"]
    assert "the symbion CLI is not installed or does not run" in said and "/symbion:" not in said


def test_shims_quote_paths_with_spaces(tmp_path):
    spaced = tmp_path / "with space"
    env, skill = _env(spaced), _skill(spaced / "pkg" / "skill")
    _cli(spaced, skill)
    project = _project(spaced)
    assert _run(START, project, env).stdout == "PKG-START\n"
    assert _run(GOTCHAS, project, env, stdin="{}\n").stdout == "{}\nPKG-GOTCHAS\n"


def test_shims_honor_xdg_cache_home(tmp_path):
    env, skill = _env(tmp_path, xdg=tmp_path / "xdg"), _skill(tmp_path / "pkg" / "skill")
    _cli(tmp_path, skill)
    project = _project(tmp_path)
    assert _run(START, project, env).stdout == "PKG-START\n"
    assert (tmp_path / "xdg" / "symbion" / "skill_dir").read_text() == f"{skill}\n"
    assert not (Path(env["HOME"]) / ".cache").exists()
    assert _run(GOTCHAS, project, env, stdin="{}\n").stdout == "{}\nPKG-GOTCHAS\n"


def test_gotcha_shim_hands_its_input_to_the_package_script(tmp_path):
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    log = _cli(tmp_path, skill)
    _cache(env).parent.mkdir(parents=True)
    _cache(env).write_text(f"{skill}\n", newline="\n")
    call = '{"session_id": "s1", "tool_input": {"file_path": "f"}}\n'
    r = _run(GOTCHAS, _project(tmp_path), env, stdin=call)
    assert (r.returncode, r.stdout) == (0, call + "PKG-GOTCHAS\n") and not log.exists()


def test_gotcha_shim_without_a_cache_is_silent(tmp_path):
    env = _env(tmp_path)
    log = _cli(tmp_path, _skill(tmp_path / "pkg" / "skill"))
    r = _run(GOTCHAS, _project(tmp_path), env, stdin="{}\n")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "") and not log.exists()


def test_setup_command_carries_the_specs_strings():
    """/symbion:setup is prose an agent follows, so this holds what can be held:
    the spec's exact strings, the plugin's own version as the CLI it needs, an
    approval at the install and at adoption, and no bare pip."""
    text = (PLUGIN / "commands" / "setup.md").read_text(encoding="utf-8")
    version = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    for s in ("uv tool install --python '>=3.11' 'symbion[gui]'", "pipx install 'symbion[gui]'",
              "uv tool upgrade symbion", "pipx upgrade symbion", f"symbion {version} or later",
              "- We use symbion for durable notes and tickets.", "https://symbion.nullphase.net/",
              "`symbion init`", "`symbion init --yes`", "adoption.md", "symbion serve",
              "symbion commit", '!`symbion -V 2>&1 || echo "symbion: not installed"`',
              '!`git rev-parse --show-toplevel 2>/dev/null || echo "not a git repository"`'):
        assert s in text, s
    # Claude Code aborts the whole command when an injected line exits non-zero
    # (a stranger run, outside a git repo), so each one must end in a fallback.
    for line in re.findall(r"!`([^`]*)`", text):
        assert "|| echo" in line, line
    assert "pip install" not in text.replace("pipx install", "")
    assert "not a git repository" in text
    steps = re.split(r"\n(?=\d+\. )", text)
    for marker in ("`symbion init --yes`", "adoption.md"):
        step = next(s for s in steps if marker in s)
        assert "approv" in step, (marker, step)
    # uv and pipx put the command in a bin dir a stock PATH may lack, and their
    # fixes (update-shell, ensurepath) write a shell's rc file: the final review.
    install = next(s for s in steps if "`symbion init --yes`" in s)
    for s in ("command -v symbion", "uv tool update-shell", "pipx ensurepath",
              "ask before any change to a shell's rc file"):
        assert s in install, s
    assert len(text.encode()) < 4096, "it loads into context on every /symbion:setup"
    assert "in full" in next(s for s in steps if "adoption.md" in s), "a dialog alone hid the table"
    assert "--no-browser" in next(s for s in steps if "symbion serve" in s and s[0] == "8")


def test_wrap_command_sends_the_agent_to_the_package_wrap():
    """/symbion:wrap: `/symbion wrap` was the user skill's, and the plugin has
    no user skill (the user, 2026-10-30). The steps stay in wrap.md, found as
    setup finds adoption.md."""
    text = (PLUGIN / "commands" / "wrap.md").read_text(encoding="utf-8")
    assert "read `wrap.md` in the directory `symbion skill --path` prints" in text
    assert "`symbion:symbion`" in text and (SKILL_DIR / "wrap.md").is_file()
    assert not re.findall(r"!`([^`]*)`", text)
