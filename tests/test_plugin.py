"""The Claude Code plugin (spec 2026-10-30): a thin plugin whose stub skill and
shims reach the installed package, so the skill always matches its CLI."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
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
                     f"wrap.md) are in {SKILL_DIR.as_posix()}; `symbion skill NAME` "
                     f"prints one (`symbion skill wrap`).")
    assert "!`symbion schema`" not in out and "\n---\n" not in out and "name: symbion" not in out
    assert schema.strip() in out
    last = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").rstrip().splitlines()[-1]
    assert out.rstrip().endswith(last)


# The bytes of `symbion skill` on a fresh store, less its first line, which names
# this machine's path: 11,923 on 2026-11-03, plus 5%.
SKILL_BUDGET = 12519


def test_the_skill_an_agent_loads_stays_in_its_budget(repo, tmp_path, capsys):
    """SKILL.md and its schema table load into every session that writes: under
    16 KB, and a line goes in only when another comes out (the user, 2026-10-14).
    A ratchet holds the second (the user, 2026-11-03): a cut lowers the budget,
    and a raise goes in a commit that says why."""
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    assert cli.main(["--dir", str(s), "skill"]) == 0
    size = len(capsys.readouterr().out.split("\n", 1)[1].encode())
    assert size <= SKILL_BUDGET, f"{size} bytes: cut a line, or raise SKILL_BUDGET in a commit that says why"
    assert round(size * 1.05) >= SKILL_BUDGET, f"{size} bytes: lower SKILL_BUDGET to {round(size * 1.05)}"


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


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root reads a mode-0 file")
@pytest.mark.skipif(os.name == "nt", reason="chmod cannot make a file unreadable on Windows")
def test_skill_prints_when_the_store_is_unreadable(repo, tmp_path, capsys):
    """A sandboxed agent shell (agy's) stats the sibling store and refuses to
    open its symbion.toml: the stub's `symbion skill` printed a traceback, and
    no skill. The store exists, so the skill must not say it is missing."""
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text("")
    (s / "symbion.toml").chmod(0)
    try:
        assert cli.main(["skill"]) == 0
        out = capsys.readouterr().out
        assert cli.main(["skill", "--path"]) == 0
        path = capsys.readouterr().out.strip()
    finally:
        (s / "symbion.toml").chmod(0o644)
    assert out.startswith("symbion's skill files") and NO_STORE not in out
    assert "could not read this project's store" in out and "symbion.toml" in out
    assert path == SKILL_DIR.as_posix()


def test_skill_path_answers_when_the_store_config_is_broken(repo, tmp_path, capsys):
    """The plugin's hook asks `skill --path` first and reads a failure as a
    CLI that does not run. A bad key in symbion.toml failed it, so the hook
    sent the user to /symbion:setup, and the package's hook, which names the
    key, never ran (audit 2026-11-02)."""
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text('provenance = "x"\n')
    assert cli.main(["skill", "--path"]) == 0
    assert capsys.readouterr().out.strip() == SKILL_DIR.as_posix()


def test_skill_name_prints_that_file_before_any_store(repo, tmp_path, capsys):
    """Setup's Read of adoption.md, in the installed package, asked Claude
    Code's "read outside the working directories" question of each new user
    (a stranger run, 2026-11-04). `symbion skill adoption` prints it through
    the shell call the plugin already makes. Like `--path`, it answers
    before any store resolves, a broken symbion.toml included."""
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text('provenance = "x"\n')
    for name, f in (("adoption", "adoption.md"), ("wrap.md", "wrap.md"), ("catalogs", "catalogs.md"),
                    ("mechanics", "mechanics.md")):
        assert cli.main(["skill", name]) == 0, name
        assert capsys.readouterr().out == (SKILL_DIR / f).read_text(encoding="utf-8"), name
    assert cli.main(["skill", "SKILL"]) == 2
    assert "one of adoption, catalogs, mechanics, wrap" in capsys.readouterr().err


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
    # Hermes's manifest is YAML, which the standard library cannot read.
    hermes = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(r"^name: symbion$", hermes, re.M)
    assert re.search(r'^version: "(.+)"$', hermes, re.M)[1] == re.sub(r"\.dev\d+$", "", version)


def test_the_plugin_readme_covers_each_host_and_scopes_the_upgrade():
    """Hermes's catalog clones plugin/ alone, README included, and bans
    self-updating code: the upgrade is Claude Code's, and says so."""
    text = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "Hermes Agent" in text.splitlines()[0]
    assert "hermes plugins install phreakocious/symbion#plugin --enable" in text
    assert "Under Claude Code, the plugin upgrades the installed symbion itself" in text


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


@pytest.mark.parametrize("pointer", [None, "\n", " \r\n\t\n"])
def test_start_shim_is_silent_where_nothing_adopted_symbion(tmp_path, pointer):
    """A blank `.symbion` falls through, as the package's hook reads it: the
    shim's nonempty test sent such a project to setup at each session start
    (audit, 2026-11-12)."""
    p = _project(tmp_path)
    if pointer is not None:
        (p / ".symbion").write_bytes(pointer.encode())
    r = _run(START, p, _env(tmp_path))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_start_shim_sends_an_adopted_project_to_setup(tmp_path):
    env = _env(tmp_path)
    sibling = _project(tmp_path / "a")
    (tmp_path / "a" / "p-notes").mkdir()
    (tmp_path / "a" / "p-notes" / "notes.jsonl").write_text("")
    pointer = _project(tmp_path / "b")
    (pointer / ".symbion").write_text("../elsewhere-notes\n")
    named = _project(tmp_path / "c")
    projects = [(sibling, {}), (pointer, {}), (named, {"SYMBION_DIR": "/nowhere"})]
    with contextlib.suppress(OSError):                  # Windows may refuse a link
        (_project(tmp_path / "d") / ".symbion").symlink_to(tmp_path / "nowhere")
        projects.append((tmp_path / "d" / "p", {}))     # summary names the broken link
    for project, extra in projects:
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


def test_start_shim_takes_a_crlf_answer_from_the_cli(tmp_path):
    """Python on Windows writes CRLF to a pipe: the path kept its CR, named no
    dir, and a working CLI read as missing (audit, 2026-11-08)."""
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    log = _cli(tmp_path, skill)
    fake = tmp_path / "bin" / "symbion"
    fake.write_text(fake.read_text().replace(f"echo '{skill}'", f"printf '%s\\r\\n' '{skill}'"))
    r = _run(START, _project(tmp_path), env)
    assert (r.returncode, r.stdout) == (0, "PKG-START\n")
    assert _cache(env).read_text() == f"{skill}\n" and log.exists()


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


def _agy_said(r) -> str:
    """The one message an agy hook answer injects."""
    (step,) = json.loads(r.stdout)["injectSteps"]
    return step["systemMessage"]["systemMessage"]


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
        assert (r.returncode, _agy_said(r)) == (0, f"PKG-START {project}\n{call}"), seps


def test_start_shim_under_agy_without_a_workspace_says_nothing(tmp_path):
    env = _env(tmp_path)
    log = _cli(tmp_path, _skill(tmp_path / "pkg" / "skill"))
    r = _run_agy(tmp_path, env, '{"conversationId": "c1", "workspacePaths": []}')
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "") and not log.exists()


@AGY_PATHS
def test_start_shim_names_its_host_to_the_package_hook(tmp_path):
    """The package hook answers in the host's JSON; an env marker can name the
    wrong one when one agent starts in another's terminal."""
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    (skill / "session_start.sh").write_text('echo "PKG-START $SYMBION_HOST"\n')
    _cli(tmp_path, skill)
    project = _project(tmp_path)
    marked = {**env, "ANTIGRAVITY_CONVERSATION_ID": "c1"}
    assert _run(START, project, marked).stdout == "PKG-START claude\n"
    call = json.dumps({"workspacePaths": [str(project)]})
    assert _agy_said(_run_agy(tmp_path, env, call)) == "PKG-START antigravity"


@AGY_PATHS
def test_start_shim_under_agy_puts_plain_text_in_its_json(tmp_path):
    """agy reads only JSON: a broken symbion.toml's error is plain text, and
    reached agy as a hook answer it could not parse (audit, 2026-11-08)."""
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    (skill / "session_start.sh").write_text(
        "printf 'error: could not parse \"a\\\\b\"\\tx\\r\\n\\001next %%s\\n'\n")
    _cli(tmp_path, skill)
    call = json.dumps({"workspacePaths": [str(_project(tmp_path))]})
    r = _run_agy(tmp_path, env, call)
    assert (r.returncode, _agy_said(r)) == (0, 'error: could not parse "a\\b" x\nnext %s')
    (skill / "session_start.sh").write_text("echo '{\"injectSteps\": []}'\n")
    assert _run_agy(tmp_path, env, call).stdout == '{"injectSteps": []}\n'


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


TO_PI_SETUP = ("symbion: this project has a symbion store, but the symbion CLI is not "
               "installed or does not run: run /symbion-setup.\n")


def _run_host(host, project, env, stdin=""):
    """As Pi's extension and Hermes's plugin run the hook: in the project, named
    in SYMBION_PROJECT_DIR."""
    return subprocess.run(["sh", str(START), host], cwd=project, input=stdin, text=True,
                          capture_output=True, env={**env, "SYMBION_PROJECT_DIR": str(project)})


@pytest.mark.parametrize("host", ["pi", "hermes"])
def test_start_shim_under_pi_or_hermes_names_its_host_to_the_package_hook(tmp_path, host):
    env, skill = _env(tmp_path), _skill(tmp_path / "pkg" / "skill")
    (skill / "session_start.sh").write_text('echo "PKG-START $SYMBION_HOST"\ncat\n')
    _cli(tmp_path, skill)
    r = _run_host(host, _project(tmp_path), env, '{"session_id": "p1"}\n')
    assert (r.returncode, r.stdout) == (0, f'PKG-START {host}\n{{"session_id": "p1"}}\n')


@pytest.mark.parametrize("host, line", [("pi", TO_PI_SETUP), ("hermes", TO_SETUP)])
def test_start_shim_under_pi_or_hermes_sends_an_adopted_project_to_setup(tmp_path, host, line):
    """Pi's command is /symbion-setup: its prompt names take no namespace.
    Hermes offers the plugin's setup skill as /symbion:setup, as Claude Code does."""
    env = _env(tmp_path)
    _cli(tmp_path, None, broken=True)
    project = _project(tmp_path / "a")
    (tmp_path / "a" / "p-notes").mkdir()
    (tmp_path / "a" / "p-notes" / "notes.jsonl").write_text("")
    assert _run_host(host, project, env).stdout == line
    assert _run_host(host, _project(tmp_path / "b"), env).stdout == ""


def test_start_shim_under_hermes_names_the_gateways_path_when_no_symbion_is_on_it(tmp_path):
    """Hermes runs the hooks in its gateway, on the PATH it started with, and its
    terminal on a login shell's: the hook said "not installed" while the
    terminal's `symbion -V` answered (2026-11-13), so setup would find the CLI and
    fix nothing. A CLI on PATH that fails keeps the
    setup line (the test above), and so does Pi."""
    env = _env(tmp_path)
    env["PATH"] += ":/a\\b"
    project = _project(tmp_path / "a")
    (tmp_path / "a" / "p-notes").mkdir()
    (tmp_path / "a" / "p-notes" / "notes.jsonl").write_text("")
    said = _run_host("hermes", project, env).stdout
    assert said.startswith("symbion: this project has a symbion store, but the Hermes gateway, ")
    if os.name != "nt":   # Git's sh prints PATH in its own form, `\` turned to `/`
        assert f"finds no symbion on its PATH ({env['PATH']})." in said
    assert "`hermes gateway install --force`" in said and "`hermes gateway stop`" in said
    assert said.endswith("Otherwise run /symbion:setup.\n")
    assert _run_host("pi", project, env).stdout == TO_PI_SETUP
    assert _run_host("hermes", _project(tmp_path / "b"), env).stdout == ""


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
    assert not (PLUGIN / "commands").exists()
    text = (PLUGIN / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    assert _description(PLUGIN / "skills" / "setup" / "SKILL.md") == \
        "Install symbion and set it up in this project, saying what each step writes and asking before each write"
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
    for marker in ("`symbion init --yes`", "Run `symbion skill adoption`"):
        step = next(s for s in steps if marker in s)
        assert "approv" in step, (marker, step)
    # uv and pipx put the command in a bin dir a stock PATH may lack, and their
    # fixes (update-shell, ensurepath) write a shell's rc file: the final review.
    install = next(s for s in steps if "`symbion init --yes`" in s)
    for s in ("command -v symbion", "uv tool update-shell", "pipx ensurepath",
              "ask before any change to a shell's rc file"):
        assert s in install, s
    # init's re-check runs alone: the plugin review's minors (the user, 2026-10-30).
    assert "in a call of its own, run `symbion init` again" in install
    # "each path and what is written there" left out the install command and where it
    # put the CLI (stranger runs, 2026-11-04); an append to a file with no final
    # newline joined its last line (2026-11-06).
    assert "the exact install command and where it puts the CLI" in install
    assert "after a newline if the file lacks a final one" in install
    # agy never reads CLAUDE.md, and its model skipped a parenthetical naming its
    # file; a new AGENTS.md would hide CLAUDE.md from Pi (live run, 2026-11-03).
    store = next(s for s in steps if s.startswith("4. "))
    for s in ("Antigravity never reads `CLAUDE.md`", "else create `GEMINI.md`",
              "`--agent antigravity`", "`/permissions`"):
        assert s in store, s
    # In auto mode, symbion fails in agy's sandbox until those rules are in, and the
    # fact line's `|| echo` reads that as no install (2026-11-03).
    cli_step = next(s for s in steps if s.startswith("3. "))
    for s in ("operation not permitted", "`BypassSandbox`"):
        assert s in cli_step, s
    # Rules asked for after init: each later call took 5-41 s of review and each read
    # outside the workspace prompted. A rule may name a store init has not made, so they
    # go in at the approval, and a sandboxed -V shows they are in (agy trial, 2026-11-03).
    assert "before approving" in store
    assert "`symbion -V` without `BypassSandbox`" in install
    # With no bypass after the rules, the sandbox refused to list the repo's parent and
    # zsh read the sibling-store sweep as `no matches found` (agy run, 2026-11-03).
    # Default mode ran every call outside the workspace with no refusal (agy run,
    # 2026-11-11), so the bypass is for auto mode alone.
    assert ("in auto mode, take `BypassSandbox` only for what `adoption.md` reads "
            "outside the repo") in install
    adoption = (SKILL_DIR / "adoption.md").read_text(encoding="utf-8")
    for s in ("Antigravity's auto mode", "`no matches found`", "in agy, with `BypassSandbox`"):
        assert s in adoption, s
    # agy's read_file refused serve's log in /tmp even with the rules.
    assert "head -1" in next(s for s in steps if "symbion serve" in s and s[0] == "8")
    # 1 KB more for each host's file and init flag (the user, 2026-11-03); Hermes
    # is the second such host (2026-11-11).
    assert len(text.encode()) < 6144, "it loads into context on every /symbion:setup"
    four = next(s for s in steps if s[0] == "4")
    assert "Under Hermes, every `symbion init` takes `--agent hermes`" in four
    assert "Hermes reads the first of `.hermes.md`, `AGENTS.override.md`, `AGENTS.md` and `CLAUDE.md`" in four
    assert "in full" in next(s for s in steps if "Run `symbion skill adoption`" in s), "a dialog alone hid the table"
    assert "--no-browser" in next(s for s in steps if "symbion serve" in s and s[0] == "8")


def test_wrap_command_sends_the_agent_to_the_package_wrap():
    """/symbion:wrap: `/symbion wrap` was the user skill's, and the plugin has
    no user skill (the user, 2026-10-30). The steps stay in wrap.md, printed as
    setup prints adoption.md."""
    text = (PLUGIN / "skills" / "wrap" / "SKILL.md").read_text(encoding="utf-8")
    assert _description(PLUGIN / "skills" / "wrap" / "SKILL.md") == \
        "End the session: put what it knows that no symbion entry holds into the store, then commit the store with the next session's handoff as the message"
    assert "run `symbion skill wrap` and follow it" in text
    assert "`symbion:symbion`" in text and (SKILL_DIR / "wrap.md").is_file()
    assert not re.findall(r"!`([^`]*)`", text)


LIB = PLUGIN / "hooks" / "upgrade_lib.sh"


def _pv(tmp_path, text):
    root = tmp_path / "root"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(text)
    r = subprocess.run(["sh", "-c", '. "$1"; symbion_plugin_version; printf %s "$pv"', "sh", str(LIB)],
                       env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(root)},
                       capture_output=True, text=True, timeout=30)
    return r.stdout


def test_lib_reads_the_plugin_version(tmp_path):
    assert _pv(tmp_path, '{\n  "name": "symbion",\n  "version": "0.16.0",\n') == "0.16.0"
    # a formatter may squeeze plugin.json onto one line
    assert _pv(tmp_path, '{"name":"symbion","version":"0.16.0","x":1}') == "0.16.0"
    assert _pv(tmp_path, '{"name": "symbion"}') == ""
    assert _pv(tmp_path, '{"version": "0.16.0; rm -rf ~"}') == ""


# The upgrade worker runs synchronously here, as the shims launch it detached. The
# fakes answer from tmp_path/bin; each logs its argv as a JSON list to NAME.calls.

# A venv's lib dir as a real one names it: Lib on Windows, where the worker cuts at \Lib\.
_LIB = "Lib" if os.name == "nt" else "lib"


def _venv(tmp_path, marker) -> Path:
    lib = [_LIB] if os.name == "nt" else [_LIB, "python3.12"]
    pkg = tmp_path.joinpath("venv", *lib, "site-packages", "symbion")
    pkg.mkdir(parents=True, exist_ok=True)
    if marker:
        (tmp_path / "venv" / {"uv": "uv-receipt.toml", "pipx": "pipx_metadata.json"}[marker]).write_text("")
    return pkg


def _fake(tmp_path, name, body):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    calls = tmp_path / f"{name}.calls"
    # the argv as a JSON list: the test's arguments hold no quote, and _calls escapes a
    # Windows path's backslashes
    (b / name).write_text('#!/bin/sh\n_j=\nfor _a in "$@"; do _j="$_j${_j:+, }\\"$_a\\""; done\n'
                          f'echo "[$_j]" >> \'{calls}\'\n{body}\n')
    (b / name).chmod(0o755)


def _cli_v(tmp_path, version, loc, crlf=False, warn=False):
    (tmp_path / "cli_version").write_text(version)
    eol = "\\r\\n" if crlf else "\\n"
    # loc is an argument, not part of the format: there `\v` of a Windows `\venv` is an escape.
    _fake(tmp_path, "symbion",
          ('echo "UserWarning: a warning on stderr" >&2\n' if warn else "") +
          f'[ "$1" = "-V" ] && {{ printf "symbion %s from %s{eol}" "$(cat \'{tmp_path / "cli_version"}\')" \'{loc}\'; exit 0; }}\n'
          '[ "$1" = serve ] && exit 0\nexit 1')


def _work(tmp_path, cli, pv, tool=None, uname=None, crlf=False, warn=False) -> Path:
    up = tmp_path / "data" / "upgrade"
    (up / f"hold-{pv}").mkdir(parents=True, exist_ok=True)
    if cli:
        _cli_v(tmp_path, cli[0], cli[1], crlf, warn)
    if tool:    # ok moves the fake CLI to the plugin's version, noop changes nothing, fail is a
        # network error, partial moves it and then fails (uv on Windows when a serve holds
        # the command's file), break leaves a CLI whose -V gives nothing
        act = {"ok": f"printf %s {pv} > '{tmp_path / 'cli_version'}'", "noop": "exit 0",
               "fail": "echo 'error: no network'; exit 1",
               "partial": f"printf %s {pv} > '{tmp_path / 'cli_version'}'; echo 'error: Failed to install entrypoint'; exit 1",
               "break": f"printf '#!/bin/sh\\nexit 1\\n' > '{tmp_path / 'bin' / 'symbion'}'"}[tool]
        for t in ("uv", "pipx"):
            _fake(tmp_path, t, act)
    if uname:
        _fake(tmp_path, "uname", f'[ "$1" = -s ] && echo {uname}')
    env = {**_env(tmp_path), "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    r = subprocess.run(["sh", str(PLUGIN / "hooks" / "upgrade.sh"), pv], env=env,
                       capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    return up


def _calls(tmp_path, name) -> list[list[str]]:
    f = tmp_path / f"{name}.calls"
    return [json.loads(x.replace("\\", "\\\\")) for x in f.read_text().splitlines()] if f.exists() else []


def _log(up) -> list[dict]:
    return [json.loads(x) for x in (up / "log.jsonl").read_text().splitlines()]


def _result(up) -> str:
    """The agent's line, unescaped: the worker JSON-escapes it, so a Windows path's `\\` is doubled."""
    return json.loads('"' + (up / "result").read_text().rstrip("\n") + '"')


def test_worker_without_symbion_closes_the_version_quietly(tmp_path):
    up = _work(tmp_path, cli=None, pv="0.16.0")
    assert (up / "ok-0.16.0").exists() and not (up / "hold-0.16.0").exists()
    assert not (up / "result").exists() and _log(up)[-1]["outcome"] == "none"


@pytest.mark.parametrize("cv", ["0.16.0", "0.16.0.dev3", "0.16.1", "0.16.0rc1", "0.16.0+local"])
def test_worker_leaves_a_current_cli_alone(tmp_path, cv):
    up = _work(tmp_path, cli=(cv, _venv(tmp_path, "uv")), pv="0.16.0")
    assert (up / "ok-0.16.0").exists() and not (up / "result").exists()
    assert _calls(tmp_path, "uv") == []


@pytest.mark.parametrize("waiting, stays", [
    ("symbion: the symbion plugin needs symbion 0.16.0 or later, and `uv tool upgrade symbion` "
     "failed (log: x). Tell the user, who can run it by hand.", False),
    ("symbion: the symbion plugin needs symbion 0.17.0 or later, and this one is 0.15.2", True),
    ("symbion: upgraded from 0.15.2 to 0.16.0 to match the symbion plugin. Tell the user.", True)])
def test_worker_takes_back_a_need_a_current_cli_meets(tmp_path, waiting, stays):
    """A failed upgrade's line waited, the user upgraded by hand, and a later
    run found the CLI current and still sent the line (audit, 2026-11-12). A
    need it does not meet, and news of an upgrade, still go to the agent."""
    up = tmp_path / "data" / "upgrade"
    up.mkdir(parents=True)
    (up / "result").write_text(waiting + "\n", newline="\n")
    up = _work(tmp_path, cli=("0.16.0", _venv(tmp_path, "uv")), pv="0.16.0")
    assert (up / "result").exists() == stays and _log(up)[-1]["outcome"] == "current"


def test_worker_never_touches_an_editable_install(tmp_path):
    up = _work(tmp_path, cli=("0.15.2", tmp_path / "src/symbion"), pv="0.16.0")
    assert "an install the plugin does not upgrade (editable at " in _result(up)
    assert _result(up).endswith(f"(editable at {tmp_path / 'src/symbion'}). Pull its checkout and run "
                                "`pip install -e .` there again to upgrade it. Tell the user.")
    assert (up / "ok-0.16.0").exists() and _calls(tmp_path, "uv") == _calls(tmp_path, "pipx") == []


def test_worker_names_an_install_it_does_not_know(tmp_path):
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path, None)), pv="0.16.0")
    assert "(not from uv tool or pipx at " in _result(up)
    assert _result(up).endswith("). Upgrade it with the tool that installed it. Tell the user.")


def test_worker_tolerates_a_crlf_answer(tmp_path):
    loc = tmp_path / "src/symbion"
    up = _work(tmp_path, cli=("0.15.2", loc), crlf=True, pv="0.16.0")
    assert f"(editable at {loc})." in _result(up)
    assert _log(up)[-1]["kind"] == "editable" and _log(up)[-1]["outcome"] == "notice"
    up = _work(tmp_path, cli=("0.16.0", _venv(tmp_path, "uv")), crlf=True, pv="0.16.0")
    assert _log(up)[-1]["outcome"] == "current"


def test_worker_logs_each_attempt_in_full(tmp_path):
    up = _work(tmp_path, cli=("0.15.2", tmp_path / "src/symbion"), pv="0.16.0")
    (row,) = _log(up)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["time"])
    # when the worker began, beside when it wrote the row: same format, so text order is time order
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["started"]) and row["started"] <= row["time"]
    assert {k: row[k] for k in ("plugin", "before", "kind", "outcome")} == \
        {"plugin": "0.16.0", "before": "0.15.2", "kind": "editable", "outcome": "notice"}


def test_worker_compares_versions_as_numbers(tmp_path):
    up = _work(tmp_path, cli=("0.9.9", tmp_path / "src/symbion"), pv="0.16.0")
    assert _log(up)[-1]["outcome"] == "notice"
    up = _work(tmp_path / "b", cli=("0.100.0", tmp_path / "src/symbion"), pv="0.16.0")
    assert _log(up)[-1]["outcome"] == "current"


@pytest.mark.parametrize("kind,cmd", [("uv", ["tool", "upgrade", "symbion"]), ("pipx", ["upgrade", "symbion"])])
def test_worker_upgrades_with_the_tool_that_made_the_install(tmp_path, kind, cmd):
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path, kind)), pv="0.16.0", tool="ok")
    assert _calls(tmp_path, kind) == [cmd]
    # "the symbion CLI": "upgraded from A to B" alone read as the store's upgrade
    assert _result(up).startswith("symbion: the symbion CLI was upgraded from 0.15.2 to 0.16.0 "
                                  "to match the symbion plugin.")
    # a later session's skill came from the new CLI: no claim that it is the old one's
    assert "If the symbion skill was loaded before this line" in _result(up)
    assert (up / "ok-0.16.0").exists() and not (up / "hold-0.16.0").exists()
    assert (["serve", "--restart"] in _calls(tmp_path, "symbion")) is (os.name != "nt")   # Windows: by hand


def test_worker_cuts_the_venv_at_its_last_lib(tmp_path):
    # the venv sits under another /lib/: a cut at the first one finds no marker
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path / _LIB / "x", "uv")), pv="0.16.0", tool="ok")
    assert _calls(tmp_path, "uv") == [["tool", "upgrade", "symbion"]] and _log(up)[0]["kind"] == "uv"


def test_worker_cuts_only_the_git_suffix_from_the_path(tmp_path):
    # a uv venv under "J (Work)": a cut at the first " (" reads it as editable
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path / "J (Work)", "uv")), pv="0.16.0", tool="ok")
    assert _log(up)[0]["kind"] == "uv"
    loc = tmp_path / "J (Work)" / "src" / "symbion"
    up = _work(tmp_path / "b", cli=("0.15.2", f"{loc} (git 1a2b3c4)"), pv="0.16.0")
    assert f"(editable at {loc})." in _result(up)


def _kind(loc, cwd) -> str:
    r"""upgrade.sh's symbion_install_kind alone: sourcing the file would run the worker. The
    function goes in a file and LOC in the environment: on Windows the command line
    turns each `\\` into `\`, and a `sh -c` of the function read `\\Lib\\` as `\Lib\`,
    which matched nothing."""
    fn = re.search(r"^symbion_install_kind\(\) \{\n.*?^\}\n", (PLUGIN / "hooks" / "upgrade.sh").read_text(),
                   re.M | re.S).group(0)
    (cwd / "kind.sh").write_text(fn + 'symbion_install_kind "$LOC"\n', newline="\n")
    return subprocess.run(["sh", "kind.sh"], cwd=cwd, env={**os.environ, "LOC": loc},
                          capture_output=True, text=True, timeout=30).stdout.strip()


def test_install_kind_reads_a_windows_pipx_venv_with_a_space(tmp_path):
    loc = r"C:\Users\a b\pipx\venvs\symbion\Lib\site-packages\symbion"
    if os.name == "nt":     # no `\` in a name: the same shape, a real path under tmp_path
        venv = tmp_path / "a b" / "pipx" / "venvs" / "symbion"
        loc = str(venv / "Lib" / "site-packages" / "symbion")
    else:                   # `\` is no separator: the venv is one name, relative to the cwd
        venv = tmp_path / loc.rpartition("\\Lib\\")[0]
    venv.mkdir(parents=True)
    (venv / "pipx_metadata.json").write_text("")
    assert _kind(loc, tmp_path) == "pipx"


def test_install_kind_reads_debians_dist_packages_as_other(tmp_path):
    assert _kind("/usr/lib/python3/dist-packages/symbion", tmp_path) == "other"


def test_worker_compares_numbers_not_text(tmp_path):
    _work(tmp_path, cli=("0.9.0", _venv(tmp_path, "pipx")), pv="0.16.0", tool="noop")
    assert _calls(tmp_path, "pipx") == [["upgrade", "symbion"]]    # 0.9.0 < 0.16.0


def test_worker_waits_for_pypi(tmp_path):
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path, "uv")), pv="0.16.0", tool="noop")
    assert not (up / "ok-0.16.0").exists() and (up / "hold-0.16.0").is_dir()
    assert not (up / "result").exists() and _log(up)[-1]["outcome"] == "wait"


def test_worker_tells_a_wait_past_a_day_once(tmp_path):
    """pipx upgrade drops an install's index args, and uv tool upgrade keeps a
    pin: either waits forever, so a wait a day old is told, once a version."""
    cli = ("0.15.2", _venv(tmp_path, "pipx"))
    up = _work(tmp_path, cli=cli, pv="0.16.0", tool="noop")
    assert (up / "wait-0.16.0").exists() and not (up / "result").exists()
    _work(tmp_path, cli=cli, pv="0.16.0", tool="noop")        # an hour on: still quiet
    assert not (up / "result").exists()
    day = time.time() - 25 * 3600
    os.utime(up / "wait-0.16.0", (day, day))
    _work(tmp_path, cli=cli, pv="0.16.0", tool="noop")
    assert _result(up).startswith("symbion: the symbion plugin needs symbion 0.16.0 or later, "
                                  "and a day of `pipx upgrade symbion` has left it at 0.15.2 (log: ")
    assert _log(up)[-1]["outcome"] == "wait" and not (up / "ok-0.16.0").exists()
    (up / "result").unlink()                                   # delivered
    _work(tmp_path, cli=cli, pv="0.16.0", tool="noop")
    assert not (up / "result").exists()
    # the user upgrades by hand: the next run finds the CLI current and takes a waiting line back
    (up / "result").write_text("symbion: the symbion plugin needs symbion 0.16.0 or later, and a day\n")
    _work(tmp_path, cli=("0.16.0", cli[1]), pv="0.16.0")
    assert not (up / "result").exists() and (up / "ok-0.16.0").exists()


@pytest.mark.parametrize("shell", ["sh", "dash", "ksh", "bash"])
def test_every_hook_parses_in_each_shell(shell):
    """OpenBSD's sh is ksh, Debian's is dash: a hook one cannot parse dies
    silently there, and a parse error in upgrade.sh passed every macOS test
    until an OpenBSD run (2026-11-11). macOS has /bin/ksh."""
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"no {shell} on PATH")
    for f in sorted((PLUGIN / "hooks").glob("*.sh")):
        r = subprocess.run([exe, "-n", str(f)], capture_output=True, text=True, timeout=30)
        assert (f.name, r.returncode, r.stderr) == (f.name, 0, "")


def test_worker_reads_the_version_past_a_warning_on_stderr(tmp_path):
    # PYTHONWARNINGS can put a line on stderr ahead of each -V answer
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path, "uv")), pv="0.16.0", tool="ok", warn=True)
    row = _log(up)[0]
    assert (row["before"], row["after"], row["outcome"]) == ("0.15.2", "0.16.0", "upgraded")


def test_worker_reports_a_cli_with_no_version_after_the_upgrade_once(tmp_path):
    cli = ("0.15.2", _venv(tmp_path, "uv"))
    up = _work(tmp_path, cli=cli, pv="0.16.0", tool="break")
    assert "`uv tool upgrade symbion` failed (log: " in _result(up)
    (up / "result").unlink()                                   # delivered
    _work(tmp_path, cli=cli, pv="0.16.0", tool="break")
    assert not (up / "result").exists() and not (up / "ok-0.16.0").exists() and (up / "hold-0.16.0").is_dir()
    rows = _log(up)
    assert [(r["outcome"], r["exit"]) for r in rows] == [("failed", 0), ("failed", 0)]
    assert rows[0]["output"].endswith("symbion -V gave no version after the upgrade")


def test_worker_reports_a_failure_once_per_version(tmp_path):
    cli = ("0.15.2", _venv(tmp_path, "uv"))
    up = _work(tmp_path, cli=cli, pv="0.16.0", tool="fail")
    assert "`uv tool upgrade symbion` failed (log: " in _result(up)
    assert f"(log: {tmp_path / 'data'}/upgrade/log.jsonl)." in _result(up)    # the worker joins with `/`
    (up / "result").unlink()                                   # delivered
    _work(tmp_path, cli=cli, pv="0.16.0", tool="fail")
    assert not (up / "result").exists() and (up / "hold-0.16.0").is_dir()
    assert [r["outcome"] for r in _log(up)] == ["failed", "failed"]
    assert "error: no network" in _log(up)[0]["output"]


def test_worker_logs_the_version_a_failed_command_left(tmp_path):
    """uv on Windows replaced the package, then failed to copy the command a running serve
    held: the row read the version from before the command (live run, 2026-11-12)."""
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path, "uv")), pv="0.16.0", tool="partial", uname="MINGW64_NT-10.0")
    row = _log(up)[-1]
    assert (row["outcome"], row["exit"], row["before"], row["after"]) == ("failed", 1, "0.15.2", "0.16.0")
    assert _result(up).endswith("A running `symbion serve` can hold symbion's files: stop it first.")


def test_worker_on_windows_leaves_serve_to_the_user(tmp_path):
    up = _work(tmp_path, cli=("0.15.2", _venv(tmp_path, "pipx")), pv="0.16.0", tool="ok", uname="MINGW64_NT-10.0")
    assert ["serve", "--restart"] not in _calls(tmp_path, "symbion")
    assert _result(up).endswith("Restart any running `symbion serve` by hand: Ctrl-C it and start it again.")
    up = _work(tmp_path / "b", cli=("0.15.2", _venv(tmp_path / "b", "pipx")), pv="0.16.0", tool="fail", uname="MSYS_NT")
    assert _result(up).endswith("A running `symbion serve` can hold symbion's files: stop it first.")


# The shims launch the worker. A copy of plugin/hooks sits in base/root-PV with its
# upgrade.sh swapped for a stub; fake mkdir, find and git log what the shim runs.

def _data(base, ok=(), hold=None, hold_age_min=0, result=None) -> Path:
    d = Path(base) / "data"
    up = d / "upgrade"
    up.mkdir(parents=True, exist_ok=True)
    for v in [ok] if isinstance(ok, str) else ok:
        (up / f"ok-{v}").write_text("")
    if hold:
        (up / f"hold-{hold}").mkdir(exist_ok=True)
        t = time.time() - hold_age_min * 60
        os.utime(up / f"hold-{hold}", (t, t))
    if result:
        (up / "result").write_text(result + "\n", newline="\n")
    return d


def _shim_args(base, which, pv, data, stdin, stub_sleep, cache=True):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    root = base / f"root-{pv}"
    shutil.copytree(PLUGIN / "hooks", root / "hooks", dirs_exist_ok=True)
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(f'{{"name": "symbion", "version": "{pv}"}}\n')
    (root / "hooks" / "upgrade.sh").write_text(
        f'sleep {stub_sleep}\necho "$1" >> \'{base / "stub.runs"}\'\n')
    for n in ("mkdir", "find", "git"):
        _fake(base, n, f'for _p in /bin /usr/bin; do [ -x "$_p/{n}" ] && exec "$_p/{n}" "$@"; done')
    env = {**_env(base), "CLAUDE_PLUGIN_ROOT": str(root)}
    if data is not None:
        env["CLAUDE_PLUGIN_DATA"] = str(data)
    skill = base / "pkg" / "skill"
    if not skill.exists():
        _skill(skill)
    if cache:
        _cache(env).parent.mkdir(parents=True, exist_ok=True)
        _cache(env).write_text(f"{skill}\n", newline="\n")
    project = base / "p"
    if not project.exists():
        _project(base)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return ["sh", str(root / "hooks" / {"start": "session_start.sh", "gotchas": "gotchas.sh"}[which])], project, env, stdin


def _shim(base, which, pv="0.16.0", data=None, project=None, stdin="{}\n", stub_sleep=0, cache=True):
    cmd, proj, env, stdin = _shim_args(base, which, pv, data, stdin, stub_sleep, cache)
    if project:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    return subprocess.run(cmd, cwd=project or proj, input=stdin, text=True, capture_output=True, env=env, timeout=30)


def _shims_async(base, which, n, pv="0.16.0", data=None):
    """N shims started from one setup: a second _shim_args rewrites the hooks and the
    fakes while the first shim reads them, and it sourced an empty upgrade_lib.sh."""
    cmd, proj, env, stdin = _shim_args(base, which, pv, data, "{}\n", 0)
    procs = [subprocess.Popen(cmd, cwd=proj, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) for _ in range(n)]
    for p in procs:
        # A start shim reads no stdin and may be gone before the write: communicate()
        # ignores the broken pipe for that, and so does this (an OpenBSD run, 2026-11-13).
        with contextlib.suppress(BrokenPipeError):
            p.stdin.write(stdin)
        with contextlib.suppress(BrokenPipeError):
            p.stdin.close()
        # communicate() flushes stdin first: on Python 3.12 a closed one raised
        # ValueError (an Ubuntu run, 2026-11-13); 3.14 skips it.
        p.stdin = None
    return procs


def _stub_runs(base) -> list[str]:
    f = Path(base) / "stub.runs"
    return f.read_text().splitlines() if f.exists() else []


def _wait_for(pred, timeout=10):
    # Returns when pred holds; the bound only fails a slow run: 3 s timed out once
    # in a 3.12 run of this file that took twice its usual time (2026-11-13).
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.05)
    pytest.fail("timed out")


def test_check_does_nothing_without_the_plugin_env(tmp_path):        # agy, Pi
    r = _shim(tmp_path, "gotchas", data=None, stdin="{}\n")
    assert _calls(tmp_path, "mkdir") == [] and r.stdout == "{}\nPKG-GOTCHAS\n"


def test_check_with_its_marker_runs_builtins_only(tmp_path):
    data = _data(tmp_path, ok="0.16.0")
    _shim(tmp_path, "gotchas", pv="0.16.0", data=data)
    assert _calls(tmp_path, "mkdir") == _calls(tmp_path, "find") == _calls(tmp_path, "git") == []


def test_check_with_its_marker_runs_with_an_empty_path(tmp_path):
    data = _data(tmp_path, ok="0.16.0")
    cmd, proj, env, _ = _shim_args(tmp_path, "gotchas", "0.16.0", data, "", 0)
    _cache(env).unlink()
    sh = shutil.which("sh") or pytest.skip("no sh on PATH")
    r = subprocess.run([sh, cmd[1]], cwd=proj, input="", text=True, capture_output=True,
                       env={**env, "PATH": ""}, timeout=30)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not list((data / "upgrade").glob("hold-*"))


def test_check_launches_one_worker_per_version(tmp_path):
    data = _data(tmp_path)
    for _ in range(3):
        assert _shim(tmp_path, "gotchas", pv="0.16.0", data=data).stderr == ""
    _wait_for(lambda: _stub_runs(tmp_path) == ["0.16.0"])
    assert (data / "upgrade/hold-0.16.0").is_dir()


def test_two_starts_at_once_launch_one_worker(tmp_path):              # RF 4
    data = _data(tmp_path)
    procs = _shims_async(tmp_path, "start", 2, pv="0.16.0", data=data)
    assert [p.communicate()[1] for p in procs] == ["", ""]
    _wait_for(lambda: len(_stub_runs(tmp_path)) >= 1)
    time.sleep(0.5)
    assert _stub_runs(tmp_path) == ["0.16.0"]


def test_launch_returns_before_the_worker_ends(tmp_path):
    t = time.monotonic()
    _shim(tmp_path, "gotchas", pv="0.16.0", data=_data(tmp_path), stub_sleep=3)
    assert time.monotonic() - t < 1.5 and _stub_runs(tmp_path) == []
    _wait_for(lambda: _stub_runs(tmp_path) == ["0.16.0"], timeout=6)


@pytest.mark.skipif(os.name == "nt", reason="no process groups; the Windows VM run covers it")
def test_worker_outlives_a_kill_of_the_hooks_process_group(tmp_path):
    """At a hook's timeout Claude Code kills its whole process group, and a worker in
    that group died mid-upgrade (live run, 2026-11-12). It runs in a session of its own."""
    if not (shutil.which("setsid") or shutil.which("perl")):
        pytest.skip("neither setsid nor perl: the worker stays in the hook's group")
    data = _data(tmp_path)
    cmd, proj, env, stdin = _shim_args(tmp_path, "start", "0.16.0", data, "{}\n", 2)
    started = tmp_path / "stub.started"
    root = Path(cmd[1]).parents[1]
    (root / "hooks" / "upgrade.sh").write_text(
        f": > '{started}'\nsleep 2\necho \"$1\" >> '{tmp_path / 'stub.runs'}'\n")
    p = subprocess.Popen(cmd, cwd=proj, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
    p.stdin.write(stdin)
    p.stdin.close()
    _wait_for(started.exists)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(p.pid, 9)
    p.wait(timeout=10)
    _wait_for(lambda: _stub_runs(tmp_path) == ["0.16.0"], timeout=6)


def test_only_a_session_start_retries_a_stale_hold(tmp_path):
    data = _data(tmp_path, hold="0.16.0", hold_age_min=61)
    hold = data / "upgrade/hold-0.16.0"
    mtime = hold.stat().st_mtime
    assert _shim(tmp_path, "gotchas", pv="0.16.0", data=data).stderr == ""
    assert _calls(tmp_path, "find") == [] and hold.stat().st_mtime == mtime
    assert _shim(tmp_path, "start", pv="0.16.0", data=data).stderr == ""
    _wait_for(lambda: _stub_runs(tmp_path) == ["0.16.0"])


def test_a_session_start_keeps_a_hold_under_an_hour_old(tmp_path):
    data = _data(tmp_path, hold="0.16.0", hold_age_min=59)
    hold = data / "upgrade/hold-0.16.0"
    mtime = hold.stat().st_mtime
    assert _shim(tmp_path, "start", pv="0.16.0", data=data).stderr == ""
    # find is the age test; a retry would rmdir the hold and mkdir a new one
    assert len(_calls(tmp_path, "find")) == 1 and hold.stat().st_mtime == mtime
    assert _stub_runs(tmp_path) == []


def test_two_plugin_versions_share_one_data_dir(tmp_path):
    data = _data(tmp_path, ok=["0.16.0", "0.17.0"])
    for pv in ("0.16.0", "0.17.0", "0.16.0"):
        _shim(tmp_path, "gotchas", pv=pv, data=data)
    assert _calls(tmp_path, "mkdir") == []


def test_upgrade_check_takes_paths_with_spaces(tmp_path):             # RF 1
    d = tmp_path / "a b"
    _shim(d, "gotchas", pv="0.16.0", data=_data(d))
    _wait_for(lambda: _stub_runs(d) == ["0.16.0"])


# The gotcha shim hands the worker's result to the agent, once.

def _adopted_project(base) -> Path:
    p = _project(Path(base))
    (Path(base) / "p-notes").mkdir()
    (Path(base) / "p-notes" / "notes.jsonl").write_text("")
    return p


def _deliver(base, stdin, result) -> str:
    data = _data(base, ok="0.16.0", result=result)
    return _shim(base, "gotchas", pv="0.16.0", data=data, project=_adopted_project(base),
                 stdin=stdin, cache=False).stdout


def _deliver_with_package(base, pkg_text, result) -> str:
    skill = Path(base) / "pkg" / "skill"
    _skill(skill)
    (skill / "gotchas.sh").write_text(
        "cat >/dev/null\nprintf '%s\\n' "
        f"'{{\"hookSpecificOutput\":{{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"{pkg_text}\"}}}}'\n")
    data = _data(base, ok="0.16.0", result=result)
    return _shim(base, "gotchas", pv="0.16.0", data=data, project=_adopted_project(base),
                 stdin='{"tool_input":{}}').stdout


def test_result_reaches_the_agent_once(tmp_path):
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded from 0.15.2 to 0.16.0")
    p = _adopted_project(tmp_path)
    first = _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=p, stdin='{"tool_input":{}}', cache=False)
    assert json.loads(first.stdout)["hookSpecificOutput"] == {
        "hookEventName": "PostToolUse", "additionalContext": "symbion: upgraded from 0.15.2 to 0.16.0"}
    assert (first.returncode, first.stderr) == (0, "")
    assert _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=p, stdin='{"tool_input":{}}',
                 cache=False).stdout == ""
    assert not list((data / "upgrade").glob("result*"))


def test_result_waits_for_a_project_that_adopted_symbion(tmp_path):
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded to 0.16.0, distinctive")
    r = _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=_project(tmp_path), stdin="{}", cache=False)
    assert "distinctive" not in r.stdout and (data / "upgrade/result").exists()


def test_result_joins_the_package_gotcha(tmp_path):
    # both texts hold \" \\ and \n escapes, already JSON-escaped as the worker writes them
    out = _deliver_with_package(tmp_path, pkg_text=r'trap \"q\" a\\b\nline', result=r'up \"x\" c\\d\ne')
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == 'up "x" c\\d\ne\ntrap "q" a\\b\nline'
    assert not list((tmp_path / "data/upgrade").glob("result*"))


def test_result_names_the_failure_event(tmp_path):
    out = _deliver(tmp_path, stdin='{"hook_event_name":"PostToolUseFailure","tool_input":{}}', result="x")
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert not list((tmp_path / "data/upgrade").glob("result*"))


def test_result_stays_when_the_package_prints_something_else(tmp_path):
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded")
    r = _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=_adopted_project(tmp_path), stdin="{}")
    assert r.stdout == "{}\nPKG-GOTCHAS\n" and (data / "upgrade/result").read_text() == "symbion: upgraded\n"


def test_unadopted_project_still_gets_the_package_line_unchanged(tmp_path):
    skill = tmp_path / "pkg" / "skill"
    _skill(skill)
    line = '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"trap"}}'
    (skill / "gotchas.sh").write_text(f"cat >/dev/null\necho '{line}'\n")
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded")
    r = _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=_project(tmp_path), stdin="{}")
    assert (r.stdout, r.stderr) == (line + "\n", "") and (data / "upgrade/result").exists()


def test_a_waiting_result_asks_git_nothing_at_a_projects_root(tmp_path):
    """While a result waits, each tool call in a project that never adopted
    symbion ran git twice to find its main worktree, about 70 ms a call until
    one lands in an adopted project (audit, 2026-11-12). A `.git` dir at the
    project's root makes it its own main worktree."""
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded")
    cmd, proj, env, _ = _shim_args(tmp_path, "gotchas", "0.16.0", data, "{}", 0, cache=False)
    (tmp_path / "git.calls").unlink(missing_ok=True)
    r = subprocess.run(cmd, cwd=proj, input="{}", text=True, capture_output=True, env=env, timeout=30)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert (data / "upgrade/result").exists() and not (tmp_path / "git.calls").exists()


def test_a_subagent_leaves_the_result_for_the_main_agent(tmp_path):
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded, distinctive")
    p = _adopted_project(tmp_path)
    sub = _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=p, cache=False,
                stdin='{"agent_id":"a1","tool_input":{}}')
    assert sub.stdout == "" and (data / "upgrade/result").exists()
    main = _shim(tmp_path, "gotchas", pv="0.16.0", data=data, project=p, cache=False, stdin='{"tool_input":{}}')
    assert "distinctive" in json.loads(main.stdout)["hookSpecificOutput"]["additionalContext"]
    assert not list((data / "upgrade").glob("result*"))


def test_delivery_writes_nothing_to_stderr_with_an_empty_path(tmp_path):
    data = _data(tmp_path, ok="0.16.0", result="symbion: upgraded")
    cmd, proj, env, _ = _shim_args(tmp_path, "gotchas", "0.16.0", data, "", 0, cache=False)
    (tmp_path / "p-notes").mkdir()
    (tmp_path / "p-notes/notes.jsonl").write_text("")
    sh = shutil.which("sh") or pytest.skip("no sh on PATH")
    r = subprocess.run([sh, cmd[1]], cwd=proj, input="{}", text=True, capture_output=True,
                       env={**env, "PATH": ""}, timeout=30)
    assert r.returncode == 0 and r.stderr == ""


def _backslash_hook(tmp_path, name) -> tuple[list[str], Path | None]:
    """argv and cwd that run hooks/NAME through a $0 with `\\` and no `/`, as Pi on
    Windows does (Node's path.join). Elsewhere `\\` is no separator, so a file named
    `hooks\\NAME` beside a copy of hooks/ gives the shell that same $0."""
    if os.name == "nt":
        return ["sh", str(PLUGIN / "hooks" / name)], None
    w = tmp_path / "w"
    shutil.copytree(PLUGIN / "hooks", w / "hooks", dirs_exist_ok=True)
    shutil.copy(PLUGIN / "hooks" / name, w / f"hooks\\{name}")
    return ["sh", f"hooks\\{name}"], w


def test_hooks_find_their_library_through_a_backslash_path(tmp_path):
    env = {**_env(tmp_path), "CLAUDE_PROJECT_DIR": str(_adopted_project(tmp_path))}   # no symbion
    cmd, cwd = _backslash_hook(tmp_path, "session_start.sh")
    r = subprocess.run(cmd, cwd=cwd, input="", text=True, capture_output=True, env=env, timeout=30)
    assert (r.stdout, r.stderr) == (TO_SETUP, "")
    data = _data(tmp_path, result="symbion: upgraded, distinctive")
    env = {**env, "CLAUDE_PLUGIN_DATA": str(data), "CLAUDE_PLUGIN_ROOT": str(tmp_path / "no-root")}
    cmd, cwd = _backslash_hook(tmp_path, "gotchas.sh")
    r = subprocess.run(cmd, cwd=cwd, input='{"tool_input":{}}', text=True, capture_output=True, env=env, timeout=30)
    assert "distinctive" in r.stdout and r.stderr == ""
    cmd, cwd = _backslash_hook(tmp_path, "upgrade.sh")
    subprocess.run([*cmd, "0.16.0"], cwd=cwd, text=True, capture_output=True, env=env, timeout=30)
    assert _log(data / "upgrade")[-1]["outcome"] == "none"
