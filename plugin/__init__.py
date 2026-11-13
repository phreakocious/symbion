"""The symbion plugin for Hermes Agent. It gives Hermes what the plugin's hooks
give Claude Code by running the same shims (hooks/) and the installed CLI's
files: it carries no copy of either (spec 2026-11-11). Hermes imports it into
its own Python, so it imports only the standard library. It never fails a turn
or a tool call: a failure, a timeout or a missing `sh` sends nothing."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent
START = PLUGIN / "hooks" / "session_start.sh"
GOTCHAS = PLUGIN / "hooks" / "gotchas.sh"
# Claude Code's names for the tools its gotcha matcher covers. Hermes's search
# tools stay out, as Claude Code's Grep and Glob do.
TOOLS = {"terminal": "Bash", "read_file": "Read", "write_file": "Write", "patch": "Edit"}
# A session start may pull the store; a gotcha runs after every file or terminal tool.
START_TIMEOUT = 10
GOTCHA_TIMEOUT = 5
# The compactions that rewrite the history, as Claude Code's do. The opt-in
# micro_summarize and prune_tool_results can run every few turns.
COMPACTION_KINDS = frozenset({"summarize", "provider_native"})
# A V4A patch's files, in Hermes's own dialect (tools/patch_parser.py): any blanks
# after `***`, and a move as `Move File: a -> b`, which touches both.
V4A_FILE = re.compile(r"^\*\*\*\s*(?:(?:Update|Add|Delete)\s+File:\s*(.+?)|Move\s+File:\s*(.+?)\s*->\s*(.+?))\s*$",
                      re.M)



def hook_context(out: str) -> str | None:
    """The start hook's answer: Claude Code's JSON, or plain text (an error, the
    not-installed line), which reaches the model as it is."""
    text = out.strip()
    if not text:
        return None
    try:
        content = json.loads(text)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, TypeError, KeyError):
        return text
    return content if isinstance(content, str) else text


def gotcha_text(out: str) -> str | None:
    """The gotcha lines in gotchas.sh's answer, or None."""
    try:
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, TypeError, KeyError):
        return None
    return text if isinstance(text, str) and text else None


def with_gotchas(result: str, text: str) -> str:
    """`result` with the gotcha lines. A JSON object gets them as its first key:
    Hermes parses the result after this hook to tell a failed call
    (`_detect_tool_failure`), and the preview of a result over its size budget
    keeps a JSON object's scalar keys and cuts its tail (`_pageable_tool_json`).
    Other text gets them after a blank line."""
    # shortcut: past the budget the preview keeps about 1,300 chars of gotchas (four)
    # and cuts the rest after gotchas.sh marked them delivered; split them if a file outgrows that.
    try:
        data = json.loads(result)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict):
        return json.dumps({"symbion": text, **data}, ensure_ascii=False)
    return result + "\n\n" + text


def gotcha_inputs(sid: str, agent_id: str, tool_name: str, args: object, is_error: bool) -> list[str]:
    """Claude Code's PostToolUse JSON for gotchas.sh, one line per run: none for a
    tool it skips, one per file for a V4A patch. gotchas.sh reads the event, the
    tool and agent_id from the text before tool_input."""
    tool = TOOLS.get(tool_name)
    if tool is None or not isinstance(args, dict):
        return []
    if tool == "Edit" and args.get("mode") == "patch":
        patch = args.get("patch")
        inputs = [{"file_path": f} for m in V4A_FILE.finditer(patch) for f in m.groups() if f] \
            if isinstance(patch, str) else []
    elif tool in ("Write", "Edit"):
        # gotchas.sh reads an edit's file from its first `*_path` key; Hermes names it `path`.
        inputs = [{"file_path": args.get("path"), **{k: v for k, v in args.items() if k != "path"}}]
    else:
        inputs = [args]
    head = {"session_id": sid, "hook_event_name": "PostToolUseFailure" if is_error else "PostToolUse",
            "tool_name": tool, **({"agent_id": agent_id} if agent_id else {})}
    # Unescaped, as Claude Code and Pi send it: gotchas.sh matches names in the raw text.
    return [json.dumps({**head, "tool_input": i}, ensure_ascii=False) for i in inputs]


class Sessions:
    """What the gateway knows of its sessions, shared by its threads: which owe
    the summary on their next turn, and each delegate_task child's parent."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._due: set[str] = set()
        # shortcut: never pruned, an id pair per child for the gateway's life;
        # drop a session's entries on on_session_finalize if a gateway grows it.
        self._parent: dict[str, str] = {}

    def note_parent(self, sid: str, parent: str) -> None:
        if sid and parent and sid != parent:
            with self._lock:
                self._parent[sid] = parent

    def root(self, sid: str) -> str:
        """The session at the top of `sid`'s parents: `sid` when it has none."""
        with self._lock:
            seen = {sid}
            while (p := self._parent.get(sid)) and p not in seen:
                seen.add(p)
                sid = p
        return sid

    def mark_due(self, sid: str) -> None:
        if sid:
            with self._lock:
                self._due.add(sid)

    def take_due(self, sid: str) -> bool:
        """Whether `sid` owes the summary; true once per mark."""
        with self._lock:
            if sid in self._due:
                self._due.discard(sid)
                return True
        return False


def run(args: list[str], stdin: str, cwd: str, env: dict[str, str], timeout: float) -> str:
    """`args` with `stdin`; its stdout, or "" on any error, timeout or nonzero
    exit. A timeout kills the process group: a hung `symbion` is the shim's
    child, and it holds the pipe open after the shim dies.
    shortcut: Windows has no process groups, so there a timeout kills the shim
    alone and a hung symbion lives on; a job object would end both."""
    try:
        p = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                             start_new_session=os.name != "nt")
    except OSError:
        return ""
    try:
        out, _ = p.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                p.kill()
            else:
                os.killpg(p.pid, signal.SIGKILL)
            p.communicate(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ""
    return out if p.returncode == 0 else ""


def hook_env(cwd: str, root: str) -> dict[str, str]:
    """The shims' environment: the gateway's, less the Claude Code plugin's
    upgrade markers and Hermes's own Python path, plus what Hermes gives its
    terminal commands. The gateway has neither Hermes variable, and the summary
    would name the wrong reader. Its PYTHONPATH names Hermes's checkout and
    site-packages, which symbion, another install, imported first."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "PYTHONHOME")}
    env.update(SYMBION_PROJECT_DIR=cwd, HERMES_AGENT="true", SYMBION_HERMES_SESSION=root)
    return env


def skill_description(path: Path) -> str:
    """A SKILL.md's frontmatter description: Hermes lists a plugin's skills in
    every session's skill index under the one given at registration, and
    blank without it. Ours are double-quoted scalars that read as JSON."""
    try:
        line = next(x for x in path.read_text(encoding="utf-8").splitlines() if x.startswith("description:"))
        value = line.split(":", 1)[1].strip()
        return json.loads(value) if value.startswith('"') else value
    except (OSError, StopIteration, ValueError):
        return ""


def cli_skill() -> Path | None:
    """The installed CLI's SKILL.md, or None with no symbion that runs."""
    out = run(["symbion", "skill", "--path"], "", str(PLUGIN), hook_env(str(PLUGIN), ""), GOTCHA_TIMEOUT)
    skill = Path(out.strip()) / "SKILL.md" if out.strip() else None
    return skill if skill and skill.is_file() else None


class Plugin:
    """Hermes's hooks for one profile's gateway. Each one catches its own
    errors: Hermes logs a callback that raises, and the turn goes on."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.sessions = Sessions()

    def on_pre_llm_call(self, session_id: str = "", is_first_turn: bool = False, platform: str = "",
                        parent_session_id: str = "", **_) -> dict | None:
        try:
            self.sessions.note_parent(session_id, parent_session_id)
            # Taken before the run, so a failed or slow symbion costs one turn.
            due = self.sessions.take_due(session_id)
            # A delegate_task child gets no summary, as a Claude Code subagent gets none.
            if platform == "subagent" or self.sessions.root(session_id) != session_id:
                return None
            cwd = self.ctx.session_cwd()
            if not (is_first_turn or due) or not cwd:
                return None
            text = hook_context(run(["sh", str(START), "hermes"], json.dumps({"session_id": session_id}),
                                    cwd, hook_env(cwd, session_id), START_TIMEOUT))
            return {"context": text} if text else None
        except Exception:
            return None

    def on_session_open(self, session_id: str = "", reason: str = "", **_) -> None:
        if reason == "resume":
            self.sessions.mark_due(session_id)

    def on_post_compaction(self, session_id: str = "", kind: str = "", **_) -> None:
        if kind in COMPACTION_KINDS:
            self.sessions.mark_due(session_id)

    def on_transform_tool_result(self, tool_name: str = "", args: object = None, result: object = None,
                                 session_id: str = "", status: str = "", **_) -> str | None:
        try:
            cwd = self.ctx.session_cwd()
            if not isinstance(result, str) or not cwd:
                return None
            # A child reads its root session's list, with marks of its own.
            root = self.sessions.root(session_id)
            texts = []
            for line in gotcha_inputs(root, session_id if root != session_id else "", tool_name, args,
                                      status == "error"):
                text = gotcha_text(run(["sh", str(GOTCHAS)], line, cwd, hook_env(cwd, root), GOTCHA_TIMEOUT))
                if text:
                    texts.append(text)
            return with_gotchas(result, "\n".join(texts)) if texts else None
        except Exception:
            return None

    def session_env(self, sid: str) -> dict[str, str] | None:
        return {"SYMBION_HERMES_SESSION": self.sessions.root(sid)} if sid else None



def register(ctx) -> None:
    plugin = Plugin(ctx)
    ctx.register_hook("pre_llm_call", plugin.on_pre_llm_call)
    ctx.register_hook("on_session_open", plugin.on_session_open)
    ctx.register_hook("post_compaction", plugin.on_post_compaction)
    ctx.register_hook("transform_tool_result", plugin.on_transform_tool_result)
    ctx.register_session_env(["SYMBION_HERMES_SESSION"], plugin.session_env)
    # Hermes offers a plugin's skills as /symbion:setup and /symbion:wrap and loads
    # them with no config; a plugin command could queue its prompt only with
    # allow_gateway_injection set (a live check, 2026-11-11). wrap loads
    # symbion:symbion first: the installed CLI's skill, as in Claude Code.
    skills = {"setup": PLUGIN / "skills/setup/SKILL.md", "wrap": PLUGIN / "skills/wrap/SKILL.md"}
    if (skill := cli_skill()) is not None:
        skills["symbion"] = skill
    for name, path in skills.items():
        try:
            ctx.register_skill(name, path, description=skill_description(path))
        except (OSError, ValueError):
            pass
