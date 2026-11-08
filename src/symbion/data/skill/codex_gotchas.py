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


def nosymbion(cmd):
    """The command less its symbion commands and their heredoc bodies, as
    gotchas.sh cuts them: writing about a file is no use of it. A variable
    set to symbion, or a path to it, is symbion where it is called."""
    for name in re.findall(r"""(?:^|[\s;&|(])([A-Za-z_]\w*)=["']?(?:[^\s"';&|]*/)?symbion(?=[\s"';&|]|$)""",
                           cmd):
        cmd = re.sub(r"\$(?:%s|\{%s\}) " % (name, name), "symbion ", cmd)
    out, rest = [], cmd
    while (m := re.search(r"symbion ", rest)):
        pre, rest = rest[:m.start()], rest[m.end() - 1:]
        word = re.split(r'[;&|("\n]', pre)[-1].lstrip(" ")
        if " " in word or (word and not word.endswith("/")):
            out.append(pre + "symbion")
            continue
        seg = re.split(r"[;&|\n]", rest, maxsplit=1)[0]
        rest = rest[len(seg):]
        if "<<" in seg:                             # its body runs to the delimiter's line
            d = seg.split("<<", 1)[1].lstrip("-").lstrip(" ")
            d = d[1:].split(d[0], 1)[0] if d[:1] in ("'", '"') else re.match(r"\w*", d).group()
            i = rest.find("\n" + d)
            rest = rest[i + 1 + len(d):] if i >= 0 else ""
        out.append(pre)
    return "".join(out) + rest


def strings(x):
    """Every string in a tool's arguments, nested or not."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, (list, dict)):
        for v in x.values() if isinstance(x, dict) else x:
            yield from strings(v)


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
        if isinstance(args, dict) and "command" in args:
            c = args["command"]
            c = nosymbion(c) if isinstance(c, str) else [
                nosymbion(x) if isinstance(x, str) else x for x in c] if isinstance(c, list) else c
            args = {**args, "command": c}
        # The strings themselves: in JSON text a newline read `/n` and a quote
        # `/"` once backslashes became slashes, and a name beside one never matched.
        call = "\n".join(strings(args))
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
        ident, name, text, root = row.split("\t")[:4]    # a field added to the list is dropped here
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
