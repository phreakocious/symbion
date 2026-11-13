"""Antigravity (agy): the hook's JSON, the skill line and `init --agent
antigravity`. agy installs the plugin (tests/test_plugin.py) and reads only its
own hook format (agy 1.4.12, 2026-10-30)."""
import io
import json
import shlex
import subprocess
import sys
from importlib import resources
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


def test_the_plugin_shim_names_the_host_over_the_shell_marker(tmp_path, monkeypatch, capsys):
    """A `claude` started in agy's terminal can inherit agy's marker, which
    `shell_author` ranks first; the shim's SYMBION_HOST says who runs the hook."""
    store.ensure_store(tmp_path)
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "c1")
    monkeypatch.setenv("SYMBION_HOST", "claude")
    assert list(_hook(tmp_path, monkeypatch, capsys, b'{"session_id": "s1"}')) == [
        "hookSpecificOutput", "systemMessage"]
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID")
    monkeypatch.setenv("SYMBION_HOST", "antigravity")
    assert list(_hook(tmp_path, monkeypatch, capsys)) == ["injectSteps"]


def test_the_skill_line_under_agy_names_the_package_skill(tmp_path, monkeypatch, capsys):
    """With the plugin installed, the line names the package's SKILL.md. The
    plugin's copy is the `!` stub, which agy reads unrun, and its run_command
    cut `symbion skill` in the middle past ~10 KB (an agy session, 2026-11-11)."""
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
            f"({(cli._skill_dir() / 'SKILL.md').as_posix()})") in said


def test_agy_rules_leave_out_a_dir_inside_another_and_usr(tmp_path, monkeypatch):
    """Each rule is one the user pastes by hand: a dir inside another rule's,
    or under /usr, which the sandbox reads already, would be one more."""
    pkg = Path(str(resources.files("symbion"))).resolve().parent
    monkeypatch.setattr(sys, "prefix", str(pkg.parent))
    # Windows has no /usr, and a POSIX path gains a drive there (C:\usr): nest it instead.
    monkeypatch.setattr(sys, "base_prefix",
                        str(pkg / "lib") if sys.platform == "win32" else "/usr/lib/python3")
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    assert cli._agy_rules(tmp_path / "s") == [
        f"read_file({pkg.parent.as_posix()})",
        f"write_file({(tmp_path / 's').resolve().as_posix()})"]


def test_init_for_antigravity_names_the_plugin_and_writes_nothing_there(tmp_path, capsys):
    """The plugin carries agy's skill and hook. A copy without the native
    manifest came from Claude Code's, whose hook agy cannot parse."""
    args = ["--dir", str(tmp_path / "notes"), "init", "--agent", "antigravity", "--yes"]
    note = f"note: on Antigravity the symbion plugin carries the skill and the hook: " \
           f"{cli.AGY_PLUGIN_INSTALL}\n"
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert note in out
    # In auto mode agy's sandbox reads only the workspace and system dirs, and
    # these rules let symbion start, add and commit there (agy 1.4.12, 2026-11-03).
    assert "/permissions" in out and "--add-dir" not in out
    rules = [line.strip() for line in out.splitlines() if line.startswith("  ")
             and line.strip().startswith(("read_file(", "write_file("))]
    assert f"write_file({(tmp_path / 'notes').resolve().as_posix()})" in rules
    pkg = Path(str(resources.files("symbion"))).resolve().parent
    for need in (Path(sys.prefix).resolve(), pkg):
        assert any(need.as_posix().startswith(r[len("read_file("):-1]) for r in rules), need
    # A rule with `~` made agy refuse every sandboxed command.
    assert all(Path(r.split("(", 1)[1][:-1]).is_absolute() for r in rules), rules
    assert "codex" not in out
    assert not (Path.home() / ".gemini").exists()
    copy = Path.home() / ".gemini/config/plugins/symbion"
    (copy / "skills/symbion").mkdir(parents=True)
    assert cli.main(args) == 0
    assert note in capsys.readouterr().out, "a converted copy is no install"
    (copy / ".antigravity-plugin").mkdir()
    (copy / ".antigravity-plugin/plugin.json").write_text("{}")
    assert cli.main(args) == 0
    assert "on Antigravity" not in capsys.readouterr().out
    # Last, and no `nothing to change` above it: a stale copy is the user's to fix.
    assert cli.main(args[:-1]) == 1
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith(f"the Antigravity plugin at {copy} "), "an empty manifest is stale"


def test_a_stale_agy_plugin_is_named_by_version_in_an_install(tmp_path, monkeypatch):
    """agy installs a copy, which no upgrade of symbion touches (stale on
    2026-11-09 and 11-11). An install compares versions."""
    copy = tmp_path / "copy"
    (copy / ".antigravity-plugin").mkdir(parents=True)
    here = tmp_path / "lib/site-packages/symbion"   # in no repo
    monkeypatch.setattr(cli.metadata, "version", lambda _: "0.15.2")
    for theirs, said in (("0.15.1", f"is 0.15.1, and symbion is 0.15.2: run `{cli.AGY_PLUGIN_INSTALL}`"),
                         ("0.16.0", "is 0.16.0, and symbion is 0.15.2: upgrade symbion"),
                         ("0.15.2", None)):
        (copy / ".antigravity-plugin/plugin.json").write_text(json.dumps({"version": theirs}))
        assert cli._agy_plugin_stale(copy, here) == (
            said and f"the Antigravity plugin at {copy} {said}"), theirs
    monkeypatch.setattr(cli.metadata, "version", lambda _: "0.15.2.dev0")
    assert cli._agy_plugin_stale(copy, here) is None, "a dev build of the same release"


def test_a_stale_agy_plugin_is_named_by_file_in_a_checkout(tmp_path):
    """A checkout's version spans many commits: the copy matched 0.15.2 and
    still lacked a day of hook changes (2026-11-11). It compares each file git
    tracks in plugin/, as the work tree has it."""
    repo, copy = tmp_path / "repo", tmp_path / "copy"
    for root in (repo / "plugin", copy):
        (root / "hooks").mkdir(parents=True)
        (root / "hooks/start.sh").write_text("new")
        (root / "README.md").write_text("r")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "plugin"], check=True)
    (repo / "plugin/__pycache__").mkdir()
    (repo / "plugin/__pycache__/x.pyc").write_text("untracked")
    here = repo / "src/symbion"
    assert cli._agy_plugin_stale(copy, here) is None
    (copy / "hooks/start.sh").write_text("old")
    (copy / "README.md").unlink()
    src = shlex.quote(str(repo / "plugin"))
    assert cli._agy_plugin_stale(copy, here) == (
        f"the Antigravity plugin at {copy} differs from {repo / 'plugin'} in "
        f"README.md, hooks/start.sh: run `agy plugin install {src}`")
