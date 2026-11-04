"""Pi: the hook's JSON and the skill line under `SYMBION_HOST=pi`, `init
--agent pi`, and the package's extension. The start shim's `pi` branch is in
tests/test_plugin.py. The extension runs the plugin's shims, so Pi parses the
JSON Claude Code gets (spec 2026-11-02)."""
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from symbion import cli, store

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _hook(store_dir, monkeypatch, capsys, stdin: bytes = b'{"session_id": "p1"}') -> dict:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    assert cli.main(["--dir", str(store_dir), "summary", "--hook"]) == 0
    return json.loads(capsys.readouterr().out)


def test_session_start_under_pi_speaks_claude_codes_json(tmp_path, monkeypatch, capsys):
    """The extension reads `additionalContext` for the model and `systemMessage`
    for its notify line."""
    store.ensure_store(tmp_path)
    store.add(tmp_path, kind="task", target={"type": "item", "name": "x"}, author="human",
              body="the canary task")
    store.add(tmp_path, kind="note", target={"type": "doc", "name": "plan.md"}, author="human",
              tags=["gotcha"], body="a trap")
    monkeypatch.setenv("SYMBION_HOST", "pi")
    monkeypatch.setenv("AI_AGENT", "pi")
    out = _hook(tmp_path, monkeypatch, capsys)
    assert list(out) == ["hookSpecificOutput", "systemMessage"]
    assert "the canary task" in out["hookSpecificOutput"]["additionalContext"]
    assert "a trap" in (Path.home() / ".cache/symbion/gotchas/p1").read_text(encoding="utf-8")


def test_the_skill_line_under_pi_names_a_file_the_model_reads(tmp_path, monkeypatch, capsys):
    """Pi's system prompt tells the model to load a skill by reading its SKILL.md;
    `/skill:symbion` is a command only the user can type, and a model ran
    `symbion:symbion` in the shell (live check, 2026-11-03). The skills Pi lists
    all lead to the installed skill directory, so any of them serves."""
    store.ensure_store(tmp_path)
    store.add(tmp_path, kind="task", target={"type": "item", "name": "x"}, author="human",
              body="a task")
    monkeypatch.setenv("SYMBION_HOST", "pi")
    said = _hook(tmp_path, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"]
    package = f"before a write: load the symbion skill ({(cli._skill_dir() / 'SKILL.md').as_posix()})"
    assert package in said
    # A Pi started from a Hermes or agy shell inherits their variables, and kept
    # their hint (the Pi branch's review, 2026-11-03).
    for skill, var in ((cli._hermes_home() / "skills/symbion/SKILL.md", "HERMES_AGENT"),
                       (Path.home() / cli._AGY_SKILL, "ANTIGRAVITY_CONVERSATION_ID")):
        skill.parent.mkdir(parents=True)
        skill.write_text("x")
        monkeypatch.setenv(var, "1")
        assert package in _hook(tmp_path, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"], var
        monkeypatch.delenv(var)
    link = Path.home() / ".agents/skills/symbion"
    link.mkdir(parents=True)
    (link / "SKILL.md").write_text("x")
    said = _hook(tmp_path, monkeypatch, capsys)["hookSpecificOutput"]["additionalContext"]
    assert "before a write: load the symbion skill (~/.agents/skills/symbion/SKILL.md)" in said


def test_init_for_pi_names_the_package_and_writes_nothing_there(tmp_path, monkeypatch, capsys):
    """The package carries Pi's skill and hooks. Any package source that names
    symbion counts, a local path during development included."""
    args = ["--dir", str(tmp_path / "notes"), "init", "--agent", "pi", "--yes"]
    note = (f"note: on Pi the symbion package carries the skill and the hooks: "
            f"{cli.PI_PACKAGE_INSTALL}\n")

    def says_note(*extra) -> bool:
        assert cli.main([*args, *extra]) == 0
        out = capsys.readouterr().out
        assert "codex" not in out
        return note in out

    assert says_note()
    assert not (Path.home() / ".pi").exists()
    settings = Path.home() / ".pi/agent/settings.json"
    settings.parent.mkdir(parents=True)
    for packages, installed in ((["git:github.com/phreakocious/symbion"], True),
                                ([{"source": "/src/symbion"}], True),
                                (["npm:other"], False)):
        settings.write_text(json.dumps({"packages": packages}))
        assert says_note() is not installed, packages
    settings.write_text("{not json")
    assert says_note()
    elsewhere = tmp_path / "pi-agent"
    elsewhere.mkdir()
    (elsewhere / "settings.json").write_text(json.dumps({"packages": ["/x/symbion"]}))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(elsewhere))
    assert not says_note()
    # Pi expands `~` there, and reads the packages in its cwd's .pi/settings.json
    # (Pi 1.5.0): each missed, and printed the note beside an installed package.
    shutil.move(elsewhere, Path.home() / "pi-agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "~/pi-agent")
    assert not says_note()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "no-pi-agent"))
    assert says_note()
    (tmp_path / "proj/.pi").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(tmp_path / "proj")], check=True)
    (tmp_path / "proj/.pi/settings.json").write_text(json.dumps({"packages": [{"source": "/src/symbion"}]}))
    monkeypatch.chdir(tmp_path / "proj")
    assert not says_note()
    assert cli.main([*args, "--merge-hook"]) == 0
    assert "note: --merge-hook merges nothing for Pi" in capsys.readouterr().out


def _node_strips_types() -> bool:
    """Node runs a .ts file without flags from 23.6 on."""
    v = re.match(r"v(\d+)\.(\d+)", subprocess.run([NODE, "--version"], capture_output=True,
                                                  text=True).stdout)
    return bool(v) and (int(v[1]), int(v[2])) >= (23, 6)


# shortcut: Node's type stripping runs the .ts tests; an older Node, or none, skips them.
@pytest.mark.skipif(not NODE or not _node_strips_types(), reason="needs Node 23.6+")
def test_the_extension_passes_its_node_tests():
    r = subprocess.run([NODE, "--test", str(REPO / "plugin/pi/symbion.test.ts")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_pi_package_names_its_one_extension():
    pkg = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
    assert pkg["private"] is True
    assert pkg["pi"] == {"extensions": ["./plugin/pi/symbion.ts"]}
    assert (REPO / "plugin/pi/symbion.ts").is_file()
