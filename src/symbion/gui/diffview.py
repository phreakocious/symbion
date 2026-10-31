"""A unified diff, parsed into files, hunks and lines, and drawn as one piece
of HTML, unified or side by side (the diff viewer design, 2026-10-30).

The input is git's text (`gitref.commit_show`, `gitref.diff_text`). A hunk
is read by the counts in its `@@` line, not by its lines' first characters:
a deleted line `-- x` reads `--- x`, as a file's header does."""
from __future__ import annotations

import codecs
import html
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

MODES = {"unified": "unified", "split": "side by side"}

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# git's lines about a file that are not its text, shown as it words them
_NOTES = ("new file mode", "deleted file mode", "old mode", "new mode", "similarity index",
          "dissimilarity index", "rename from", "rename to", "copy from", "copy to",
          "Binary files")
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
MARK_CHARS = 2000          # a pair longer than this gets no word marks: the match is quadratic


@dataclass
class File:
    name: str
    notes: list = field(default_factory=list)
    # [(the @@ line, [(op, old line no., new line no., text)])];
    # op is " ", "-", "+", or "\\" for git's "No newline at end of file"
    hunks: list = field(default_factory=list)
    plus: int = 0
    minus: int = 0


def _unquote(p: str) -> str:
    """A path as git C-quotes one with a quote, a backslash or a byte past ASCII."""
    if not p.startswith('"'):
        return p
    return codecs.escape_decode(p[1:-1].encode())[0].decode("utf-8", "replace")


def _side(p: str, prefix: str) -> str | None:
    p = _unquote(p.rstrip("\t"))         # git ends a name holding a space with a tab
    return None if p == "/dev/null" else p.removeprefix(prefix)


def parse(text: str) -> list[File]:
    files, f, lines, left, right, old, new = [], None, None, 0, 0, 0, 0
    for ln in text.split("\n"):
        if lines is not None and (left > 0 or right > 0 or ln.startswith("\\")):
            op = ln[:1]
            if op == "\\":
                lines.append(("\\", None, None, ln[1:].strip()))
            elif op == "-":
                lines.append(("-", old, None, ln[1:]))
                old, left, f.minus = old + 1, left - 1, f.minus + 1
            elif op == "+":
                lines.append(("+", None, new, ln[1:]))
                new, right, f.plus = new + 1, right - 1, f.plus + 1
            else:
                lines.append((" ", old, new, ln[1:]))
                old, new, left, right = old + 1, new + 1, left - 1, right - 1
            continue
        lines = None
        if ln.startswith("diff --git "):
            rest = ln[len("diff --git "):]
            # `a/P b/P` when the names are one; a rename's comes from its own lines
            f = File(rest[2:2 + (len(rest) - 5) // 2] if not rest.startswith('"') else rest)
            files.append(f)
        elif f is None:
            continue
        elif m := _HUNK.match(ln):
            old, new = int(m[1]), int(m[3])
            left, right = int(m[2] or 1), int(m[4] or 1)
            lines = []
            f.hunks.append((ln, lines))
        elif ln.startswith("--- ") and (p := _side(ln[4:], "a/")):
            f.name = p
        elif ln.startswith("+++ ") and (p := _side(ln[4:], "b/")):
            f.name = p
        elif ln.startswith(_NOTES):
            f.notes.append(ln)
            if ln.startswith("rename to "):
                f.name = _unquote(ln[len("rename to "):])
    return files


def rows(lines) -> list[tuple]:
    """A hunk's lines as side-by-side rows of indexes into `lines`: (left,
    right), a run of deleted lines beside the run of inserted lines after it,
    None where one run is shorter; a context line on both sides; a note
    ("\\") alone, as (None, None, index), after the run it ends."""
    out, dels, adds, notes = [], [], [], []

    def flush():
        for i in range(max(len(dels), len(adds))):
            out.append((dels[i] if i < len(dels) else None, adds[i] if i < len(adds) else None))
        out.extend((None, None, k) for k in notes)
        dels.clear(), adds.clear(), notes.clear()

    for i, (op, *_) in enumerate(lines):
        if op == "-":         # git puts a block's deletions before its insertions
            dels.append(i)
        elif op == "+":
            adds.append(i)
        elif op == "\\":
            notes.append(i)
        else:
            flush()
            out.append((i, i))
    flush()
    return out


def _marked(a: str, b: str) -> tuple[str, str]:
    """A deleted line and the inserted line beside it, escaped, the words
    that differ marked. A pair with little in common is left whole: its
    washes already say it all changed."""
    if len(a) + len(b) > MARK_CHARS:
        return html.escape(a), html.escape(b)
    olds, news = _TOKEN.findall(a), _TOKEN.findall(b)
    sm = SequenceMatcher(None, olds, news, autojunk=False)
    if sm.ratio() < 0.4:
        return html.escape(a), html.escape(b)
    left, right = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        x, y = html.escape("".join(olds[i1:i2])), html.escape("".join(news[j1:j2]))
        if op == "equal":
            left.append(x), right.append(y)
            continue
        if x:
            left.append(f'<span class="sb-dx">{x}</span>')
        if y:
            right.append(f'<span class="sb-dx">{y}</span>')
    return "".join(left), "".join(right)


def _texts(lines, pairs) -> list[str]:
    """Each line's HTML: a paired line's words marked, the rest escaped."""
    out = [html.escape(t) for *_, t in lines]
    for p in pairs:
        if len(p) == 2 and p[0] is not None and p[1] is not None and p[0] != p[1]:
            out[p[0]], out[p[1]] = _marked(lines[p[0]][3], lines[p[1]][3])
    return out


_CLS = {" ": "ctx", "-": "del", "+": "ins"}


def _no(n) -> str:
    return f'<span class="ln">{"" if n is None else n}</span>'


def _unified(lines) -> list[str]:
    texts, out = _texts(lines, rows(lines)), []
    for (op, a, b, _), t in zip(lines, texts):
        out.append(f'<span class="nl">{t}</span>' if op == "\\" else
                   f'{_no(a)}{_no(b)}<span class="{_CLS[op]}">{t}</span>')
    return out


def _split(lines) -> list[str]:
    pairs = rows(lines)
    texts, out = _texts(lines, pairs), []
    for p in pairs:
        if len(p) == 3:
            out.append(f'<span class="nl">{texts[p[2]]}</span>')
            continue
        for i, side in zip(p, (1, 2)):
            if i is None:
                out.append('<span class="ln none"></span><span class="none"></span>')
            else:
                op = lines[i][0]
                out.append(f'{_no(lines[i][side])}<span class="{_CLS[op]}">{texts[i]}</span>')
    return out


def counts(plus, minus) -> str:
    """`+a −b` as the diff's head and the commit page show it."""
    if plus is None:
        return '<span class="sb-diff-n">binary</span>'
    return (f'<span class="sb-diff-n"><span class="sb-plus">+{plus}</span> '
            f'<span class="sb-minus">−{minus}</span></span>')


def render(files, mode: str, uid: str) -> str:
    """`files` as one block of HTML in `mode`, every text escaped: a list
    of the files when there are several, each a link to its section (ids
    from `uid`, unique on the page), then each file's head, notes and hunks."""
    out = ['<div class="sb-diff">']
    if len(files) > 1:
        out.append('<div class="sb-diff-files">')
        out += [f'<a href="#{uid}-{i}">{html.escape(f.name)}</a>{counts(f.plus, f.minus)}'
                for i, f in enumerate(files)]
        out.append("</div>")
    draw = _split if mode == "split" else _unified
    for i, f in enumerate(files):
        out.append(f'<section class="sb-diff-file" id="{uid}-{i}"><div class="sb-diff-fh">'
                   f'<span>{html.escape(f.name)}</span>{counts(f.plus, f.minus)}</div>')
        out += [f'<div class="sb-diff-note">{html.escape(n)}</div>' for n in f.notes]
        if f.hunks:
            out.append(f'<div class="sb-diff-grid sb-diff-{mode}">')
            for head, lines in f.hunks:
                out.append(f'<span class="hk">{html.escape(head)}</span>')
                out += draw(lines)
            out.append("</div>")
        out.append("</section>")
    out.append("</div>")
    return "".join(out)
