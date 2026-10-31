"""Codex PreToolUse: read the session-start cache without importing symbion.

The hook input contract is documented at https://learn.chatgpt.com/docs/hooks.
Bash and apply_patch both carry tool_input.command. Match other local tools'
arguments too; responses never participate. Cache format is shared with gotchas.sh.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def main():
    event = json.load(sys.stdin)
    sid = event.get("session_id", "")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
        return
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    listing = cache / "symbion" / "gotchas" / sid
    if not listing.is_file():
        return
    tool = event.get("tool_name", "")
    args = event.get("tool_input", {})
    edit = tool == "apply_patch"
    if edit:
        patch = args.get("command", "") if isinstance(args, dict) else args
        if not isinstance(patch, str):
            return
        call = "\n".join(re.findall(
            r"^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+)$", patch, re.M))
    elif tool in {"Edit", "Write", "NotebookEdit"}:
        edit = True
        call = args.get("file_path", args.get("notebook_path", ""))
    else:
        call = json.dumps(args, ensure_ascii=False)
    call = call.replace("\\\\", "/").replace("\\", "/")
    # Codex subagents share the parent's session_id. Their transcript path
    # distinguishes readers; rebuilding the list after compaction resets them.
    reader = event.get("agent_id") or event.get("transcript_path")
    mark = str(listing)
    if reader:
        key = hashlib.sha256(str(reader).encode()).hexdigest()[:16]
        mark += f".{listing.stat().st_mtime_ns}.{key}"
    gate = Path(mark + (".edit" if edit else ".seen"))
    seen = set(gate.read_text(encoding="utf-8").splitlines()) if gate.exists() else set()
    output = []
    for row in listing.read_text(encoding="utf-8").splitlines():
        ident, name, text, root = row.split("\t")
        if ident in seen:
            continue
        # Same root/path boundary rule as gotchas.sh: README.md must not
        # match docs/README.md or README.md.bak. Absolute paths keep the root.
        prefix = r"(?<![A-Za-z0-9._~/@+\-])(?:\./)?"
        if root:
            prefix += r"(?:[^\s\"']*/" + re.escape(root) + r"/)?"
        else:
            prefix += r"(?:[^\s\"']*/)?"
        if not re.search(prefix + re.escape(name) + r"(?![A-Za-z0-9._~/-])", call):
            continue
        output.append(json.loads('"' + text + '"'))
        seen.add(ident)
        for suffix in ((".seen", ".edit") if edit else (".seen",)):
            with Path(mark + suffix).open("a", encoding="utf-8") as stream:
                stream.write(ident + "\n")
    if output:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "additionalContext": "\n".join(output)}}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # An unavailable cache or malformed event never fails a tool call.
