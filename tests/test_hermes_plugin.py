"""The Hermes plugin (spec 2026-11-11): plugin/__init__.py, which Hermes imports
into its own Python, so it runs here with no Hermes installed. It turns Hermes's
hooks into Claude Code's hook JSON for the shared shims, as Pi's extension does."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from symbion import store

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("symbion_hermes_plugin", REPO / "plugin/__init__.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


hp = _load()


def _one(lines):
    (line,) = lines
    return json.loads(line)


def test_tools_map_to_claude_codes_names_and_others_are_skipped():
    """gotchas.sh knows Claude Code's tool names. Search tools stay out, as
    Claude Code's Grep and Glob do."""
    assert _one(hp.gotcha_inputs("s", "", "terminal", {"command": "cat a.py"}, False)) == {
        "session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "cat a.py"}}
    assert _one(hp.gotcha_inputs("s", "", "read_file", {"path": "a.py", "offset": 1}, False)) == {
        "session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"path": "a.py", "offset": 1}}
    assert hp.gotcha_inputs("s", "", "search_files", {"pattern": "a.py"}, False) == []
    assert hp.gotcha_inputs("s", "", "terminal", "cat a.py", False) == []
    # gotchas.sh matches names in the raw text, which Claude Code and Pi send unescaped.
    assert '"naïve.py"' in hp.gotcha_inputs("s", "", "read_file", {"path": "naïve.py"}, False)[0]


def test_an_edit_names_its_file_as_file_path():
    """gotchas.sh reads an edit's file from the first key ending in `_path"`:
    with Hermes's `path` it read the literal word and never fired."""
    for tool, name, args in (
            ("write_file", "Write", {"path": "src/x.py", "content": "y"}),
            ("patch", "Edit", {"path": "src/x.py", "old_string": "a", "new_string": "b"}),
            ("patch", "Edit", {"mode": "replace", "path": "src/x.py", "old_string": "a"})):
        (line,) = hp.gotcha_inputs("s", "", tool, args, False)
        j = json.loads(line)
        assert j["tool_name"] == name
        assert j["tool_input"]["file_path"] == "src/x.py" and "path" not in j["tool_input"]
        assert '"tool_input": {"file_path": "src/x.py"' in line


def test_a_v4a_patch_runs_once_per_file_as_an_edit():
    """For OpenAI models Hermes's patch takes a V4A patch and no path. It names
    each file on a `***` line, in tools/patch_parser.py's dialect: any blanks
    after `***`, and a move as `Move File: a -> b`, both of which it touches."""
    patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n***Add File:b.py\n+z\n"
             "*** Move File: old.py -> new.py\n*** Delete  File: d.py\n*** End Patch")
    lines = hp.gotcha_inputs("s", "", "patch", {"mode": "patch", "patch": patch}, False)
    assert [(json.loads(x)["tool_name"], json.loads(x)["tool_input"]) for x in lines] == [
        ("Edit", {"file_path": f}) for f in ("a.py", "b.py", "old.py", "new.py", "d.py")]
    assert hp.gotcha_inputs("s", "", "patch", {"mode": "patch", "patch": "no headers"}, False) == []


def test_an_error_is_a_failure_and_a_child_carries_its_agent_id():
    """gotchas.sh reads the event and agent_id from the text before
    tool_input, where a file's text cannot put one."""
    line = hp.gotcha_inputs("root", "kid", "read_file", {"path": "a.py"}, True)[0]
    j = json.loads(line)
    assert (j["session_id"], j["hook_event_name"], j["agent_id"]) == ("root", "PostToolUseFailure", "kid")
    assert line.index('"hook_event_name"') < line.index('"agent_id"') < line.index('"tool_input"')
    assert "agent_id" not in _one(hp.gotcha_inputs("root", "", "read_file", {"path": "a.py"}, False))


def test_hook_context_reads_the_json_and_passes_plain_text():
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": "the summary"},
                      "systemMessage": "symbion: 1 open"})
    assert hp.hook_context(out + "\n") == "the summary"
    plain = "symbion: this project has a symbion store, but … run /symbion-setup."
    assert hp.hook_context(plain + "\n") == plain
    assert hp.hook_context("") is None and hp.hook_context(" \n") is None
    assert hp.gotcha_text(json.dumps({"hookSpecificOutput": {"additionalContext": "a trap"}})) == "a trap"
    assert hp.gotcha_text("") is None and hp.gotcha_text("not json") is None


def test_root_follows_parents_and_stops_at_a_loop_or_a_stranger():
    s = hp.Sessions()
    s.note_parent("c", "p")
    s.note_parent("g", "c")
    assert (s.root("g"), s.root("c"), s.root("p"), s.root("x")) == ("p", "p", "p", "x")
    s.note_parent("a", "b")
    s.note_parent("b", "a")
    assert s.root("a") in ("a", "b")


def test_a_due_mark_is_taken_once_across_threads():
    """The gateway runs every session's hooks in one process, on its threads."""
    s = hp.Sessions()
    assert s.take_due("s") is False
    s.mark_due("s")
    got = []
    threads = [threading.Thread(target=lambda: got.append(s.take_due("s"))) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == [False] * 15 + [True]


# --- register(ctx) and the hooks, end to end ---------------------------------

SID = "20261111_120000_aaaa"
KID = "20261111_120100_bbbb"
TRAP = "TRAP-CANARY: read this before you touch trap.txt"


class FakeCtx:
    """What register(ctx) and the hooks use of Hermes's PluginContext."""

    def __init__(self, cwd=""):
        self.cwd = cwd
        self.hooks, self.skills, self.descriptions, self.env = {}, {}, {}, []

    def register_hook(self, name, fn, **_):
        self.hooks[name] = fn

    def register_skill(self, name, path, description="", **_):
        self.skills[name] = Path(path)
        self.descriptions[name] = description

    def register_session_env(self, names, resolver):
        self.env.append((list(names), resolver))

    def session_cwd(self):
        return self.cwd


def _plugin(cwd=""):
    ctx = FakeCtx(cwd)
    hp.register(ctx)
    return ctx


def test_the_manifest_declares_what_register_registers():
    """`hermes plugins validate` fails a catalog entry whose declared hooks
    differ from the registered ones."""
    ctx = _plugin()
    text = (REPO / "plugin/plugin.yaml").read_text(encoding="utf-8")
    block = re.search(r"^provides_hooks:\n((?:  - \w+\n)+)", text, re.M)[1]
    assert re.findall(r"- (\w+)", block) == sorted(ctx.hooks) == sorted(
        ["pre_llm_call", "transform_tool_result", "on_session_open", "post_compaction"])
    assert {"setup", "wrap"} <= set(ctx.skills)
    assert [names for names, _ in ctx.env] == [["SYMBION_HERMES_SESSION"]]
    assert re.search(r'^requires_hermes: ">=\d+\.\d+\.\d+"$', text, re.M)


@pytest.fixture
def adopted(tmp_path, monkeypatch):
    """A project beside its store, with a task, a gotcha on trap.txt, the
    venv's symbion on PATH and a Hermes home that links the skill."""
    if os.name == "nt":
        # shortcut: Hermes on Windows is untested (docs/guide.md). Here CreateProcess finds
        # no sh on the PATH below and cannot run the shebang symbion (2026-11-13); a fake
        # symbion.exe and Git's usr/bin on PATH would run these there.
        pytest.skip("Hermes on Windows is untested: the fixture's sh and symbion are POSIX")
    proj = tmp_path / "proj"
    proj.mkdir()
    for a in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "c0"]):
        subprocess.run(["git", "-C", str(proj), *a], check=True, capture_output=True)
    notes = tmp_path / "proj-notes"
    store.ensure_store(notes)
    store.add(notes, kind="task", target={"type": "item", "name": "x"}, author="human",
              body="the canary")
    store.add(notes, kind="note", target={"type": "file", "name": "trap.txt"}, author="human",
              tags=["gotcha"], body=TRAP)
    # The hooks drop PYTHONPATH, which conftest.py uses to put this tree's src/
    # first: a symbion on PATH that sets it runs this tree, not the venv's install.
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "symbion").write_text(
        f"#!/bin/sh\nPYTHONPATH='{REPO / 'src'}' exec '{sys.executable}' -c "
        f"'import sys; from symbion.cli import main; sys.exit(main())' \"$@\"\n")
    (bin_ / "symbion").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join(
        [str(bin_), str(Path(shutil.which("git")).parent), "/usr/bin", "/bin"]))
    skill = tmp_path / "hermes-home/skills/symbion/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("x")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    return proj


def _turn(ctx, sid=SID, first=False, **kw):
    r = ctx.hooks["pre_llm_call"](session_id=sid, is_first_turn=first, model="m", platform="cli", **kw)
    return r["context"] if r else None


def test_the_summary_goes_on_a_first_turn_a_resume_and_a_summarizing_compaction(adopted):
    ctx = _plugin(str(adopted))
    said = _turn(ctx, first=True)
    assert "the canary" in said and "skill_view symbion" in said
    cached = (Path.home() / ".cache/symbion/skill_dir").read_text(encoding="utf-8").strip()
    assert cached == (REPO / "src/symbion/data/skill").as_posix()   # this tree's symbion ran
    assert _turn(ctx) is None
    ctx.hooks["on_session_open"](session_id=SID, platform="cli", reason="create")
    assert _turn(ctx) is None
    ctx.hooks["on_session_open"](session_id=SID, platform="cli", reason="resume")
    assert "the canary" in _turn(ctx)
    ctx.hooks["post_compaction"](session_id=SID, old_session_id="", kind="micro_summarize")
    assert _turn(ctx) is None
    ctx.hooks["post_compaction"](session_id=SID, old_session_id="", kind="summarize")
    assert "the canary" in _turn(ctx)
    assert _turn(ctx, KID, first=True, parent_session_id=SID) is None


def test_a_subagent_gets_no_summary(adopted):
    """A Claude Code subagent gets no SessionStart summary either."""
    ctx = _plugin(str(adopted))
    r = ctx.hooks["pre_llm_call"](session_id=KID, is_first_turn=True, platform="subagent",
                                  parent_session_id="")
    assert r is None


def _tool(ctx, sid, path, result="the file's text", status="ok", tool="read_file"):
    return ctx.hooks["transform_tool_result"](tool_name=tool, args={"path": path}, result=result,
                                              session_id=sid, task_id="t", status=status)


def test_a_gotcha_reaches_a_read_once_and_a_childs_marks_are_its_own(adopted):
    ctx = _plugin(str(adopted))
    _turn(ctx, first=True)
    got = _tool(ctx, SID, "trap.txt")
    assert got.startswith("the file's text\n\n") and TRAP in got
    assert _tool(ctx, SID, "trap.txt") is None
    assert _tool(ctx, SID, "other.txt") is None
    assert _turn(ctx, KID, first=True, parent_session_id=SID) is None
    assert TRAP in _tool(ctx, KID, "trap.txt")
    assert _tool(ctx, KID, "trap.txt") is None
    assert dict(ctx.env[0][1](KID)) == {"SYMBION_HERMES_SESSION": SID}


@pytest.mark.skipif(os.name == "nt", reason="no process groups: a timeout kills the shim alone")
def test_a_hung_symbion_costs_one_turn(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    hung = tmp_path / "hung.sh"
    # symbion under the shim is a grandchild: a timeout that kills only the shim
    # leaves it running, one more for each turn that hits it.
    hung.write_text(f"echo x >> '{log}'\nsh -c \"sleep 1; echo late >> '{log}'\"\n")
    monkeypatch.setattr(hp, "START", hung)
    monkeypatch.setattr(hp, "START_TIMEOUT", 0.5)
    ctx = _plugin(str(tmp_path))
    t = time.monotonic()
    assert _turn(ctx, first=True) is None
    assert time.monotonic() - t < 5
    assert _turn(ctx) is None
    assert log.read_text().count("x") == 1
    ctx.hooks["on_session_open"](session_id=SID, platform="cli", reason="resume")
    assert _turn(ctx) is None and _turn(ctx) is None
    assert log.read_text().count("x") == 2
    time.sleep(1.5)
    assert "late" not in log.read_text()


def test_no_cwd_runs_nothing_and_a_non_string_result_is_left_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(hp, "cli_skill", lambda: None)
    monkeypatch.setattr(hp, "run", lambda *a, **k: calls.append(a) or "")
    ctx = _plugin("")
    assert _turn(ctx, first=True) is None
    assert ctx.hooks["transform_tool_result"](tool_name="read_file", args={"path": "a"},
                                              result={"x": 1}, session_id=SID) is None
    assert calls == []


def test_a_json_result_carries_the_gotcha_as_its_first_key(monkeypatch):
    """Text after a JSON result made Hermes read a failed call as a success, and
    fell past the preview of a result over Hermes's size budget, which keeps a
    JSON object's scalar keys (review minors 2 and 3, 2026-11-12)."""
    monkeypatch.setattr(hp, "cli_skill", lambda: None)
    monkeypatch.setattr(hp, "run", lambda *a, **k: json.dumps({"hookSpecificOutput": {"additionalContext": TRAP}}))
    ctx = _plugin("/p")
    failed = json.dumps({"output": "boom", "exit_code": 1, "error": None})
    got = _tool(ctx, SID, "trap.txt", result=failed, status="error", tool="terminal")
    assert list(json.loads(got).items()) == [("symbion", TRAP), ("output", "boom"), ("exit_code", 1),
                                             ("error", None)]
    assert _tool(ctx, SID, "trap.txt", result="[1]") == "[1]\n\n" + TRAP


def test_an_error_result_is_sent_as_a_failure(monkeypatch):
    seen = []
    monkeypatch.setattr(hp, "cli_skill", lambda: None)
    monkeypatch.setattr(hp, "run", lambda args, stdin, *a, **k: seen.append(json.loads(stdin)) or "")
    ctx = _plugin("/p")
    _tool(ctx, SID, "a.py", result='{"error": "no such file"}', status="error")
    assert [s["hook_event_name"] for s in seen] == ["PostToolUseFailure"]


def test_the_hooks_env_drops_claude_plugin_vars_and_names_hermes(monkeypatch):
    """A Hermes started from a Claude Code shell must never start the plugin's
    upgrade worker, which keys on these."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/d")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/r")
    # Hermes runs its gateway with its own PYTHONPATH, its checkout and its
    # site-packages: symbion, another install, imported Hermes's packages first.
    monkeypatch.setenv("PYTHONPATH", "/hermes-agent:/hermes/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/hermes")
    env = hp.hook_env("/p", "root")
    assert "CLAUDE_PLUGIN_DATA" not in env and "CLAUDE_PLUGIN_ROOT" not in env
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert (env["SYMBION_PROJECT_DIR"], env["HERMES_AGENT"], env["SYMBION_HERMES_SESSION"]) == \
        ("/p", "true", "root")


def test_setup_and_wrap_are_plugin_skills_and_the_cli_s_skill_is_symbion(adopted, monkeypatch):
    """Hermes offers a plugin's skills as /symbion:setup and /symbion:wrap, the
    names Claude Code and agy use, and loads one with no config: a plugin
    command could queue its prompt only with allow_gateway_injection set (a
    live check, 2026-11-11). wrap tells the model to load symbion:symbion
    first, so the installed CLI's own skill is registered under that name."""
    ctx = _plugin(str(adopted))
    assert ctx.skills == {"setup": REPO / "plugin/skills/setup/SKILL.md",
                          "wrap": REPO / "plugin/skills/wrap/SKILL.md",
                          "symbion": REPO / "src/symbion/data/skill/SKILL.md"}
    monkeypatch.setenv("PATH", "/usr/bin:/bin")       # no symbion: setup is the way in
    assert set(_plugin(str(adopted)).skills) == {"setup", "wrap"}
    # Hermes lists a plugin's skills in every session's skill index under the
    # description given at registration, and blank without one.
    for name, path in ctx.skills.items():
        line = next(x for x in path.read_text(encoding="utf-8").splitlines() if x.startswith("description:"))
        assert ctx.descriptions[name] == json.loads(line.split(":", 1)[1].strip()) != "", name
