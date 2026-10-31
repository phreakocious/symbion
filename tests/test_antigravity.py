"""Antigravity (agy): the hook's JSON, the skill line and `init --agent
antigravity`. agy installs the plugin (tests/test_plugin.py) and reads only its
own hook format (agy 1.4.12, 2026-10-30)."""
import io
import json
import sys
from pathlib import Path

from symbion import cli, store

AGY_IN = b'{"conversationId": "c1", "workspacePaths": ["/p"]}'


def _hook(store_dir, monkeypatch, capsys, stdin: bytes = AGY_IN) -> dict:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    assert cli.main(["--dir", str(store_dir), "summary", "--hook"]) == 0
    return json.loads(capsys.readouterr().out)


def test_session_start_under_agy_speaks_its_inject_steps(tmp_path, monkeypatch, capsys):
    """agy parses no `hookSpecificOutput`, and its `systemMessage` reaches the
    model. A `metadata` beside it made agy 1.4.12 panic and drop the output.
    The session id is the input's `conversationId`."""
    store.ensure_store(tmp_path)
    store.add(tmp_path, kind="task", target={"type": "item", "name": "x"}, author="human",
              body="the canary task")
    store.add(tmp_path, kind="note", target={"type": "doc", "name": "plan.md"}, author="human",
              tags=["gotcha"], body="a trap")
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "c1")
    out = _hook(tmp_path, monkeypatch, capsys)
    assert list(out) == ["injectSteps"]
    (step,) = out["injectSteps"]
    assert list(step["systemMessage"]) == ["systemMessage"]
    assert "the canary task" in step["systemMessage"]["systemMessage"]
    assert "a trap" in (Path.home() / ".cache/symbion/gotchas/c1").read_text(encoding="utf-8")


def test_the_skill_line_under_agy_names_the_copy_agy_installed(tmp_path, monkeypatch, capsys):
    """agy lists each skill by the path of its copy and reads it from there."""
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "c1")
    store.ensure_store(tmp_path)
    store.add(tmp_path, kind="task", target={"type": "item", "name": "x"}, author="human",
              body="a task")
    assert "load the symbion skill" not in _hook(
        tmp_path, monkeypatch, capsys)["injectSteps"][0]["systemMessage"]["systemMessage"]
    skill = Path.home() / ".gemini/config/plugins/symbion/skills/symbion/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("x")
    said = _hook(tmp_path, monkeypatch, capsys)["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert ("before a write: load the symbion skill "
            "(~/.gemini/config/plugins/symbion/skills/symbion/SKILL.md)") in said


def test_init_for_antigravity_names_the_plugin_and_writes_nothing_there(tmp_path, capsys):
    """The plugin carries agy's skill and hook. A copy without the native
    manifest came from Claude Code's, whose hook agy cannot parse."""
    args = ["--dir", str(tmp_path / "notes"), "init", "--agent", "antigravity", "--yes"]
    note = f"note: on Antigravity the symbion plugin carries the skill and the hook: " \
           f"{cli.AGY_PLUGIN_INSTALL}\n"
    assert cli.main(args) == 0
    assert note in capsys.readouterr().out
    assert not (Path.home() / ".gemini").exists()
    copy = Path.home() / ".gemini/config/plugins/symbion"
    (copy / "skills/symbion").mkdir(parents=True)
    assert cli.main(args) == 0
    assert note in capsys.readouterr().out, "a converted copy is no install"
    (copy / ".antigravity-plugin").mkdir()
    (copy / ".antigravity-plugin/plugin.json").write_text("{}")
    assert cli.main(args) == 0
    assert "on Antigravity" not in capsys.readouterr().out
