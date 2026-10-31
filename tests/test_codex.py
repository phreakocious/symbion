"""`init --agent codex`: the skill link and the hook, in a temporary HOME."""
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

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
    assert not (home / ".claude").exists()      # Claude Code's half comes with the plugin
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
    assert "linked" not in out and out.count("run /hooks") == 2, out


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
    for author, body in (("codex", "my parked idea"), ("human", "user parked idea")):
        store.add(notes, kind="idea", target={"type": "project", "name": None},
                  author=author, body=body, status="open")
    handler = json.loads((Path.home() / ".codex/hooks.json").read_text()
                         )["hooks"]["SessionStart"][0]["hooks"][0]
    command = handler["commandWindows" if os.name == "nt" else "command"]
    git = Path(shutil.which("git")).parent       # /usr/local/bin on OpenBSD
    # A bare base PATH proves the hook needs nothing from the developer's;
    # Windows keeps its own, where powershell.exe lives.
    base = os.environ["PATH"] if os.name == "nt" else "/usr/bin:/bin"
    env = {**os.environ, "PATH": os.pathsep.join([str(Path(sysconfig.get_path("scripts"))), str(git), base]),
           "CLAUDE_PROJECT_DIR": str(tmp_path)}
    if os.name == "nt":
        env["SYMBION_PROJECT_DIR"] = str(tmp_path)  # Windows always uses the session cwd
    result = subprocess.run(command, shell=True, cwd=project, env=env, text=True,
                            input=json.dumps({"cwd": str(project), "source": "startup"}),
                            capture_output=True)
    assert result.returncode == 0 and result.stderr == ""
    assert "user parked idea" in result.stdout
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
    out = capsys.readouterr().out
    assert f"kept hook in {settings}" in out
    assert "[[hooks.PreToolUse]]" in out


def test_codex_gotchas_before_read_and_patch(tmp_path, monkeypatch):
    """Run the installed handler against the real summary cache. A patch's
    body can name other files; only its headers spend those files' warnings."""
    notes = tmp_path / "notes"
    assert cli.main(["--dir", str(notes), "init", "--agent", "codex", "--yes"]) == 0
    for name in ("README.md", "src/other.py", "src/moved.py"):
        store.add(notes, kind="note", target={"type": "file", "name": name},
                  author="human", tags=["gotcha"], body=f"trap for {name}")
    payload = json.dumps({"session_id": "codex-session", "hook_event_name": "SessionStart"})
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload.encode())))
    assert cli.main(["--dir", str(notes), "summary", "--hook"]) == 0
    hooks = json.loads((Path.home() / ".codex/hooks.json").read_text())["hooks"]
    assert "PreToolUse" in hooks
    handler = hooks["PreToolUse"][0]["hooks"][0]

    def call(tool, args, **extra):
        result = subprocess.run(handler["command"], shell=True, capture_output=True,
                                text=True, input=json.dumps({"session_id": "codex-session",
                                    "hook_event_name": "PreToolUse", "tool_name": tool,
                                    "tool_input": args, **extra}))
        assert result.returncode == 0 and not result.stderr, result
        if not result.stdout:
            return ""
        out = json.loads(result.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        return out["additionalContext"]

    assert call("Bash", {"command": "cat docs/README.md README.md.bak"}) == ""
    assert "trap for README.md" in call("Bash", {"command": "cat README.md"})
    assert call("Bash", {"command": "cat README.md"}) == ""
    assert "trap for README.md" in call("Bash", {"command": "cat README.md"},
                                       transcript_path="subagent.jsonl")
    assert call("Bash", {"command": "cat README.md"}, transcript_path="subagent.jsonl") == ""
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n-a\n+src/other.py\n*** End Patch"
    said = call("apply_patch", {"command": patch})
    assert "trap for README.md" in said and "trap for src/other.py" not in said
    assert call("apply_patch", {"command": patch}) == ""
    assert call("Bash", {"command": "true"}, tool_response="src/other.py") == ""
    moved = "*** Begin Patch\n*** Update File: src/other.py\n*** Move to: src/moved.py\n@@\n-a\n+b\n*** End Patch"
    said = call("apply_patch", {"command": moved})
    assert "trap for src/other.py" in said and "trap for src/moved.py" in said
    assert call("Bash", {"command": "cat src/other.py"}) == ""
    assert call("Bash", {"command": "cat README.md"}, session_id="../bad") == ""


def test_codex_existing_hooks_get_gotcha_merge(tmp_path):
    settings = Path.home() / ".codex/hooks.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"SessionStart": [cli._codex_hook_entry(
        Path.home() / ".agents/skills/symbion")], "PreToolUse": [{"matcher": "foreign"}]}}))
    args = ["--dir", str(tmp_path / "notes"), "init", "--agent", "codex", "--yes", "--merge-hook"]
    assert cli.main(args) == 0
    data = json.loads(settings.read_text())["hooks"]
    assert data["PreToolUse"][0] == {"matcher": "foreign"}
    assert len(data["PreToolUse"]) == 2 and len(data["SessionStart"]) == 1
    before = settings.read_bytes()
    assert cli.main(args) == 0
    assert settings.read_bytes() == before
