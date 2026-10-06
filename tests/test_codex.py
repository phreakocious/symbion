"""`init --agent codex`: the skill link and the hook, in a temporary HOME."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from symbion import cli, store

SKILL = Path(__file__).resolve().parents[1] / "src/symbion/data/skill"


@pytest.fixture(autouse=True)
def default_codex_home(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)


@pytest.mark.parametrize("agent", ["codex", "both"])
def test_install_preview_apply_and_rerun(tmp_path, capsys, agent):
    notes = tmp_path / "notes"
    args = ["--dir", str(notes), "init", "--agent", agent]
    assert cli.main(args) == 1
    assert not notes.exists()
    home = Path.home()
    assert not (home / ".agents").exists()
    assert "run /hooks" not in capsys.readouterr().out
    assert cli.main([*args, "--yes"]) == 0
    # Its own line: at the end of the hook's line it went unread.
    assert "\nnote: Codex runs this hook only after you trust it: in Codex, run /hooks" \
        in capsys.readouterr().out
    link = home / ".agents/skills/symbion"
    assert link.resolve() == SKILL  # a junction when Windows disallows symlinks
    assert (home / ".claude/skills/symbion").exists() == (agent == "both")
    hook_file = home / ".codex/hooks.json"
    entry = json.loads(hook_file.read_text())["hooks"]["SessionStart"][0]
    assert entry["matcher"] == "startup|resume|clear|compact"
    assert entry["hooks"][0]["command"] == cli._CODEX_HOOK_COMMAND
    assert entry == cli._codex_hook_entry(link)
    before = hook_file.read_bytes()
    capsys.readouterr()
    assert cli.main([*args, "--yes"]) == 0
    assert hook_file.read_bytes() == before
    out = capsys.readouterr().out
    # A re-run that changes nothing repeats no setup advice but the trust
    # step, which init cannot see: Codex skips an untrusted hook in silence.
    assert "linked" not in out and out.count("run /hooks") == 1, out


@pytest.mark.parametrize("contents", ['{"hooks": {"Stop": []}}\n', "{invalid"])
def test_codex_preserves_existing_config_and_foreign_skill(tmp_path, monkeypatch, capsys, contents):
    custom = tmp_path / "custom codex"
    custom.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(custom))
    settings = custom / "hooks.json"
    settings.write_text(contents)
    link = Path.home() / ".agents/skills/symbion"
    link.mkdir(parents=True)
    (link / "SKILL.md").write_text("mine")
    assert cli.main(["--dir", str(tmp_path / "notes"), "init", "--agent", "codex", "--yes"]) == 0
    assert settings.read_text() == contents
    assert (link / "SKILL.md").read_text() == "mine"
    out = capsys.readouterr().out
    assert "left alone" in out and "run /hooks to review it" in out


def test_the_installed_codex_hook_reads_its_session_cwd_as_codex(tmp_path, monkeypatch):
    """Codex runs a hook in the session's cwd. A CLAUDE_PROJECT_DIR inherited
    from a Claude Code parent must not pick another project, and the summary's
    reader is codex, so a row codex wrote is not listed as someone else's."""
    home = tmp_path / "home with spaces"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    project = tmp_path / "project with spaces"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    notes = tmp_path / "project with spaces-notes"
    subprocess.run([sys.executable, "-c",
                    'from symbion.cli import main; main(["init", "--agent", "codex", "--yes"])'],
                   cwd=project, check=True, capture_output=True)
    for author, body in (("codex", "my parked idea"), ("human", "owner parked idea")):
        store.add(notes, kind="idea", target={"type": "project", "name": None},
                  author=author, body=body, status="open")
    handler = json.loads((Path.home() / ".codex/hooks.json").read_text()
                         )["hooks"]["SessionStart"][0]["hooks"][0]
    command = handler["commandWindows" if os.name == "nt" else "command"]
    git = Path(shutil.which("git")).parent       # /usr/local/bin on OpenBSD
    # A bare base PATH proves the hook needs nothing from the developer's;
    # Windows keeps its own, where powershell.exe lives.
    base = os.environ["PATH"] if os.name == "nt" else "/usr/bin:/bin"
    env = {**os.environ, "PATH": os.pathsep.join([str(Path(sys.executable).parent), str(git), base]),
           "CLAUDE_PROJECT_DIR": str(tmp_path)}
    if os.name == "nt":
        env["SYMBION_PROJECT_DIR"] = str(tmp_path)  # Windows always uses the session cwd
    result = subprocess.run(command, shell=True, cwd=project, env=env, text=True,
                            input=json.dumps({"cwd": str(project), "source": "startup"}),
                            capture_output=True)
    assert result.returncode == 0 and result.stderr == ""
    assert "owner parked idea" in result.stdout
    assert "my parked idea" not in result.stdout
    # Only the Codex link exists here, and the summary names it.
    assert "~/.agents/skills/symbion/SKILL.md" in result.stdout


def test_inline_codex_hook_does_not_get_registered_twice(tmp_path, capsys):
    settings = Path.home() / ".codex/config.toml"
    settings.parent.mkdir()
    contents = '''[[hooks.SessionStart]]
matcher = "startup|resume|clear|compact"
[[hooks.SessionStart.hooks]]
type = "command"
command = 'bash "$HOME/.agents/skills/symbion/session_start.sh"'
'''
    settings.write_text(contents)
    assert cli.main(["--dir", str(tmp_path / "notes"), "init", "--agent", "codex", "--yes"]) == 0
    assert settings.read_text() == contents
    assert not settings.with_name("hooks.json").exists()
    assert f"kept hook in {settings}" in capsys.readouterr().out
