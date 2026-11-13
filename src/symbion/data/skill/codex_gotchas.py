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


_HEREDOC = re.compile(r"<<-? *(['\"]?)(\w+)\1")


def _scan(text, cut, outer="", q=""):
    """(where the scan stopped, the quote state there), from state `q`. With
    `cut`, it stops at the first `;`, `&`, `|`, `)` or newline outside quotes
    and comments, or at `outer`, the quote around another shell's command. A
    backslash outside single quotes escapes the next character; `>&` and `&>`
    are redirections; a `$(` in the command holds its own `)`; a comment runs
    to its line's end. Without `cut`, a heredoc's body is skipped: its state is
    a newline and its delimiter."""
    d, i = 0, 0
    while i < len(text):
        if q.startswith("\n"):
            m = re.compile(r"(?m)^" + re.escape(q[1:]) + r"$").search(text, i)
            if not m:
                return len(text), q
            i, q = m.end(), ""
            continue
        c = text[i]
        if q == "#":
            if c == "\n":
                if cut:
                    return i, q
                q = ""
        elif q != "'" and c == "\\":
            i += 2
            continue
        elif q:
            if c == q:
                q = ""
        elif cut and c == outer:
            return i, q
        elif not cut and text.startswith("<<", i) and (m := _HEREDOC.match(text, i)):
            j = text.find("\n", m.end())
            q = "\n" + m[2]
            if j < 0:
                return len(text), q
            i = j + 1
            continue
        elif c in "'\"":
            q = c
        elif c == "#" and text[i - 1:i] in ("", " ", "\t", "(", "\n"):
            q = "#"
        elif c == "(" and text[i - 1:i] == "$":
            d += 1
        elif c == ")" and d:
            d -= 1
        elif cut and (c in ";|)\n" or c == "&" and ">" not in (text[i - 1:i], text[i + 1:i + 2])):
            return i, q
        i += 1
    return i, q


def nosymbion(cmd):
    """The command less its symbion commands and their heredoc bodies, as
    gotchas.sh cuts them: writing about a file is no use of it. A variable
    set to symbion, or a path to it, is symbion where it is called."""
    for name in re.findall(r"""(?:^|[\s;&|(])([A-Za-z_]\w*)=["']?(?:[^\s"';&|]*/)?symbion(?=[\s"';&|]|$)""",
                           cmd):
        cmd = re.sub(r"\$(?:%s|\{%s\}) " % (name, name), "symbion ", cmd)
    # A command runs to the end of its command, as gotchas.sh cuts it: `--body
    # 'first; then src/x.py'` fired on src/x.py (audit, 2026-11-08). The quote
    # state before the word, carried along so each character is read once:
    # inside a comment the word is no command; inside a quoted string (`ssh h
    # 'cd x; symbion …'`) it is another shell's, which that quote also ends,
    # unless a `$(` opens it (`"$(symbion …)"`), which its `)` ends.
    out, rest, state = [], cmd, ""
    while (m := re.search(r"symbion ", rest)):
        pre, rest = rest[:m.start()], rest[m.end() - 1:]
        state = _scan(pre, False, q=state)[1]
        word = re.split(r'[;&|(\n]', pre)[-1].lstrip(" ")     # `grep "symbion x"`: no command
        if " " in word or (word and not word.endswith("/")) or state == "#":
            out.append(pre + "symbion")
            continue
        # in a heredoc's body, or opened by `$(`: a new context
        outer = "" if state.startswith("\n") or re.search(r"\$\( *$", pre) else state
        seg = rest[:_scan(rest, True, outer)[0]]
        rest = rest[len(seg):]
        if "<<" in seg:                             # its body runs to the delimiter's line
            d = seg.split("<<", 1)[1].lstrip("-").lstrip(" ")
            d = d[1:].split(d[0], 1)[0] if d[:1] in ("'", '"') else re.match(r"\w*", d).group()
            i = rest.find("\n" + d) if d else -1     # a `<<<` has no delimiter line
            rest = rest[i + 1 + len(d):] if i >= 0 else rest
        out.append(pre)
    return "".join(out) + rest


def strings(x):
    """Every string in a tool's arguments, nested or not."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, (list, dict)):
        for v in x.values() if isinstance(x, dict) else x:
            yield from strings(v)


def _before(call, name):
    """The text before each place the call holds `name` with no path char
    after it. A pattern with a dir before the name tried every start of a
    run with no blank: 16 KB took 2.4 s, and the timeout is 5 s (audit,
    2026-11-12)."""
    for m in re.finditer("(?=" + re.escape(name) + r"(?![A-Za-z0-9._~/-]))", call):
        yield call[:m.start()]


def named(call, name, root):
    """Whether the call names `name` as gotchas.sh does: at a word's start,
    after `./` or a char no path holds, or after a dir ending in `/<root>/`
    (with no root, any dir). README.md must not match docs/README.md or
    README.md.bak. Absolute paths keep the root."""
    for before in _before(call, name):
        word = re.split(r"[\s\"']", before)[-1]
        bare = word[:-2] if word.endswith("./") else word
        if not re.fullmatch(r"[A-Za-z0-9._~/@+-]", bare[-1:]) or \
                word.endswith(f"/{root}/" if root else "/"):
            return True
    return False


def in_worktree(call, name, root):
    """Whether the call names `name` in a linked worktree of the repo whose
    root dir is `root`: the dir before it holds a `.git` file naming
    `<root>/.git/worktrees/`, as gotchas.sh reads it. Tried on a line's or a
    quote's whole tail (a path with a blank), then a word's (audit, 2026-11-08)."""
    for before in _before(call, name):
        line = re.split(r"[\n\"']", before)[-1]
        for d in (line, re.split(r"[\s\"']", line)[-1]):
            if not d.endswith("/"):
                continue
            try:
                with open(d + ".git", encoding="utf-8") as f:
                    g = f.readline().rstrip("\n")
            except (OSError, UnicodeDecodeError):
                continue
            if g.startswith("gitdir: ") and f"/{root}/.git/worktrees/" in g:
                return True
    return False


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
    # Not splitlines(): it splits at U+2028 too, which a row's text may hold.
    for row in filter(None, listing.read_text(encoding="utf-8").split("\n")):
        ident, name, text, root = row.split("\t")[:4]    # a field added to the list is dropped here
        if ident in seen or name not in call:
            continue
        if not named(call, name, root) and not (root and in_worktree(call, name, root)):
            continue
        output.append(json.loads('"' + text + '"'))
        seen.add(ident)
        for suffix in ((".seen", ".edit") if edit else (".seen",)):
            with Path(mark + suffix).open("a", encoding="utf-8") as stream:
                stream.write(ident + "\n")
    if output:
        # The words every row shares, written beside the list: once, before the first row.
        head = Path(str(listing) + ".head")
        if head.is_file():
            output.insert(0, json.loads('"' + head.read_text(encoding="utf-8") + '"'))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "additionalContext": "\n".join(output)}}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # An unavailable cache or malformed event never fails a tool call.
