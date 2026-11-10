"""The note row, reused by every surface. Colors are CSS classes from theme.py
-- no hexes here, the hex audit forbids them outside theme.py.

Refreshables are defined PER call (per page/client). A module-level
@ui.refreshable accumulates instances across pages and its .refresh() then
touches a deleted client (RuntimeError 'client has been deleted').
"""
from __future__ import annotations

import functools
import hashlib
import html
import itertools
import logging
import os
import re
import subprocess
import sys
from contextlib import contextmanager, suppress
from datetime import datetime
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import NamedTuple
from urllib.parse import quote

from fastapi.responses import PlainTextResponse
from markdown_it.token import Token
from nicegui import app, run, ui
from nicegui.element import Element
from nicegui.elements.mixins.text_element import TextElement
from nicegui.elements.mixins.value_element import ValueElement
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

from .. import api, catalog, gitref, term
from .. import summary as summ
from .. import store as S
from . import diffview
from .filters import href

# A checkout's markdown file, in the viewer, is read by ui.markdown (markdown2).
# It carries code-style identifiers (snake_case_module), and markdown2's
# default emphasis mangles intra-word underscores to <em>; `code-friendly`
# keeps *asterisk* emphasis and disables underscore emphasis; `mermaid` draws
# a ```mermaid block. A row body is read in term.body_parser instead, as at a
# terminal.
FILE_MD_EXTRAS = ["fenced-code-blocks", "tables", "code-friendly", "mermaid"]

# Mermaid's default theme draws dark lines on the dark page. A block's own
# init, after this one, still wins.
_MERMAID = re.compile(r"^```mermaid[ \t]*\n", re.M)


def dark_mermaid(text: str) -> str:
    return _MERMAID.sub(lambda m: m.group(0) + "%%{init: {'theme': 'dark'}}%%\n", text)


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """A fenced block in a language pygments knows, coloured as markdown2
    coloured it, in the .codehilite classes ui.markdown serves the CSS for.
    Any other block gets "", and markdown-it escapes it plain."""
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return ""
    return (f'<pre class="codehilite"><code>'
            f'{highlight(code, lexer, HtmlFormatter(nowrap=True))}</code></pre>')


# A bare http(s) URL in prose; the punctuation that ends a sentence or closes
# a bracket around it is the prose's (as GFM's autolinks read it).
_URL = re.compile(r"https?://[^\s<>\"'`]+")


def _url_end(url: str) -> str:
    while url[-1] in ".,:;!?*_~" or url[-1] == ")" and url.count(")") > url.count("(") \
            or url[-1] == "]" and url.count("]") > url.count("["):
        url = url[:-1]
    return url


def _linkify(state) -> None:
    """Each bare URL in a body's prose as a link (the user, 2026-10-16:
    "external URLs in bodies should become clickable"). Read off the parsed
    text, so one in code or in a link already made stays as it is.
    markdown-it's own linkify wants linkify-it-py."""
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        out, depth = [], 0
        for tok in block.children:
            depth += tok.nesting if tok.type in ("link_open", "link_close") else 0
            if tok.type != "text" or depth or "://" not in tok.content:
                out.append(tok)
                continue
            text, at = tok.content, 0
            for m in _URL.finditer(text):
                url = _url_end(m[0])
                href = state.md.normalizeLink(url)
                if len(url) <= len("https://") or not state.md.validateLink(href):
                    continue
                out.append(Token("text", "", 0, content=text[at:m.start()]))
                out += [Token("link_open", "a", 1, attrs={"href": href}),
                        Token("text", "", 0, content=url), Token("link_close", "a", -1)]
                at = m.start() + len(url)
            out.append(Token("text", "", 0, content=text[at:]))
        block.children = out


def _titles_as_tips(state) -> None:
    """A link's title as the GUI's own tip (`data-tip`, chrome's _TIPS_JS): the
    browser's waited about a second and drew in the system's small font (the
    user, 2026-11-07). `aria-description` keeps it for a screen reader."""
    for block in state.tokens:
        for tok in block.children or ():
            if tok.type == "link_open" and (tip := tok.attrs.pop("title", None)):
                tok.attrSet("data-tip", tip)
                tok.attrSet("aria-description", tip)


_BODY_MD = term.body_parser(_highlight)
_BODY_MD.core.ruler.push("sb_linkify", _linkify)
_BODY_MD.core.ruler.push("sb_tips", _titles_as_tips)


@functools.lru_cache(maxsize=1000)
def _body_html(body: str) -> str:
    return _BODY_MD.render(body)


def _blocks(body: str) -> list[str]:
    """The body's top-level markdown blocks, each as the source lines it
    spans: a fence or a table is one block, blank lines and all."""
    lines = body.splitlines(keepends=True)
    return ["".join(lines[t.map[0]:t.map[1]]).rstrip("\n") for t in _BODY_MD.parse(body)
            if t.level == 0 and t.map]


def diff_blocks(old: str, new: str) -> list[tuple[str, str]]:
    """old against new, block by markdown block, each rendered as a body is:
    `=` kept, `-` gone, `+` came."""
    # ponytail: a block is the unit, so a one-word edit shows its paragraph
    # twice and a list is one block; mark the words inside a changed block
    # if that bites.
    a, b = _blocks(old), _blocks(new)
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out += [("=", t) for t in a[i1:i2]]
        else:
            out += [("-", t) for t in a[i1:i2]] + [("+", t) for t in b[j1:j2]]
    return out


def word_diff(old: str | None, new: str | None) -> str:
    """old against new word by word, escaped for HTML: what went in <del>,
    what came in <ins>. A `checked` is one line, which a block diff shows
    whole twice: a path changed inside a long one, and nothing said where."""
    a, b = (re.findall(r"\S+\s*", t or "") for t in (old, new))
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        gone, came = html.escape("".join(a[i1:i2])), html.escape("".join(b[j1:j2]))
        if op == "equal":
            out.append(gone)
            continue
        if gone:
            out.append(f"<del>{gone}</del>")
        if came:
            out.append(f"<ins>{came}</ins>")
    return "".join(out) or "—"


def changes(old, new) -> list[str]:
    """What `new`, the version after `old`, changed: a phrase each. An
    earlier version said nothing of it, so a change to one field of a long
    row read as no change at all (the user, 2026-10-09). The body, checked
    and result are only named: the card under the line shows them."""
    def name(t):
        return summ.ref_label(t) if t.name else t.type

    out = []
    if old.kind != new.kind:
        out.append(f"kind {old.kind} → {new.kind}")
    if old.target != new.target:
        out.append(f"target {name(old.target)} → {name(new.target)}")
    if old.status != new.status:
        out.append(f"{old.status or 'no status'} → {new.status or 'no status'}")
    out += [f for f in ("body", "checked", "result", "options")
            if getattr(old, f) != getattr(new, f)]
    a, b = old.measurements or {}, new.measurements or {}
    out += [f"{k} {a[k]} → {b[k]}" if k in a and k in b else f"+{k} {b[k]}" if k in b
            else f"−{k}" for k in sorted(a.keys() | b.keys(), key=str.casefold)
            if a.get(k) != b.get(k)]
    for f, label in (("due", "due"), ("arc_id", "arc")):
        if getattr(old, f) != getattr(new, f):
            out.append(f"{label} {getattr(old, f) or 'none'} → {getattr(new, f) or 'none'}")
    out += [f"−#{t}" for t in old.tags if t not in new.tags]
    out += [f"+#{t}" for t in new.tags if t not in old.tags]
    out += [f"−↗ {name(r)}" for r in old.refs if r not in new.refs]
    out += [f"+↗ {name(r)}" for r in new.refs if r not in old.refs]
    return out


def edited(old: str, new: str) -> bool:
    """A rewrite a diff helps: not one that kept the old body whole, whose
    seams mark what it added (summary.seams), and not a replacement, which a
    diff shows all gone and all new. By words: by letters, two texts in one
    language share a third by chance, and a 6 KB pair took 0.37 s to
    compare, against 3 ms by words."""
    words = [re.findall(r"\S+", t) for t in (old, new)]
    return old not in new and SequenceMatcher(None, *words, autojunk=False).ratio() >= 0.3


class NoteMarkdown(ui.markdown):
    """A row's body, read as `list` and `show` read it at a terminal: the
    same parser, so a list, a `~~strike~~` and a raw tag come out alike.
    ui.markdown's markdown2 read them otherwise, so this replaces its
    renderer, keeping its DOMPurify pass and its .codehilite CSS."""
    # ponytail: overrides a private nicegui hook; if it moves,
    # test_a_tag_in_a_body_prints_as_text fails, and ui.html takes the HTML.
    def _handle_content_change(self, content: str) -> None:
        self._props["innerHTML"] = _body_html(content)


# A cite as `show` reads one (summary._CITE), not inside a URL or a link
# already made. Bodies cite rows by id ("settled by <id>"), and the GUI had
# no way to follow one but pasting it into the search.
_NOTE_ID = re.compile(r"(?<![/=[])" + summ._CITE.pattern)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
# Only a body that may hold a block _CODE misses pays for a parse.
_MAYBE_BLOCK = re.compile(r"~~~|^(?: {4}|\t)", re.M)


@functools.lru_cache(maxsize=8)
def _tails(store_dir: str, _stamp) -> dict:
    """summary.cite_ids of the store. `_stamp` is the notes file's mtime and
    size, so a row written while the server runs is seen on the next render."""
    return summ.cite_ids([n.id for n in S.load(store_dir)])


@functools.lru_cache(maxsize=8)
def _closed(store_dir: str, _stamp) -> frozenset:
    """The ids of every version of each entry whose newest version is
    resolved. `_stamp` as `_tails`'s."""
    loaded = S.load(store_dir)
    by_id = {n.id: n for n in loaded}
    out = set()
    for n in S.heads(loaded):
        if S.read_status(n) == "resolved":
            while n and n.id not in out:          # a looped chain ends here
                out.add(n.id)
                n = by_id.get(n.supersedes)
    return frozenset(out)


# A cited id's title: an id says nothing until it is clicked, and a handoff
# cites several (the design critique, 2026-11-07).
TIP_CHARS = 60
# The measurements a compact card names before "+N more".
MEAS_SHOWN = 6


@functools.lru_cache(maxsize=8)
def _tips(store_dir: str, _stamp) -> dict:
    """Each version's id to its entry's title as a link: the newest version's
    kind, status where it has one, and first words. `_stamp` as `_tails`'s."""
    loaded = S.load(store_dir)
    by_id = {n.id: n for n in loaded}
    out = {}
    for n in S.heads(loaded):
        tip = " · ".join(filter(None, (n.kind, S.read_status(n),
                                       summ.clip(n.body, TIP_CHARS))))
        while n and n.id not in out:              # a looped chain ends here
            out[n.id] = tip
            n = by_id.get(n.supersedes)
    return out


def link_ids(body: str, store_dir) -> str:
    """Each full note id in `body`, and each tail of a row in this store, as
    a markdown link to that note, except inside code, where a link would
    print as literal brackets. A tail of no row here stays text: rows cite
    other stores' rows too. A link to a resolved entry is struck through,
    as a done item is, and its tip says so (the user, 2026-10-16): every
    link's markdown title, which `_titles_as_tips` makes its tip, is its
    entry's kind, status and first words."""
    def link(m):
        nid = m[1] if len(m[1]) > 17 else tails().get(m[1])
        if not nid:
            return m[1]
        tip = _tips(str(store_dir), stamp()).get(nid, "").replace("\\", "\\\\") \
            .replace('"', '\\"')
        text = f"~~{m[1]}~~" if nid in _closed(str(store_dir), stamp()) else m[1]
        return f'[{text}](/notes?id={nid} "{tip}")'

    def stamp():
        st = S.notes_path(store_dir).stat()
        return st.st_mtime_ns, st.st_size

    def tails():
        return _tails(str(store_dir), stamp())

    def prose(text):
        return "".join(p if i % 2 else _NOTE_ID.sub(link, p)
                       for i, p in enumerate(_CODE.split(text)))
    # A code block by the renderer's own reading: a `~~~` fence or an
    # indented block printed the link's brackets.
    lines = body.split("\n")
    code = {i for tok in _BODY_MD.parse(body) if tok.type in ("fence", "code_block") and tok.map
            for i in range(*tok.map)} if _MAYBE_BLOCK.search(body) else set()
    runs = itertools.groupby(range(len(lines)), key=lambda i: i in code)
    return "\n".join("\n".join(lines[i] for i in ix) if is_code
                     else prose("\n".join(lines[i] for i in ix))
                     for is_code, ix in ((k, list(g)) for k, g in runs))


def kind_class(kind: str) -> str:
    """A kind's colour, as `list` prints it, for its chip and its row's edge;
    a kind the store declares shares one (2026-10-01; chips by bits before,
    which left `decision` and `idea` uncoloured), unless its `color` picks
    one: theme.kind_css gives its own class that colour."""
    return f"sb-kind-{kind}" if kind in term.KIND else f"sb-kind-own sb-kind-{kind}"


def target_href(type: str, name: str | None) -> str:
    """A target's own page: an arc's checklist, else the ref-aware object
    view. `project` has none, so it gets its rows in the filtered view."""
    if not name:
        return href(type=type)
    if type == "arc":
        return f"/arc?id={quote(name)}"
    return f"/object?type={quote(type)}&name={quote(name, safe='')}"


def bare(n) -> bool:
    """A row with nothing to say, no body and no verdict: a seeded box. What
    it targets is its whole content, so that leads instead of a blank line."""
    return not n.body.strip() and not n.spec.verdict


@functools.lru_cache(maxsize=8)
def _arc_names(store_dir: str, _stamp) -> dict:
    """Arc id -> name. `_stamp` is arcs.jsonl's mtime and size, as `_tails`'s."""
    return {a.id: a.name for a in S.load_arcs(store_dir)}


TIP_COMMITS = 8


def check_tip(cfg, prov, state, distance, at=None) -> str:
    """What the badge was measured against. The chip lit up on hover and a
    click did nothing (the user, 2026-09-08); `behind N` asks the reader to
    go and look at N commits, so the tip lists them. `at` names the commit
    for a rewritten stamp's successor, which no run was stamped at."""
    sha = S.stamp_sha(prov)
    if not sha or state == "external":
        return ""
    at = at or f"stamped at {sha[:7]}"
    if state == "current":
        return f"{at}, this checkout's HEAD"
    if state == "diverged":
        return f"{at}, on another line of development than HEAD"
    if state == "dangling":
        return (f"{at}, which no branch or tag holds: an amend, a rebase or a deleted "
                f"branch left it behind. Re-run the check to stamp a live commit")
    if state == "rewritten" and (s := gitref.successor(cfg, prov)):
        tree = {True: "; same tree", False: "; the tree differs"}.get(s.same_tree, "")
        moved = {"sha": s.sha, **({"repo": prov["repo"]} if "repo" in prov else {})}
        return (f"{at}; rewritten as {s.sha[:7]}, matched by author date and subject{tree}\n"
                + check_tip(cfg, moved, s.state, s.distance, at=s.sha[:7]))
    if state not in ("behind", "ahead"):
        return at
    cfg = gitref.stamp_cfg(cfg, prov)            # the commits of the stamp's repo
    lines = gitref.oneline(cfg, f"{sha}..HEAD" if state == "behind" else f"HEAD..{sha}",
                           TIP_COMMITS)
    more = f"\n+{distance - len(lines)} more" if distance > len(lines) else ""
    since = (f"HEAD is {summ._count(distance, 'commit')} past it" if state == "behind"
             else f"it is {summ._count(distance, 'commit')} past HEAD")
    return f"{at}; {since}:\n" + "\n".join(lines) + more


def _check_chip(ctx, n, git_head=None) -> tuple[dict, object, object]:
    """A check's state chip, what fills its tip on the first hover, and what
    a click opens: each None where there is none."""
    state, distance = api.verdict_state(ctx.cfg, n, git_head)
    text = f"{state} {distance}" if state in ("behind", "ahead") else state
    # An age with no tip read as vague (the user, 2026-10-07): say what it dates.
    fixed_tip = ""
    if state == "unverifiable":
        text = f"unverifiable — {api.why_unverifiable(n.provenance)}"
    elif state == "external":
        text = f"external — {summ.age_phrase(n.provenance['at'])}"
        fixed_tip = (f"ran {S.shown(n.provenance['at'])}, against something outside the "
                     f"repo: no commit makes it stale, only time")
    elif state == "unstamped":
        text = f"unstamped — {summ.age_phrase(n.created_at)}"
        fixed_tip = (f"written {S.shown(n.created_at)}, before symbion stamped checks: "
                     f"no commit is known for it")
    elif state == "diverged":
        # A run on a live branch says look there, not re-run: in red, the
        # demo store's newest checks read as faults (the user, 2026-10-22).
        sha = S.stamp_sha(n.provenance)
        if b := gitref.unmerged(gitref.stamp_cfg(ctx.cfg, n.provenance), [sha]).get(sha):
            text = f"diverged — on {b}"
    elif state == "rewritten" and (s := gitref.successor(ctx.cfg, n.provenance, git_head)):
        text = f"rewritten — {api.rewritten_words(s)}"
    cls = {"current": "sb-chip-good", "behind": "sb-chip-notable", "external": "",
           "pending": "", "unstamped": "", "rewritten": "sb-chip-notable",
           "ahead": "sb-chip-notable",
           "diverged": "sb-chip-notable", "dangling": "sb-chip-bad",
           "unverifiable": "sb-chip-bad"}[state]
    chip = {"t": text, "c": f"sb-chip {cls}".strip(), "m": "check-state"}
    if fixed_tip:
        return chip | {"tip": fixed_tip}, None, None
    if not S.stamp_sha(n.provenance) or state == "pending":
        return chip, None, None
    # Filled on first hover: a `git log` per badge at render time doubled
    # the cost of a board of checks (45 badges: 1.5s to 2.9s).
    if state not in ("behind", "ahead"):
        return chip | {"tip": "", "key": "check"}, \
            lambda: check_tip(ctx.cfg, n.provenance, state, distance), None
    return chip | {"tip": "", "key": "check", "open": True}, \
        lambda: check_tip(ctx.cfg, n.provenance, state, distance) + f"\n{_CLICK[state]}", \
        lambda: check_dialog(ctx, n, state, distance)


_CLICK = {"behind": "click: the commits, and what they changed",
          "ahead": "click: the commits",
          "since": "click: the commits, and what they changed in the file"}


def since_tip(cfg, n) -> str:
    """The commits a card's "N commits since" counts, newest first: like
    `behind N`, the count asks the reader to go and look at them."""
    shas = gitref.changed_since(cfg, [n])[n.id] or []
    if not shas:
        return ""
    subj = gitref.subjects(cfg, shas[:TIP_COMMITS])
    more = f"\n+{len(shas) - TIP_COMMITS} more" if len(shas) > TIP_COMMITS else ""
    return (f"{n.target.name} changed in {summ._count(len(shas), 'commit')} since this entry:\n"
            + "\n".join(f"{s[:7]} {subj.get(s, '')}" for s in shas[:TIP_COMMITS]) + more)


def commit_tip(cfg, name) -> str:
    """A commit chip's tip: the commit's subject, or that this checkout has
    no such commit."""
    return next(iter(gitref.subjects(cfg, [name]).values()), "not a commit in this checkout")


def _due(n, status) -> tuple[str, str, str | None]:
    """Words while the row can still fire, the bare date once it cannot: a
    resolved row is never late. (text, class, tip)."""
    state = S.due_state(n.due) if status == "open" else None
    if state is None:
        return f"due {S.shown(n.due)}", "sb-chip", None
    past, days, _ = state
    cls = "sb-chip-bad" if past else "sb-chip-notable" if days <= summ.DUE_DAYS else ""
    return summ.due_phrase(state), f"sb-chip {cls}".strip(), S.shown(n.due)


def snippet(body: str, words, n: int = summ.BODY_CHARS) -> str:
    """The clipped body, or when the clip holds none of `words`, a clip that
    starts shortly before the first of them: a search result that cannot
    show why it matched reads as a false hit."""
    head = summ.clip(body, n)
    low = [w.lower() for w in words]
    if not low or any(w in head.lower() for w in low):
        return head
    text = summ.flatten(body)
    at = min((i for i in (text.lower().find(w) for w in low) if i >= 0), default=-1)
    if at < 0:                       # the hit is in the target, a ref or the verdict
        return head
    start = text.rfind(" ", 0, max(at - n // 4, 0)) + 1
    return "…" + summ.clip(text[start:], n)


def marked(text: str, words) -> str:
    """`text` escaped for HTML, each of `words` wrapped in <mark>, any case."""
    if not words:
        return html.escape(text)
    rx = re.compile("(" + "|".join(re.escape(w) for w in
                                   sorted(set(words), key=len, reverse=True)) + ")", re.I)
    return "".join(f"<mark>{html.escape(p)}</mark>" if i % 2 else html.escape(p)
                   for i, p in enumerate(rx.split(text)))


def heading(level: int, text: str = "") -> TextElement:
    """An `h1` or `h2` that looks as its classes say. A screen reader moves
    through a page by its headings, and every title was a div (the GUI
    review, 2026-11-07). `sb-h` takes off the browser's and Quasar's look."""
    return TextElement(tag=f"h{level}", text=text).classes("sb-h")


def ago(stamp: str, now: datetime | None = None) -> str:
    """How long ago, in the one unit that reads at a glance: `17m`, `4h`,
    `3d`; a stamp older than two months is its date. The full stamp is the
    tooltip. One that will not parse prints as `S.shown` prints it."""
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return S.shown(stamp)
    if then.tzinfo is None:
        then = then.astimezone()
    secs = max(0, ((now or S.now_shown()) - then).total_seconds())
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 86400 * 60:
        return f"{int(secs // 86400)}d"
    return S.shown(stamp)[:10]


def short_id(note_id: str) -> str:
    """The id's last two parts, as rows cite one another (`735180-a9c`): the
    date and time are already the row's age."""
    return "-".join(note_id.split("-")[-2:])


# Endings pygments names no lexer for, though one fits (the user, 2026-10-15).
_MORE_LEXERS = {".tfvars": "terraform", ".tfstate": "json"}


@functools.lru_cache(maxsize=1024)
def _lexer(filename: str):
    """pygments' lexer for a file name, or None. Kept: pygments tries every
    lexer's patterns, about 4.5 ms a name, and a page asks once a card."""
    try:
        return get_lexer_for_filename(filename)
    except ClassNotFound:
        alias = next((a for end, a in _MORE_LEXERS.items()
                      if filename.lower().endswith(end)), None)
        return get_lexer_by_name(alias) if alias else None


def view_path(ctx, name, ttype=None):
    """Where the viewer reads `name`, when it shows its kind (markdown, or a
    file pygments names a language for): (the checkout's file, None), or
    (None, branch) for a file only the default branch holds; else None. A
    ref to a .tf file on main had no button while another branch was checked
    out (the user, 2026-10-15). A target name is typed text, so the file
    must resolve inside the checkout: `..` and a symlink out of it read
    nothing. `ttype`, a row's target or ref type, picks the repo in a store
    with [repos]."""
    path = api.object_path(ctx, ttype, name)
    if path is None:
        return None
    if path.is_file():
        # Read-only, the public demo: only what git tracks, which the leak
        # scan read; a gitignored file was readable (a reviewer, 2026-10-22).
        if S.read_only() and not gitref.tracked(ctx.cfg.for_type(ttype), path):
            return None
        return (path, None) if _viewable(path.name) else None
    # ponytail: the default branch only; a file only another branch holds reads nothing
    cfg = None if ttype == "doc" else ctx.cfg.for_type(ttype)   # a repo's row: its repo's file
    branch = cfg is not None and _viewable(name) and gitref.only_on_default_branch(cfg, name)
    return (None, branch) if branch else None


def fenced(text: str, filename: str) -> str:
    """`text` as one fenced code block, in the language pygments names for
    `filename`, as a body's is coloured. A fence longer than any run of
    backticks in the text is one it cannot close."""
    fence = "`" * max(3, 1 + max(map(len, re.findall("`+", text)), default=0))
    lexer = _lexer(filename)
    lang = (lexer.aliases or ["text"])[0] if lexer else ""
    return f"{fence}{lang}\n{text.removesuffix(chr(10))}\n{fence}"


def _viewable(filename: str) -> bool:
    return filename.lower().endswith(".md") or _lexer(filename) is not None


# ponytail: the whole file in one dialog; page it if a large one is slow.
VIEW_CHARS = 500_000


def demo_says(does: str) -> bool:
    """Under `serve --read-only`, the public demo: show what this control
    does in a real store, `does` ("stars the entry"), and return True, so
    the handler stops. False otherwise, and nothing shows."""
    if not S.read_only():
        return False
    ui.notify(f"Read-only demo: in your own store, this {does}.")
    return True


@contextmanager
def refusal_shown():
    """A write the store refuses, with the words that name its fix (a shared
    store's clone with no user.name is one): shown, as the add form shows it.
    Raised out of a click handler it only reached NiceGUI's log, and the click
    did nothing. The rest of the block is skipped. Under `serve --read-only`
    the text can name a path on the serving machine: the log keeps it."""
    try:
        yield
    except (ValueError, catalog.CatalogError) as e:
        if S.read_only():
            logging.getLogger(__name__).warning("refused a visitor: %s", e)
            ui.notify("Read-only demo: refused. The reason stays in the server's log.",
                      type="negative")
        else:
            ui.notify(str(e), type="negative")


class Reading(NamedTuple):
    """The store as this clone's reader sees it: its side, its rows (`seen_from`
    that side) and those by id. A page makes one and hands it to its cards."""
    side: str | None
    loaded: list
    by_id: dict


def reading(ctx) -> Reading:
    side = S.read_side(ctx.store_dir)
    loaded = summ.seen_from(S.load(ctx.store_dir), side)
    return Reading(side, loaded, {n.id: n for n in loaded})


def read_only_refused(e: Exception) -> None:
    """`app.on_exception`: a write refused under `serve --read-only` that no
    handler named still answers the click; so does one a pulled
    symbion.toml holds until `symbion trust`."""
    if isinstance(e, S.ReadOnly):
        ui.notify("Read-only demo: nothing here writes.")
    elif isinstance(e, S.Untrusted):
        ui.notify(str(e), type="negative")


def _reveal_cmd(path) -> list[str] | None:
    """The command that shows the file `path` selected in the desktop's file
    manager, never opened; None with no desktop to show it on. It runs where
    the serve runs: over an ssh tunnel, on the far host."""
    if sys.platform == "darwin":
        return ["open", "-R", str(path)]
    if sys.platform == "win32":
        return ["explorer", f"/select,{path}"]
    # ponytail: Linux opens the folder, the file unselected; D-Bus
    # FileManager1.ShowItems selects it where a file manager implements it.
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return ["xdg-open", str(path.parent)]
    return None


def _reveal_label() -> str:
    return {"darwin": "show in Finder", "win32": "show in Explorer"}.get(
        sys.platform, "open its folder")


def _editor_url(path) -> str:
    """The link that opens the file `path` in VS Code: the browser hands it
    to the editor, with no server code. `as_uri` quotes the path."""
    # ponytail: VS Code's scheme only; Cursor's or Zed's (cursor://, zed://) when one is asked for
    return "vscode://file" + path.as_uri().removeprefix("file://")


def file_buttons(path) -> None:
    """Two buttons for the file `path` on disk: show it in the file manager,
    and open it in VS Code (the user, 2026-10-16). None with no desktop
    where the serve runs: the reveal runs there, and the link names a path
    on that machine."""
    cmd = _reveal_cmd(path)
    if cmd is None:
        return
    label = _reveal_label()
    ui.button(icon="folder_open", on_click=lambda: _reveal(cmd)) \
        .props(f'flat dense round size=sm color=muted aria-label="{label}"') \
        .tooltip(label).mark("file-reveal")
    # Read-only, the link would name a path on the serving machine.
    ro = S.read_only()
    href = "" if ro else f'href="{_editor_url(path)}" '
    ui.button(icon="open_in_new",
              on_click=(lambda: demo_says("opens the file in VS Code")) if ro else None) \
        .props(f'flat dense round size=sm color=muted {href}aria-label="open in VS Code"') \
        .tooltip("open in VS Code").mark("file-editor")


def forge_buttons(cfg, sha) -> None:
    """Links to the commit `sha` on its forge, and to the pull requests that
    hold it (the user, 2026-10-30: "the diff, then jump to it, or its MR/PR");
    none for a commit no remote branch holds."""
    links = gitref.forge_links(cfg, sha)
    # Read-only, a link would name the serving repo's remote, which may be private.
    ro = S.read_only()
    for key, icon, tip in (("commit", "public", "the commit on {}"),
                           ("prs", "call_merge", "pull requests holding it on {}")):
        if key in links:
            tip = tip.format(links["host"])
            b = ui.button(icon=icon, on_click=(lambda t=tip: demo_says(f"opens {t}")) if ro
                          else None) \
                .props("flat dense round size=sm color=muted").tooltip(tip).mark(f"forge-{key}")
            # set, not parsed: the path comes from the remote's URL
            b._props.update({"aria-label": tip} |
                            ({} if ro else {"href": links[key], "target": "_blank",
                                            "rel": "noopener"}))


async def _reveal(cmd: list[str]) -> None:
    if demo_says("shows the file on the machine that serves this page"):
        return
    try:
        r = await run.io_bound(subprocess.run, cmd, capture_output=True, text=True,
                               timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        ui.notify(f"cannot show it: {e}", type="warning")
        return
    if r.returncode and sys.platform != "win32":     # explorer exits 1 when it works
        ui.notify(f"cannot show it: {(r.stderr or r.stdout).strip() or r.returncode}",
                  type="warning")


def viewer(title: str, fill, *, mono: bool = False, reveal=None, head=None,
           wide: bool = False) -> None:
    """The file viewer's dialog, opened: `title` and a close button over
    what `fill()` renders. The help opens in it too. `reveal`, a file on
    disk, adds `file_buttons` (the user, 2026-10-16: "is open in finder
    even a possibility?"). `wide` for a diff, which may read side by side."""
    # The card is what scrolls, so it takes the focus: Quasar's dialog focuses
    # its first [autofocus][tabindex], else its own node, the card's parent,
    # where Page Up/Down scrolled nothing (the user, 2026-10-16).
    with ui.dialog() as dialog, \
            ui.card().classes("bg-panel sb-md-card" + (" sb-diff-card" if wide else "")) \
                .props("autofocus tabindex=-1"):
        with ui.row(wrap=False).classes("items-center w-full sb-md-head"):
            ui.label(title).classes("sb-subtitle" + (" sb-mono" if mono else ""))
            ui.space()
            if reveal:
                file_buttons(reveal)
            if head:
                head()
            ui.button(icon="close", on_click=dialog.close) \
                .props('flat dense round size=sm aria-label="close"')
        fill()
    dialog.open()


def viewable(ctx, name, ttype=None):
    """What a button that shows the file `name` in a dialog needs: (icon, tip,
    click(shift)), or None when the viewer cannot show it. Markdown is
    rendered (the user, 2026-10-02), anything else pygments knows coloured,
    code most of all (the user, 2026-10-11). Read on the click, so the dialog
    shows the file as it is then. A shift-click shows a file on disk in the
    file manager instead (the user, 2026-10-16)."""
    if ttype == "commit":
        return _commit_viewable(ctx, name)
    src = view_path(ctx, name, ttype)
    if src is None:
        return None
    branch = src[1]
    md = (src[0].name if src[0] else name).lower().endswith(".md")
    revealable = src[0] is not None and _reveal_cmd(src[0]) is not None

    def click(shift: bool):
        path = (view_path(ctx, name, ttype) or (None,))[0]
        cmd = path and shift and _reveal_cmd(path)
        return _reveal(cmd) if cmd else _open()

    def _open():
        path, branch = view_path(ctx, name, ttype) or (None, None)
        try:
            text = (path.read_text(encoding="utf-8", errors="replace") if path else
                    gitref.blob_text(ctx.cfg.for_type(ttype), branch, name) if branch else None)
        except OSError:
            text = None
        if text is None:
            ui.notify(f"cannot read {name}", type="warning")
            return
        if "\0" in text or len(text) > VIEW_CHARS:
            ui.notify(f"{name} is not text, or is over {VIEW_CHARS:,} characters",
                      type="warning")
            return

        def fill():
            if md:
                ui.markdown(dark_mermaid(text), extras=FILE_MD_EXTRAS).mark("file-view")
            else:
                NoteMarkdown(fenced(text, path.name if path else name)).mark("file-view")
        # a path: its case is its name
        viewer(f"{name}, as on {branch}" if branch else name, fill, mono=True, reveal=path)

    tip = (f"read {name}" + (f", as on {branch}" if branch else "")
           + (f" · shift-click: {_reveal_label()}" if revealable else ""))
    return ("article" if md else "code"), tip, click


def _commit_viewable(ctx, sha):
    """`viewable` for a commit: its message and diff in the viewer, and its
    forge links in the dialog's head. The message wraps. Read on the click,
    as a file is: a chip renders with no git."""
    if not sha:
        return None

    async def click(shift: bool):
        # git on a worker thread, as the diff's parse is: a large commit's
        # `git show` held every page's loop (the GUI review, 2026-11-07).
        def read():
            cfg = gitref.commit_cfg(ctx.cfg, sha)
            return cfg, gitref.commit_show(cfg, sha)
        cfg, got = await run.io_bound(read)
        if got is None:
            ui.notify(f"no commit {sha[:12]} in this checkout", type="warning")
            return
        author, date, parents, message, diff = got
        subject, _, body = message.partition("\n")

        def lead():
            commit_meta(sha, author, date, parents, body)
        return await diff_dialog(subject or f"commit {sha[:12]}", diff, lead=lead,
                                 head=lambda: forge_buttons(cfg, sha), mono=False)

    return "difference", f"read commit {sha[:7]}", click


def commit_meta(sha, author, date, parents, body) -> None:
    """Under a commit's subject: who and when, what a merge's diff is
    against, and the message's body."""
    # the name only: an email on a page is one more thing a demo publishes
    ui.label(f"{sha[:12]} · {author} · {S.shown(date)}") \
        .classes("sb-subtitle sb-mono").mark("commit-meta")
    if len(parents) > 1:
        ui.label(f"a merge: the changes it brought onto {parents[0][:7]}, its first parent") \
            .classes("sb-subtitle").mark("commit-merge")
    if body.strip("\n"):
        ui.label(body.strip("\n")).classes("whitespace-pre-wrap").mark("commit-body")


# The reader's pick of how a diff reads. The serve sets no storage_secret, so
# NiceGUI keeps nothing per browser: the toggle sets a cookie, which the next
# page's request carries, and this page keeps the pick in its client.
DIFF_COOKIE = "sb-diff"
_DIFF_IDS = itertools.count()


def diff_mode() -> str:
    """This page's last pick, else the browser's, else unified, as git prints."""
    client = ui.context.client
    try:
        kept = client.request.cookies.get(DIFF_COOKIE)
    except RuntimeError:            # a client built with no request
        kept = None
    mode = client.storage.get(DIFF_COOKIE, kept)
    return mode if mode in diffview.MODES else "unified"


async def diff_dialog(title: str, text: str, *, lead=None, head=None,
                      mono: bool = True) -> None:
    """The viewer, wide, on the diff `text`, with the unified / side-by-side
    toggle in its head: `lead()` draws what goes above the diff, `head()`
    buttons beside the toggle. Side by side, the unified drawing comes too:
    a window too narrow for two sides shows it instead (theme.py). Parse and
    render run on a worker thread: on the event loop, a large diff held
    every visitor for its render (audit 2026-11-02). A caller returns this
    coroutine to its handler, which NiceGUI awaits."""
    mode = diff_mode()
    files = await run.io_bound(diffview.parse, text[:VIEW_CHARS])
    uid = f"sb-d{next(_DIFF_IDS)}"
    held = {}

    def drawn(mode: str) -> str:
        if mode != "split":
            return diffview.render(files, mode, uid)
        return (f'<div class="sb-diff-wide">{diffview.render(files, mode, uid)}</div>'
                f'<div class="sb-diff-narrow">{diffview.render(files, "unified", uid + "n")}</div>')

    async def pick(e) -> None:
        ui.context.client.storage[DIFF_COOKIE] = e.value
        ui.run_javascript(f'document.cookie = "{DIFF_COOKIE}={e.value}; path=/; '
                          f'max-age=31536000; samesite=lax"')
        if view := held.get("view"):
            view.set_content(await run.io_bound(drawn, e.value))

    shown = await run.io_bound(drawn, mode) if files else ""

    def heads() -> None:
        if files:
            ui.toggle(diffview.MODES, value=mode, on_change=pick) \
                .props("dense no-caps unelevated toggle-color=primary") \
                .classes("sb-diff-mode sb-seg").mark("diff-mode")
        if head:
            head()

    def fill() -> None:
        if lead:
            lead()
        if not files:
            ui.label("no changes").classes("sb-subtitle").mark("diff-none")
            return
        held["view"] = ui.html(shown, sanitize=False).classes("w-full").mark("diff-view")
        if len(text) > VIEW_CHARS:
            ui.label(f"cut at {VIEW_CHARS:,} characters").classes("sb-subtitle")
    viewer(title, fill, mono=mono, head=heads, wide=True)


LIST_COMMITS = 50


def commit_list(ctx, cfg, rows, total: int) -> None:
    """Commits to click, newest first: each sha a link to its page, its age,
    its subject on one line, and a button that opens it in the viewer."""
    with ui.element("div").classes("sb-commits").mark("commit-list"):
        for sha, date, subject in rows:
            ui.link(sha[:7], target_href("commit", sha)).classes("sb-commits-sha") \
                .mark("commit-row")
            ui.label(ago(date)).classes("sb-commits-age").tooltip(S.shown(date))
            ui.label(subject).classes("sb-commits-sub").tooltip(subject)
            view_button(ctx, sha, ttype="commit") or ui.element("span")
    if total > len(rows):
        ui.label(f"+{total - len(rows)} more").classes("sb-subtitle").mark("commit-more")


async def since_dialog(ctx, n):
    """A file entry's `N commits since`, opened: the commits, then the file's
    diff from what the entry saw to the file now, uncommitted edits
    included, which is what the count compares (gitref.changed_since).
    A coroutine, for the click's handler; git runs on a worker thread."""
    cfg, path = ctx.cfg.for_type(n.target.type), n.target.name

    def read():
        shas = gitref.changed_since(ctx.cfg, [n])[n.id] or []
        if not shas:
            return None
        base, exact = gitref.since_base(cfg, path, shas[-1], n.target_blob)
        return (shas, base, exact, gitref.commit_rows(cfg, shas, LIST_COMMITS, walk=False),
                gitref.diff_text(cfg, base, "--", f":(literal){path}"))
    got = await run.io_bound(read)
    if got is None:
        ui.notify(f"no commit has changed {n.target.name} since this entry")
        return
    shas, base, exact, rows, text = got
    saw = "as this entry saw it" if exact else \
        f"as of {base[:7]}, the nearest commit: the entry saw edits no commit holds"

    def lead() -> None:
        ui.label(f"changed in {summ._count(len(shas), 'commit')} since this entry, "
                 f"newest first").classes("sb-subtitle")
        commit_list(ctx, cfg, rows, len(shas))
        ui.label(f"{path}, {saw}, against the file now").classes("sb-subtitle") \
            .mark("since-base")

    async def was() -> None:
        text = await run.io_bound(gitref.blob_text, cfg, base, path)
        if text is None:
            ui.notify(f"cannot read {path} at {base[:7]}", type="warning")
            return

        def fill():
            if path.lower().endswith(".md"):
                ui.markdown(dark_mermaid(text[:VIEW_CHARS]), extras=FILE_MD_EXTRAS) \
                    .mark("file-view")
            else:
                NoteMarkdown(fenced(text[:VIEW_CHARS], path)).mark("file-view")
        viewer(f"{path}, {saw}", fill, mono=True)

    def head() -> None:
        b = ui.button(icon="history", on_click=was) \
            .props("flat dense round size=sm color=muted") \
            .tooltip(f"read {path} {saw}").mark("since-was")
        b._props["aria-label"] = f"read {path} {saw}"   # set, not parsed: a file name may hold a quote
    return await diff_dialog(f"{path} since this entry", text or "", lead=lead, head=head)


async def check_dialog(ctx, n, state: str, distance: int):
    """A check's `behind N` or `ahead N`, opened: the commits between its
    stamp and HEAD, and for `behind`, what they changed, the whole tree.
    A coroutine, for the click's handler; git runs on a worker thread."""
    sha = S.stamp_sha(n.provenance)
    def read():
        cfg = gitref.stamp_cfg(ctx.cfg, n.provenance)
        rows = gitref.commit_rows(cfg, [f"{sha}..HEAD" if state == "behind" else f"HEAD..{sha}"],
                                  LIST_COMMITS)
        return cfg, rows, gitref.diff_text(cfg, sha, "HEAD") if state == "behind" else None
    cfg, rows, text = await run.io_bound(read)

    def lead() -> None:
        ui.label(f"stamped at {sha[:7]}; " +
                 (f"HEAD is {summ._count(distance, 'commit')} past it, newest first"
                  if state == "behind" else
                  f"it is {summ._count(distance, 'commit')} past HEAD, newest first")) \
            .classes("sb-subtitle")
        commit_list(ctx, cfg, rows, distance)
    if state == "ahead":
        viewer(f"commits since HEAD, to {sha[:7]}", lead, mono=True)
        return
    return await diff_dialog(f"since this run, at {sha[:7]}", text or "", lead=lead)


def view_button(ctx, name, label: str = "", ttype=None):
    """`viewable`'s button, or None."""
    if (v := viewable(ctx, name, ttype)) is None:
        return None
    icon, tip, click = v
    b = ui.button(label, icon=icon) \
        .on("click", lambda e: click(bool((e.args or {}).get("shiftKey"))), ["shiftKey"]) \
        .props("flat dense no-caps color=muted" + ("" if label else " round size=xs")) \
        .tooltip(tip).mark("file-open")
    # An icon alone is named for what it reads; each was "read" (the GUI
    # review, 2026-11-07). Words on it are its name, as a person reads it.
    if not label:
        b._props["aria-label"] = tip            # set, not parsed: a file name may hold a quote
    return b


def _waits_on_you(n, author, rd: Reading) -> bool:
    """summary.asks for the person at this page, on their side's rows: the
    other side's question waits on that side's person."""
    return summ.on_side(rd.by_id, n, rd.side) and (n.supersedes is None
                                                   or summ.asks(author, rd.by_id, n))


class Card(Element, component="card.js"):
    """One entry's card, drawn in the browser from one prop (card.js): a
    card of server elements was about 32 of them, most of a page's cost
    (measured 2026-10-29). `sources` is the markdown its body pieces were
    made from: with the prop's words and tips, what `should_see` and the
    other content filters read, as they read the old card's elements."""

    def __init__(self, card: dict, sources: list) -> None:
        super().__init__()
        self._props["card"] = card
        self._sources = sources
        if any(p["k"] == "md" and "codehilite" in p["html"] for p in card["main"]):
            # ponytail: ui.markdown's own sheet by its private method; if it
            # moves, code in a body loses its colours (the name is its own)
            css = ui.markdown._generate_codehilite_css()
            self._props["resource-name"] = \
                f"codehilite_{hashlib.sha256(css.encode()).hexdigest()[:32]}.css"
            self.add_dynamic_resource(self._props["resource-name"],
                                      lambda: PlainTextResponse(css, media_type="text/css"))

    def _displayed_contents(self, *, only_visible: bool) -> list:
        c = self._props["card"]
        parts = [*c["main"], *c["foot"], *(v for f in c["foot"] if (v := f.get("view"))),
                 *(o for p in c["main"] for o in p.get("options", ())),
                 *(p for p in (c["by"], c["age"]) if p), c["sid"]]
        return [*self._sources, *(p.get(k) for p in parts for k in ("t", "tip", "html")),
                *(x for p in c["main"] for x in p.get("items", ()))]


def _head(url: str, kind: str, text: str, *marks) -> dict:
    """A bare row's headline: its target, or on that target's own page, its arc."""
    return {"k": "head", "m": ["note-headline", *marks], "h": url, "type": kind, "t": text}


def render_note(ctx, n, refresh, *, author: str, show_target: bool = False,
                base: dict | None = None, compact: bool = False, hit=(),
                actions: bool = True, git_head: str | None = None,
                since: int | None = None, seams=(), diff_to=None,
                seen: Reading | None = None) -> None:
    """One note row: a card. What it says first, then one line of where it
    points and what it is, the resolve ring on its left and star and edit
    over its corner. Every chip is a link into the filtered view -- that is
    the browse mechanism, not a decoration. `base` is the filtered view's own
    params: a chip there adds one to them, so a click narrows (the gui spec;
    it dropped them, 2026-09-25). Off a filtered view a chip starts one.
    `hit` is the search's words: a compact body then shows and marks them.
    `actions=False` drops the buttons: an earlier version is history, and an
    edit made from it would land on the current row. `git_head` is
    gitref.head_sha's, read once by a page of many check badges, and `since`
    the row's gitref.commits_since, counted once by a page of many cards.
    `seams` are summary.seams' for the body. `diff_to` is the row that
    superseded this one: the card says what it changed, marks the words of a
    changed checked or result, and shows a rewritten body as a diff. `seen` is
    the page's reading of the store, made once: a card makes its own when the
    page gives none. The card is one `Card`, drawn by card.js from the data
    built here (the user, 2026-10-29)."""
    def narrow(**kv):
        # A filter the view has already: its chip linked to the page it was on,
        # a click that did nothing (the GUI review, 2026-11-07). None: a label.
        if base and all(base.get(k) == v for k, v in kv.items()):
            return None
        return href(**{**(base or {}), **kv})

    status = S.read_status(n)
    has_pri = "priority" in n.tags
    # ponytail: a question is the kind named `question`, as in
    # summary.asks, whose newest word skips someone else's edit that
    # changed no text. A page hands its cards one reading of the store; a card
    # without it reads the store itself (a few ms at a few thousand rows).
    answering = actions and n.kind == "question" and status == "open" \
        and summ.wrote_it(author, n.author) and _waits_on_you(n, author, seen or reading(ctx))
    # No ring beside the answers: it closed the question and lost the pick (2026-10-24).
    resolvable = actions and n.spec.status and status == "open" and not answering
    cls = f"sb-note w-full {kind_class(n.kind)}"
    if actions:
        cls += " sb-has-actions" + (" sb-starred" if has_pri else "")
    main, sources = _card_main(ctx, n, compact, hit, show_target, seams, diff_to)
    if answering:
        tip = f"added below the question, as {author}"
        main.append({"k": "answer", "m": "answer-row", "options": [
            {"t": o, "m": "answer-option", "icon": "check" if i == 0 else None,
             "tip": f"“{o}”{', the default' if i == 0 else ''}, {tip}"}
            for i, o in enumerate(n.options or ())]})
    elif n.options:
        # One line, the CLI's: three phrases parted by gaps alone read as one run
        # of words in a browser shot (2026-10-17).
        main.append({"k": "options", "m": "options",
                     "t": f"options: {summ.options_line(n.options)}"})
    foot, hover, views = _card_foot(ctx, n, status, narrow, show_target, git_head, since)
    card = {"main": main, "foot": foot, "by": {"t": n.author, "h": narrow(author=n.author)},
            "age": {"t": ago(n.created_at), "tip": S.shown(n.created_at)},
            # The age takes no focus: a keyboard reads its stamp in the id's tip
            # (the GUI review, 2026-11-07), a screen reader in the age's own words.
            "sid": {"t": short_id(n.id), "h": href(id=n.id),
                    "tip": f"{n.id}\nwritten {S.shown(n.created_at)}\nopen this entry on its own page"},
            # On the right, after the text: on the left it pushed an open row's
            # text in past a closed row's (the user, 2026-10-01).
            "resolve": bool(resolvable), "star": has_pri if actions else None}
    # data-id: the page filter matches a pasted id the card does not print.
    el = Card(card, sources).classes(cls + (" sb-resolvable" if resolvable else "")) \
        .mark("note-row")
    el._props["data-id"] = n.id
    # the prop is NiceGUI's observable copy: a tip or a kept answer goes in it
    card = el._props["card"]
    foot = card["foot"]

    def resolve():
        if n.spec.verdict:
            # A pre-registration closes with its verdict: the store refuses a
            # bare resolve, so the button opens the dialog with the result
            # field rather than showing the refusal.
            edit_dialog(ctx, n, refresh, author=author, resolving=True)
            return
        if demo_says("resolves the entry"):
            return
        with refusal_shown():
            api.supersede(ctx, n.id, author=author, status="resolved")
            refresh()

    def answer(text) -> None:
        # The answer goes below the question, as the person; its asker acts
        # on it and resolves it (the options design, 2026-10-16).
        if not isinstance(text, str) or not (text := text.strip()):
            return
        if demo_says("adds your answer below the question"):
            return
        try:
            api.supersede(ctx, n.id, author=author, append_body=text)
        except ValueError as e:
            ui.notify(str(e), type="negative")
            return
        app.storage.tab.get(_ANSWERS, {}).pop(n.id, None)    # sent: not typed for its next turn
        refresh()

    def keep(text) -> None:
        # Words typed in an answer outlive the reload another card's write
        # brings (audit, 2026-10-28): kept per tab, put back once the page connects.
        # The read-only demo keeps none: a tab's storage lives 30 days (audit, 2026-11-08).
        if S.read_only():
            return
        kept = app.storage.tab.setdefault(_ANSWERS, {})
        if isinstance(text, str) and text:
            kept[n.id] = text
        else:
            kept.pop(n.id, None)

    def copy() -> None:
        ui.clipboard.write(n.id)
        ui.notify(f"copied {n.id}")

    def tip(i) -> None:
        # once per chip: a `git log` per badge at render time doubled the
        # cost of a board of checks
        if i in hover and not foot[i]["tip"]:
            foot[i]["tip"] = hover.pop(i)() or ""
            el.update()

    def act(e) -> None:
        # The browser names what was pressed; a name or index it should
        # not send, or one this card does not offer, does nothing.
        args = e.args if isinstance(e.args, list) else [e.args]
        what, rest = args[0] if args else None, args[1:]
        first = rest[0] if rest else None
        if what == "star" and actions:
            toggle_star(ctx, n, refresh, author=author)
        elif what == "edit" and actions:
            edit_dialog(ctx, n, refresh, author=author)
        elif what == "resolve" and resolvable:
            resolve()
        elif what == "copy":
            copy()
        elif what == "answer" and answering:
            answer(first)
        elif what == "typed" and answering:
            keep(first)
        elif what == "view" and isinstance(first, int) and first in views:
            return views[first](len(rest) > 1 and bool(rest[1]))   # a reveal is async
        elif what == "tip" and isinstance(first, int):
            tip(first)

    el.on("act", act)
    if answering:
        client = ui.context.client

        def back() -> None:
            with client:
                if text := app.storage.tab.get(_ANSWERS, {}).get(n.id):
                    card["typed"] = text
                    el.update()
        client.on_connect(back)


def toggle_star(ctx, n, refresh, *, author: str) -> None:
    has_pri = "priority" in n.tags
    if demo_says("unstars the entry" if has_pri else "stars the entry"):
        return
    with refusal_shown():
        api.retag(ctx, n.id, add=() if has_pri else ("priority",),
                  rm=("priority",) if has_pri else (), author=author)
        refresh()


def _card_main(ctx, n, compact: bool, hit, show_target: bool = False, seams=(),
               diff_to=None) -> list:
    """The body, the verdict and measurements: what the row says, and the
    markdown each body piece was made from. A bare row says its target, or
    on that target's own page, its arc."""
    main, sources = [], []

    def md(text: str, **part) -> None:
        sources.append(text := link_ids(text, ctx.store_dir))
        main.append({"k": "md", "html": _body_html(text), **part})

    if diff_to:
        what = changes(n, diff_to)
        main.append({"k": "changes", "m": "changes", "tip": diff_to.created_at,
                     "t": f"{diff_to.author} edited this {summ.when_added(diff_to.created_at)}"
                          + (f": {', '.join(what)}" if what else "")})
    if bare(n):
        if show_target:
            t = n.target
            main.append(_head(href(id=n.id), "", "project", "note-target")
                        if t.type == "project" or not t.name else
                        _head(target_href(t.type, t.name), t.type, t.name, "note-target"))
        elif n.arc_id:
            p = S.arcs_path(ctx.store_dir)
            st = p.stat() if p.exists() else None
            name = _arc_names(str(ctx.store_dir), st and (st.st_mtime_ns, st.st_size)) \
                .get(n.arc_id, n.arc_id)
            main.append(_head(f"/arc?id={quote(n.arc_id)}", "arc", name))
    if n.spec.verdict:
        # Clipped on a board like the body: a check's `checked` ran to
        # three lines and pushed the next row off the screen.
        fit = (lambda t: summ.clip(t, summ.HEAD_CHARS)) if compact else (lambda t: t)
        if diff_to and (n.checked, n.result) != (diff_to.checked, diff_to.result):
            main.append({"k": "verdict", "m": "note-verdict",
                         "html": f"checked: {word_diff(n.checked, diff_to.checked)}   →   "
                                 f"result: {word_diff(n.result, diff_to.result)}"})
        else:
            main.append({"k": "verdict", "m": "note-verdict",
                         "t": f"checked: {fit(n.checked) or '—'}   →   "
                              f"result: {fit(n.result) or '—'}"})
    if n.body and compact and (hit or len(summ.flatten(n.body)) > summ.BODY_CHARS):
        # A list to scan, not a page to read: one bug's four paragraphs
        # filled the boards (seen in the browser, 2026-09-27). A search
        # result is text too, even a short one, so every hit is marked.
        text = snippet(n.body, hit)
        part = {"k": "text", "m": [], "t": text}
        # A clipped body is the link to the whole note: a "full note" link
        # under each one said it again (the user, 2026-10-01).
        if text != summ.flatten(n.body):
            part["h"] = href(id=n.id)
            part["m"].append("note-full")
        if hit:
            part["html"] = marked(text, hit)
            part["m"].append("note-snippet")
        main.append(part)
    elif diff_to and edited(n.body, diff_to.body):
        for op, text in diff_blocks(n.body, diff_to.body):
            side = {"-": "del", "+": "ins"}.get(op)
            md(text, **({"c": f"sb-diff-{side}", "m": f"diff-{side}"} if side else {}))
    elif n.body:
        # Each piece on its own, a rule in the kind's colour between: an
        # append's text after its seam, a prepend's before.
        start = 0
        for at, who, when, above, rewrote in seams:
            md(n.body[start:at])
            main.append({"k": "seam", "m": "seam", "tip": when,
                         "t": f"{who} {'edited the text above and ' if rewrote else ''}added "
                              f"{'the above ' if above else ''}{summ.when_added(when)}"})
            start = at
        md(n.body[start:])   # sanitized in the browser, as ui.markdown: DOMPurify
    if n.measurements:
        items = [f"{k}: {v}" for k, v in n.measurements.items()]
        # A list's card names the first few; the entry's page names them all.
        # One card printed dozens, most of them 0 (the design critique, 2026-11-07).
        if compact and len(items) > MEAS_SHOWN:
            items = items[:MEAS_SHOWN] + [f"+{len(items) - MEAS_SHOWN} more"]
        main.append({"k": "meas", "m": "measurements", "items": items})
    return main, sources


def _card_foot(ctx, n, status, narrow, show_target: bool, git_head, since=None):
    """One wrapping line: what kind and state the row is and where it points
    on the left; who wrote it, when, and its id on the right (card.js).
    The chips, the tips each fills on its first hover, and the view buttons'
    clicks, both by the chip's index."""
    foot, hover, views = [], {}, {}

    def pair(chip: dict, name, ttype) -> dict:
        if ttype == "commit" and name and chip.get("t"):
            # on hover, as the since chip's: a git call per chip at render
            chip |= {"tip": "", "key": "commit"}
            hover[len(foot)] = lambda: commit_tip(ctx.cfg, name)
        if v := viewable(ctx, name, ttype):
            icon, tip, click = v
            chip["view"] = {"icon": icon, "tip": tip, "m": "file-open"}
            views[len(foot)] = click
        return chip

    if n.due:
        text, cls, tip = _due(n, status)
        foot.append({"t": text, "c": cls, "tip": tip, "m": "note-due"})
    foot.append({"t": n.kind, "h": narrow(kind=n.kind),
                 "c": f"sb-chip sb-kindchip {kind_class(n.kind)}", "m": "note-kind"})
    if status:
        foot.append({"t": status, "h": narrow(status=status), "c": f"sb-chip sb-chip-{status}"})
    if n.spec.verdict:
        chip, fill, opens = _check_chip(ctx, n, git_head)
        if fill:
            hover[len(foot)] = fill
        if opens:
            views[len(foot)] = lambda shift: opens()
        foot.append(chip)
    if show_target:
        t = n.target
        chip = {"pair": True}
        if not bare(n):             # else the headline says it
            chip |= {"t": "project", "h": href(id=n.id)} if t.type == "project" or not t.name \
                else {"t": f"{t.type}: {t.name}", "h": target_href(t.type, t.name)}
            chip |= {"c": "sb-chip sb-target", "m": "note-target"}
        foot.append(pair(chip, t.name, t.type))
    if since:
        # Neutral, not a warning: on a busy file nearly every row has one.
        hover[len(foot)] = lambda: since_tip(ctx.cfg, n) + f"\n{_CLICK['since']}"
        views[len(foot)] = lambda shift: since_dialog(ctx, n)
        foot.append({"t": f"{summ._count(since, 'commit')} since", "c": "sb-chip", "tip": "",
                     "key": "since", "m": "note-since", "open": True})
    if n.arc_id and (show_target or not bare(n)):
        foot.append({"t": f"arc: {n.arc_id}", "h": f"/arc?id={quote(n.arc_id)}",
                     "c": "sb-chip sb-target", "m": "note-arc"})
    for r in n.refs:
        foot.append(pair({"pair": True, "t": f"↗ {summ.ref_label(r)}", "m": "note-ref",
                          "h": target_href(r.type, r.name), "c": "sb-chip sb-ref"},
                         r.name, r.type))
    for t in n.tags:
        foot.append({"t": f"#{t}", "h": narrow(tag=t), "c": "sb-tag", "m": f"tag-{t}"})
    return foot, hover, views


def history(ctx, every, head, author: str, git_head=None) -> None:
    """The rows `head` superseded, newest first: the store keeps every
    version, and the GUI showed only the last. Read-only: an edit made from
    an old version lands on the current row."""
    by_id = {n.id: n for n in every}
    chain, cur = [], head
    while cur.supersedes in by_id and by_id[cur.supersedes] not in chain:
        cur = by_id[cur.supersedes]
        chain.append(cur)
    if not chain:
        return
    with ui.expansion(f"earlier versions ({len(chain)})").classes("w-full sb-card") \
            .mark("history"):
        for old, new in zip(chain, [head, *chain]):
            # with its target: a rename is a version that changed nothing else
            render_note(ctx, old, lambda: None, author=author, actions=False,
                        git_head=git_head, show_target=True, diff_to=new)


def peek_entry(ctx, note_id: str, author: str, refresh) -> None:
    """The entry as its own page shows it, for the peek beside a page: the
    current version of the chain `note_id` is in, where each amendment
    joined it, and its earlier versions. `refresh` runs after a write in it."""
    rd = reading(ctx)
    every = rd.loaded
    try:
        n = S.chain_tip(every, note_id)
    except (KeyError, ValueError):            # no such row, or a looped chain
        ui.label(f"no entry {note_id} in this store").classes("text-body").mark("peek-none")
        return
    git_head = gitref.head_sha(ctx.cfg)
    render_note(ctx, n, refresh, author=author, show_target=True, git_head=git_head,
                since=gitref.commits_since(ctx.cfg, [n], git_head).get(n.id),
                seams=summ.seams(rd.by_id, n), seen=rd)
    history(ctx, every, n, author, git_head)


_SAID = "sb-said"


def say_after_reload(text: str, failed: bool = False) -> None:
    """A notice for the page a reload brings: one made before the reload
    died with the page, so `committed …` never showed (2026-10-13). A
    `failed` one stays until dismissed, as a refusal does."""
    app.storage.tab[_SAID] = [text, failed]


_ANSWERS = "sb-answers"


def show_said() -> None:
    """Once the page connects, what the write before its reload said."""
    client = ui.context.client

    def _show():
        with client:
            if said := app.storage.tab.pop(_SAID, None):
                text, failed = said
                if failed:
                    ui.notify(text, type="negative", multi_line=True, close_button="dismiss",
                              timeout=0)
                else:
                    ui.notify(text, multi_line="\n" in text)
    client.on_connect(_show)


def undo_dialog(ctx, author: str) -> None:
    """The top bar's undo asks first, with what it changes: each row the
    last write added goes, and each row it changed gets its earlier version
    back (the user, 2026-10-13). It replaced a timed notice, which set a
    deadline the store does not have, and whose undo did nothing as it
    faded."""
    if demo_says("takes back the last write"):
        return
    try:
        u = api.undo(ctx, None, author=author, apply=False)
    except S.UndoRefused as e:
        ui.notify(str(e), type="warning", multi_line=True)
        return
    now = {r["id"]: S.note_from_dict(r, kinds=ctx.kinds) for r in u["rows"]}
    with ui.dialog() as dialog, ui.card().classes("bg-panel"):
        ui.label("undo your last write").classes("sb-stat-label")
        for i in u["removed"]:
            ui.label(f"removes {short_id(i)} · {summ.clip(now[i].body or '', 80)}")
        for i in u["changed"]:
            was = S.note_from_dict(u["before"][i], kinds=ctx.kinds)
            # a row the write took away, as a discard does, comes back whole
            what = (changes(now[i], was) or ["its earlier version"]) if i in now else ["comes back"]
            ui.label(f"{short_id(i)} · {', '.join(what)} · "
                     f"{summ.clip(was.body or '', 80)}")
        for a in u["arcs"]:
            ui.label(f"arc {a} as it was before")

        def _do():
            dialog.close()
            if demo_says("takes back the last write"):
                return
            try:
                done = api.undo(ctx, (u["removed"] + u["changed"] + u["arcs"] + [None])[0],
                                author=author)
            except S.UndoRefused as e:        # a write landed since the dialog opened
                ui.notify(str(e), type="warning", multi_line=True)
                return
            say_after_reload(f"undone: {S.undo_words(done)}")
            ui.navigate.reload()

        with ui.row():
            ui.button("undo", on_click=_do).props("unelevated color=primary") \
                .mark("undo-confirm")
            ui.button("cancel", on_click=dialog.close).props("flat")
    dialog.open()


def discard_dialog(ctx, author: str) -> None:
    """Commit's other way: throw away what it would take for you, after a list
    of what goes (api.discard). The user made an arc and tasks to test things,
    and only git could throw them away (2026-11-08). A discard is a write, so
    the undo arrow takes it back."""
    if demo_says("throws away your uncommitted work"):
        return
    plan = api.discard(ctx, author=author, apply=False)
    gone, back = S.entries(plan["removed"]), {r["id"] for r in plan["restored"]}
    to = {r["id"]: r.get("author") for r in plan["returned"]}
    kept = [*(f"{summ.clip(r.get('body') or '', 60)}: another writer's change builds on it"
              for r in S.entries(plan["kept"])),
            *(f"arc {i}: an entry that stays is filed in it" for i in plan["kept_arcs"]),
            *(f"arc {i}: changed since the last commit by a writer symbion does not record"
              for i in plan["arcs_changed"])]
    n = len(gone) + len(plan["arcs"])
    if not n:
        ui.notify("nothing of yours to discard" + (f"; kept: {'; '.join(kept)}" if kept else ""),
                  multi_line=True)
        return
    with ui.dialog().mark("discard-dialog") as dialog, ui.card().classes("bg-panel"):
        ui.label("discard your uncommitted work").classes("sb-stat-label")
        for r in gone:
            what = ("back to its committed version" if r["id"] in back
                    else f"back to {to[r['id']]}'s version" if r["id"] in to else "removed")
            ui.label(f"{short_id(r['id'])} · {what} · {summ.clip(r.get('body') or '', 80)}")
        for a in plan["arcs"]:
            ui.label(f"arc {a.get('name') or a.get('id')} · removed")
        for k in kept:
            ui.label(f"kept · {k}").classes("sb-note-meta")

        def _do():
            dialog.close()
            if demo_says("throws away your uncommitted work"):
                return
            try:
                api.discard(ctx, author=author, expect=S.discard_ids(plan))
            except S.DiscardChanged as e:
                ui.notify(str(e), type="warning", multi_line=True)
                return
            say_after_reload(f"discarded {summ._count(n, 'item')}: the undo arrow brings "
                             f"{'it' if n == 1 else 'them'} back")
            ui.navigate.reload()

        with ui.row():
            ui.button(f"discard {n}", on_click=_do).props("unelevated color=negative") \
                .mark("discard-confirm")
            ui.button("cancel", on_click=dialog.close).props("flat")
    dialog.open()


_EDITS = "sb-edit-drafts"       # app.storage.tab: an edit closed unsaved, by entry id


def edit_dialog(ctx, n, refresh, *, author: str, resolving: bool = False) -> None:
    """Supersede with edits. `resolving=True` is the resolve path for a
    status+verdict row: the result is required and the row closes on save."""
    tab = app.storage.tab           # read here: the close handler runs in no page slot
    kept = (tab.get(_EDITS) or {}).get(n.id)
    with ui.dialog().mark("edit-dialog") as dialog, ui.card().classes("bg-panel"):
        # A label, as every dialog's head is, and the sentence under it: set
        # as one uppercase label it shouted (the design critique, 2026-11-07)
        with ui.column().classes("gap-0"):
            ui.label("resolve" if resolving else "edit").classes("sb-stat-label")
            ui.label("The result is the verdict." if resolving else "Its earlier version is kept.") \
                .classes("sb-subtitle").mark("edit-what")
        # What the entry is on, picked as the new-entry form's header picks
        # it: the dialog could change neither (a tester, 2026-10-15). An
        # untouched target is not sent, so a departed one never resolves again.
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("on").classes("sb-stat-label")
            commits = commit_names(ctx)            # once: each picker read it, 70 ms each
            on_type, on_name = target_picker(ctx, n.target.type, n.target.name,
                                             marks=("edit-on-type", "edit-on-name"),
                                             commits=commits)
            refs = refs_field(ctx, n.refs, commits).classes("col") \
                .style("min-width:160px").mark("edit-refs")
        body = ui.textarea("body", value=n.body).props("outlined") \
            .classes("w-full min-w-[420px]").mark("edit-body")
        # Most edits add to a row, and the body's end was a scroll away (the
        # user, 2026-10-01). A resolve's field is the result: no focus here.
        more = ui.textarea("add after the body").props(
            "outlined autogrow" + ("" if resolving else " autofocus")) \
            .classes("w-full").mark("edit-append")
        # A click outside, or Esc, closed the dialog and dropped what was
        # typed (the GUI review, 2026-11-07). It is kept per tab and entry,
        # as the composer keeps its draft, until a save or a discard.
        stale = bool(kept) and "base" in kept and kept["base"] != n.body
        with ui.row().classes("items-center gap-1 -mt-2").mark("edit-kept") as kept_row:
            ui.label("kept from your last try" + (
                "; the body has changed since, so a save refuses your edit of it: "
                "copy it, then discard" if stale else "")).classes("text-muted text-xs")
            ui.button("discard", on_click=lambda: discard(), color=None) \
                .props("flat dense no-caps size=sm").classes("text-body").mark("edit-discard")
        kept_row.set_visibility(bool(kept))
        # The star beside the tags, so an edit and a star are one write, not
        # two (the user, 2026-10-02). The switch holds `priority`; the field
        # the rest, and a `priority` typed there still counts. So with
        # closing: an open status row resolves here, with its message (the
        # user, 2026-10-13), and a row with no status retires, which only a
        # typed tag reached (the user, 2026-10-14).
        held = {"priority"} | ({"retired"} if not n.spec.status else set())
        with ui.row(wrap=False).classes("items-center gap-2 w-full"):
            tags = tags_field(ctx, ", ".join(t for t in n.tags if t not in held)) \
                .classes("col").mark("edit-tags")
            pri = ui.switch("priority", value="priority" in n.tags).mark("edit-priority")
            close = None
            if not n.spec.status:
                close = ui.switch("retired", value="retired" in n.tags).mark("edit-retired")
            elif S.read_status(n) == "open" and not resolving:
                close = ui.switch("resolve").mark("edit-resolve")
            elif S.read_status(n) == "resolved" and not n.spec.verdict:
                # Only an arc's box reopened, and undo reaches the last write
                # alone (the GUI review, 2026-11-07). A verdict closes with its
                # result: a prediction reopened would hold one.
                close = ui.switch("reopen").mark("edit-reopen")
        # Attaching an EXISTING note to an arc has no other route: seeding
        # mints new tasks, and target/provenance are the only two fields
        # supersede refuses. This is the one.
        arcs = arc_options(ctx)
        if n.arc_id and n.arc_id not in arcs:   # archived or gone: a value no option holds raised
            arcs[n.arc_id] = next((f"{a.name} (archived)" for a in S.load_arcs(ctx.store_dir)
                                   if a.id == n.arc_id), n.arc_id)
        arc = ui.select(arcs, value=n.arc_id or "",
                             label="arc").props("dense outlined") \
            .classes("w-full").mark("edit-arc")
        # The kinds the store lets this entry move to, and what the move
        # drops, named before the save (a user, 2026-11-05): only the CLI
        # could change a kind.
        movable = [k for k, s in ctx.kinds.items() if s.verdict == n.spec.verdict
                   and (not s.verdict or s.status == n.spec.status)]
        kind = None
        if len(movable) > 1:
            kind = ui.select(movable, value=n.kind, label="kind") \
                .props("dense outlined").classes("w-full").mark("edit-kind")

            def drops(k):
                lost = api.kind_losses(n, k, ctx.kinds[k])
                return f"the move drops {', '.join(lost)}; its earlier version keeps them" \
                    if lost else ""
            ui.label().classes("text-muted text-xs").mark("edit-kind-drops") \
                .bind_text_from(kind, "value", drops) \
                .bind_visibility_from(kind, "value", lambda k: bool(drops(k)))  # no empty gap
        # A verdict is the part `list` prints and queries, so a typo in it is
        # the one thing the body edit above cannot reach. Rendered only on a
        # verdict kind: an edit never changes the verdict bit, so this needs no
        # visibility binding the way add_form's kind select does.
        # Text, not a native date input: that cannot hold a datetime, so it
        # would render empty and the next save would clear the date.
        due = None
        if n.spec.status:
            due = ui.input("due", value=n.due or "",
                           placeholder="YYYY-MM-DD or 2026-10-01T22:30Z") \
                .props("dense outlined").classes("w-full").mark("edit-due")
            date_menu(due, "edit-due-pick")
        checked = res = None
        if n.spec.verdict:
            checked = ui.input("checked", value=n.checked or "") \
                .props("dense outlined").classes("w-full").mark("edit-checked")
            res = ui.input("result", value=n.result or "") \
                .props("dense outlined").classes("w-full").mark("edit-result")

        texts = {k: f for k, f in (("body", body), ("more", more), ("checked", checked),
                                   ("result", res)) if f is not None}
        opened = {k: f.value for k, f in texts.items()}
        # The body a kept edit of it started from: a save checks the entry
        # against it, so a body changed since is refused, never written over.
        base = [n.body]
        if kept:
            for k, v in kept["fields"].items():
                if k in texts:
                    texts[k].set_value(v)
            base[0] = kept.get("base", n.body)
        saved = []

        def forget():
            d = dict(tab.get(_EDITS) or {})
            if d.pop(n.id, None) is not None:
                tab[_EDITS] = d

        def discard():
            for k, f in texts.items():
                f.set_value(opened[k])
            base[0] = n.body
            kept_row.set_visibility(False)
            forget()

        def closed(e):
            if e.value:
                return
            forget()
            now = {k: f.value for k, f in texts.items() if f.value != opened[k]}
            if now and not saved and not S.read_only():     # the demo keeps none, as above
                d = dict(tab.get(_EDITS) or {})
                d[n.id] = {"fields": now} | ({"base": base[0]} if "body" in now else {})
                tab[_EDITS] = d
            dialog.delete()             # one per click: each closed one stayed in the page
        dialog.on_value_change(closed)

        def _save():
            reopen = S.read_status(n) == "resolved" and close is not None and close.value
            resolve = resolving or bool(n.spec.status and close is not None and close.value
                                        and not reopen)
            if resolve and n.spec.verdict and not (res.value or "").strip():
                ui.notify("a result is required to resolve this kind", type="warning")
                return
            target = picked_target(on_type, on_name)
            if target is None:
                return
            # What the person changed, onto the entry as it is now: the dialog
            # wrote its own copies, read when it opened, so a star given here
            # took back an agent's addition and tag made meanwhile (a review,
            # 2026-10-16). Only a body both changed cannot merge.
            # ponytail: the tip is read outside the lock, as `retag` reads it.
            tip = S.chain_tip(S.load(ctx.store_dir), n.id)
            edited = body.value != base[0]
            if edited and tip.body != base[0]:
                ui.notify(f"{short_id(n.id)} changed since this dialog opened ({tip.author} "
                          f"wrote to it): copy your text, then reopen the entry to edit its "
                          f"newest version", type="warning", multi_line=True)
                return
            on = {"target": target} if (target["type"], target["name"]) \
                != (n.target.type, n.target.name) else {}
            mine = list(refs.value or ())
            if mine != [ref_key(r) for r in n.refs]:
                was = {ref_key(r) for r in n.refs}
                on["refs"] = [k for k in map(ref_key, tip.refs) if k in mine or k not in was] + \
                    [k for k in mine if k not in was and k not in map(ref_key, tip.refs)]
            # Only a #tag typed here: a CLI body keeps its #words, and a
            # harvest of the whole body rewrote them on every save.
            harvested, cleaned = api.harvest_hashtags(
                body.value or "", known=api.harvest_hashtags(n.body)[0]) \
                if edited else ([], body.value)
            more_tags, added = api.harvest_hashtags(more.value or "")
            if not added.strip():
                text = {"body": cleaned} if edited else {}
            elif not edited:
                # After the CURRENT row's body, read under the lock, as
                # `supersede --append` does: a revision made since this
                # dialog opened is kept.
                text = {"append_body": added}
            else:
                text = {"body": S.appended(cleaned, added)}
            verdict = {k: v for k, v, was in (("checked", checked.value or None, n.checked),
                                               ("result", res.value or None, n.result))
                       if v != was} if checked is not None else {}
            closing = {"status": "resolved"} if resolve else {"status": "open"} if reopen else {}
            retired = ["retired"] if not n.spec.status and close.value else []
            due_v = (due.value or "").strip() or None if due is not None else n.due
            dated = {"due": due_v} if due_v != n.due else {}
            final = list(dict.fromkeys(split_tags(tags.value) + harvested + more_tags
                                       + (["priority"] if pri.value else []) + retired))
            gone = [t for t in n.tags if t not in final]
            kept = [t for t in tip.tags if t not in gone]
            arc_v = arc.value or None
            kinded = {"kind": kind.value} if kind is not None and kind.value != n.kind else {}
            if kinded and n.options and kind.value != "question":
                kinded["options"] = None            # named under the select
            if demo_says("saves the edit"):
                return
            from .chrome import own_write       # chrome imports this module
            try:
                with own_write(ctx, author, [n.id]):
                    api.supersede(ctx, n.id, author=author, **text,
                                  tags=kept + [t for t in final if t not in n.tags and t not in kept],
                                  # unchanged, the tip's, unchecked: an entry in a gone arc saves
                                  **({"arc_id": arc_v} if arc_v != n.arc_id else {}),
                                  **verdict, **closing, **dated, **on, **kinded)
            except (ValueError, catalog.CatalogError) as e:    # a closed catalog's refusal
                ui.notify(str(e), type="negative")
                return
            saved.append(True)
            client = ui.context.client      # the close deletes the dialog, and this handler's slot
            dialog.close()
            with client:
                refresh()

        for field in (body, more):
            field.on("keydown.meta.enter", _save)
            field.on("keydown.ctrl.enter", _save)
        with ui.row():
            ui.button("save", on_click=_save).props("unelevated color=primary") \
                .mark("edit-save")
            ui.button("cancel", on_click=dialog.close).props("flat")
    dialog.open()


def split_tags(value: str) -> list:
    """A tags field is comma- or space-separated, with or without leading
    hashes -- people type it every way and all of them mean the same list."""
    return [p.lstrip("#") for p in re.split(r"[,\s]+", value or "") if p.strip("#, \t")]


def tidy_tags(field) -> None:
    """On blur, the field shows the list it will store: a `#` typed there
    stayed on screen though the save dropped it (the user, 2026-10-01).
    A `#` key never lands at all: until the blur, a person could not tell
    whether it would be part of the tag (the user, again)."""
    field.on("blur", lambda: field.set_value(", ".join(split_tags(field.value))))
    field.on("keydown", js_handler="(e) => { if (e.key === '#') e.preventDefault(); }")


def arc_options(ctx) -> dict:
    """{id: label} for a select, unarchived only. An archived arc is a
    closed ticket; offering it invites reopening one by accident."""
    opts = {"": "— none —"}
    for a in S.load_arcs(ctx.store_dir):
        if not a.archived:
            opts[a.id] = a.name
    return opts


class Suggest(ValueElement, component="suggest.js"):
    """Free text with `options` offered as you type (suggest.js). The value
    is what was typed, or the option picked. With `tokens` the text is a
    list, and the menu completes its last word."""
    VALUE_PROP = "value"            # as NiceGUI's input: see suggest.js
    LOOPBACK = False                # the browser holds the text; no echo

    def __init__(self, value: str, options, *, tokens: bool = False) -> None:
        super().__init__(value=value, on_value_change=None)
        self._props["tokens"] = tokens
        self._offer(options)

    def set_options(self, options) -> None:
        self._offer(options)
        self.update()

    def _offer(self, options) -> None:
        """`options`: a list of names, or a dict of each name's description,
        which the menu shows under the name and matches as you type."""
        self._props["options"] = list(options)
        self._props["captions"] = {k: v for k, v in options.items() if v} \
            if isinstance(options, dict) else {}


def tags_field(ctx, value: str = ""):
    """A tags field that offers the store's tags as a word is typed, the
    most used first (the user, 2026-10-01)."""
    counts = S.tag_counts(S.load(ctx.store_dir))
    field = Suggest(value, sorted(counts, key=lambda t: (-counts[t], t)), tokens=True) \
        .props("dense outlined options-dense label=tags")
    tidy_tags(field)
    return field


def date_menu(field, marker: str) -> None:
    """A calendar on a due field, dates only (the user, 2026-10-12). The
    field stays text, so a typed datetime still goes in. A picked date
    replaces the field's value. The calendar shows the field's date part,
    and that date is never written back, so opening it keeps a time."""
    with field.add_slot("append"), ui.icon("event").classes("cursor-pointer"), \
            ui.menu() as menu:
        def _pick(e):
            if e.value and e.value != (field.value or "")[:10]:
                field.value = e.value
                menu.close()
        ui.date(on_change=_pick).props("minimal") \
            .bind_value_from(field, "value", backward=lambda v: (v or "")[:10]).mark(marker)


# The commit picker's head: a commit no row names yet is still pickable by
# its subject (the user, 2026-10-16).
RECENT_COMMITS = 30


def commit_names(ctx) -> dict:
    """Each commit a picker offers, with its subject: the newest
    RECENT_COMMITS on HEAD, then the commits this store's rows name, newest
    row first, each once. A name git does not know has None; a commit with
    an empty message, ''."""
    out = gitref.recent(ctx.cfg, RECENT_COMMITS)
    used = [o.name for o in used_objects(ctx) if o.type == "commit" and o.name not in out]
    subj = gitref.subjects(ctx.cfg, used)
    return out | {s: subj.get(s) for s in used}


def target_names(ctx, typ: str, commits=None) -> dict:
    """What the name field offers for `typ`, each name with its description
    ('' or None for none): a catalog's names; for `commit`, `commit_names`
    (or `commits`, a form's one read of it), a known commit as its short sha,
    which the write peels; else the names this store's rows have used,
    newest first. A catalog that fails offers the store's names: the write
    reports its error."""
    # ponytail: runs the catalog on the event loop, as api.add does at every
    # GUI write: 22 ms for `git ls-files`, 170 ms for the slowest adopter
    # catalog (2026-10-02). If one is slow, run both under `run.io_bound`.
    # ponytail: two offered commits that share 7 characters collide, and one
    # leaves the menu (rare: 7 hex characters are 28 bits); cut each at its
    # shortest unique length if one is seen.
    if typ == "commit":
        known = commit_names(ctx) if commits is None else commits
        return {(s[:7] if d is not None else s): d for s, d in known.items()}
    if typ in ctx.cfg.catalogs:
        try:
            return catalog.described(ctx.cfg, typ)
        except catalog.CatalogError:
            pass
    return dict.fromkeys(o.name for o in used_objects(ctx) if o.type == typ)


def used_objects(ctx) -> list:
    """The objects this store's entries are on, as target or ref, newest
    first, once each."""
    seen: dict = {}
    for n in S.newest_first(S.load(ctx.store_dir)):
        for o in (n.target, *n.refs):
            if o.name:
                seen.setdefault((o.type, o.name), o)
    return list(seen.values())


def target_picker(ctx, typ: str, name: str | None, marks=("note-on-type", "note-on-name"),
                  commits=None):
    """A target's type select and name field, the name hidden on `project`:
    the new-entry form's header and the edit dialog's. Not an arc: the arc
    select puts an entry in one, unless the entry is already on one."""
    types = ["project", *sorted(ctx.target_types - {"project", "arc"})]
    if typ not in types:
        types.append(typ)
    on_type = ui.select(types, value=typ) \
        .props('dense outlined options-dense aria-label="target type"').mark(marks[0])
    # The name in its own case: a label's capitals rewrote a path. Any
    # part of a name matches, and a new one is typed freely.
    offer = lambda t: target_names(ctx, t, commits) if t != "project" else []   # noqa: E731
    on_name = Suggest(name or "", offer(typ)) \
        .props('dense outlined options-dense placeholder=name aria-label="target name"') \
        .classes("col").style("min-width:160px").mark(marks[1])
    on_name.bind_visibility_from(on_type, "value", backward=lambda v: v != "project")
    on_type.on_value_change(lambda e: on_name.set_options(offer(e.value)))
    return on_type, on_name


def picked_target(on_type, on_name) -> dict | None:
    """The target a picker holds, or None, said, when its name is empty."""
    if on_type.value == "project":
        return {"type": "project", "name": None}
    name = (on_name.value or "").strip()
    if not name:
        ui.notify(f"name the {on_type.value} this entry is on", type="warning")
        return None
    return {"type": on_type.value, "name": name}


def refs_field(ctx, refs=(), commits=None):
    """`also on`: the objects an entry is on beside its target, its refs, as
    chips. It offers the objects this store's entries are on, and takes a
    new TYPE:NAME typed: the GUI put an entry on one object (a tester,
    2026-10-15). It offers the newest commits too, as the name field does.
    A value is the name as stored, so a kept ref resolves to itself; the
    chip shows a commit's short sha. A commit's label carries its subject
    after a newline: the menu shows it under the ref, and typing matches it;
    a chip shows the ref alone (`option-label`)."""
    # ponytail: a changed field sends every ref, as `supersede --ref` does,
    # so a kept ref whose name left its catalog resolves again; send only
    # the change if that bites.
    # ponytail: no catalog's descriptions here: the field offers every type,
    # and describing them runs each catalog at each open (113 to 244 ms for
    # one that runs Python, jq or symbion, 2026-10-16). Run them off the
    # event loop if a store wants them.
    commits = commit_names(ctx) if commits is None else commits
    options = {}
    for o in (*refs, *used_objects(ctx), *(S.Target("commit", s) for s in commits)):
        d = commits.get(o.name, "") if o.type == "commit" else ""
        options.setdefault(ref_key(o), summ.ref_label(o) + (f"\n{d}" if d else ""))
    # sb-pick-menu: a paragraph-long subject stretched the menu to the
    # window's edge.
    field = ui.select(options, value=[ref_key(r) for r in refs], multiple=True,
                      new_value_mode="add-unique", label="also on") \
        .props("dense outlined options-dense use-chips popup-content-class=sb-pick-menu")
    field._props[":option-label"] = "o => String(o.label).split('\\n')[0]"
    field.add_slot("option", r"""
      <q-item v-bind="props.itemProps">
        <q-item-section>
          <q-item-label>{{ String(props.opt.label).split('\n')[0] }}</q-item-label>
          <q-item-label v-if="String(props.opt.label).includes('\n')" caption lines="1">
            {{ String(props.opt.label).split('\n')[1] }}</q-item-label>
        </q-item-section>
      </q-item>""")
    return field


def ref_key(o) -> str:
    """An object as `refs_field` holds it: TYPE:NAME, the name as stored."""
    return f"{o.type}:{o.name or ''}"


def add_form(ctx, target_type, target_name, refresh, *, author: str,
             arc_id: str | None = None, another=None,
             start_kind: str | None = None, close=None) -> SimpleNamespace:
    """Writes go through api.add, which canonicalizes the target name, stamps
    provenance on checks, and takes author keyword-only. `arc_id` is the arc
    the form starts on. With `another`, Shift Enter adds the note and keeps
    the form, cleared, for the next one, and calls `another` in place of
    `refresh`. The target starts on (target_type, target_name), and the
    header changes it: the GUI put a note only on the project or on the page's
    object (the user, 2026-10-01). Not an arc: the arc select puts a note in one.
    `start_kind` is the kind it starts as, when this store has it. `close`
    adds a close button. It returns a handle a dropped file drives
    (`chrome._dropped`)."""
    kinds = ctx.kinds
    labels = list(kinds)
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label("new entry on").classes("sb-stat-label")
        commits = commit_names(ctx)                # once: each picker read it, 70 ms each
        on_type, on_name = target_picker(ctx, target_type, target_name, commits=commits)
        refs = refs_field(ctx, commits=commits).classes("col").style("min-width:160px") \
            .mark("note-refs")
        if close:                       # Esc alone stranded a visitor with no key to press (2026-11-09)
            ui.button(icon="close", on_click=close) \
                .props('flat dense round size=sm aria-label="close"').mark("composer-close")
        ui.space()
        ui.label("⌘/Ctrl Enter adds it" + (" · Shift Enter adds another" if another else "")
                 + " · Esc closes").classes("sb-subtitle")
    # The body first, then one toolbar: what the note says is the field a
    # person came to type in, and the rest are settings on it. Outlined like
    # the fields around it: borderless, one typed space hid the placeholder
    # and left no edge to the field (a tester, 2026-10-15).
    with ui.element("div").classes("sb-composer"):
        body = ui.textarea(placeholder="Write an entry in markdown — #tag adds a tag, "
                                       "!task sets the kind") \
            .props('outlined autogrow autofocus aria-label="body"') \
            .classes("w-full sb-compose-body mb-2").mark("note-body")
        with ui.row().classes("items-center gap-2 w-full"):
            start = start_kind if start_kind in labels else "note" if "note" in labels \
                else labels[0]
            kind = ui.select(labels, value=start) \
                .props('dense outlined options-dense aria-label="kind"') \
                .mark("note-kind-select")
            opts = arc_options(ctx)
            arc = ui.select(opts, value=arc_id if arc_id in opts else "", label="arc") \
                .props("dense outlined options-dense").classes("min-w-[150px]") \
                .mark("note-arc-select")
            checked = ui.input("checked").props("dense outlined") \
                .mark("note-checked")
            result = ui.input("result").props("dense outlined") \
                .mark("note-result")
            checked.bind_visibility_from(kind, "value", backward=lambda v: kinds[v].verdict)
            result.bind_visibility_from(kind, "value", backward=lambda v: kinds[v].verdict)
            due = ui.input("due", placeholder="YYYY-MM-DD").props("dense outlined") \
                .classes("w-[130px]").mark("note-due")
            date_menu(due, "note-due-pick")
            due.bind_visibility_from(kind, "value", backward=lambda v: kinds[v].status)
            tags = tags_field(ctx).classes("col sb-compose-tags") \
                .style("min-width:120px").mark("note-tags")
            add = ui.button().props("unelevated color=primary").mark("note-add")
            add.bind_text_from(kind, "value", backward=lambda v: f"Add {v}")

        # Harvested tags are ADDED to the field, never silently replace what was
        # typed there; `seen` is what the last harvest contributed, so a tag the
        # author deleted by hand does not reappear on the next keystroke.
        seen: list = []

        def _sync():
            named, _ = api.harvest_kind(body.value or "", kinds)
            if named:
                kind.value = named
            found, _ = api.harvest_hashtags(body.value or "")
            kept = [x for x in split_tags(tags.value) if x not in seen or x in found]
            tags.value = ", ".join(dict.fromkeys(found + kept))
            seen[:] = found

        body.on_value_change(lambda _: _sync())

        def _submit(next_one=False):
            named, cleaned = api.harvest_kind(body.value or "", kinds)
            kind.value = named or kind.value
            harvested, cleaned = api.harvest_hashtags(cleaned)
            if not cleaned and not kinds[kind.value].verdict:   # `!task` alone says nothing
                ui.notify("empty entry", type="warning")
                return
            target = picked_target(on_type, on_name)
            if target is None:
                return
            row = {"kind": kind.value,
                   "target": target,
                   "refs": list(refs.value or ()),
                   "body": cleaned,
                   "arc_id": arc.value or None,
                   "tags": list(dict.fromkeys(split_tags(tags.value) + harvested))}
            if kinds[kind.value].verdict:
                row["checked"] = checked.value or None
                row["result"] = result.value or None
            if kinds[kind.value].status:
                row["due"] = (due.value or "").strip() or None
            if demo_says("adds the entry"):
                return
            try:
                added = api.add(ctx, row, author=author)
            except (ValueError, catalog.CatalogError) as e:  # ambiguous, closed, bad ref type
                ui.notify(str(e), type="negative")
                return
            body.value = checked.value = result.value = tags.value = due.value = ""
            refs.value = []
            seen.clear()
            if next_one:
                ui.notify(f"added {short_id(added.id)}; write the next", type="positive")
                another()
            else:
                refresh()

        add.on_click(_submit)
        body.on("keydown.meta.enter", _submit)
        body.on("keydown.ctrl.enter", _submit)
        if another:
            # `prevent` last: Vue runs the guards in order, and before
            # `shift` it would take the newline from a plain Enter too.
            body.on("keydown.shift.enter.exact.prevent", lambda: _submit(next_one=True))

    def _set_target(typ, name):
        on_type.value = typ             # its change handler offers that type's names
        on_name.value = name or ""

    def _insert(text, cursor=None, block=False):
        v = body.value or ""
        if cursor is None:              # after a draft: a paragraph of its own
            start = end = len(v)
            text = ("\n\n" if v and not v.endswith("\n") else "") + text
        else:
            start, end = cursor
            if block:                   # a fence opens a code block only at a line's start
                text = (("\n" if start and v[start - 1] != "\n" else "") + text
                        + ("\n" if end < len(v) and v[end] != "\n" else ""))
        body.value = v[:start] + text + v[end:]

    def _add_ref(key):
        if key not in (refs.value or []):
            refs.set_options({**refs.options, key: key}, value=[*(refs.value or []), key])

    # The body first: its harvest sets the kind and tags, which the draft's own then replace.
    fields = {"body": body, "kind": kind, "arc": arc, "checked": checked, "result": result,
              "due": due, "tags": tags}

    def _state() -> dict:
        return {**{k: f.value for k, f in fields.items()},
                "target": [on_type.value, on_name.value], "refs": list(refs.value or ())}

    def _restore(d: dict) -> None:
        _set_target(*d["target"])
        for k, f in fields.items():
            with suppress(ValueError):   # a kind or arc gone since: the rest still comes back
                f.value = d[k]
        for key in d["refs"]:
            _add_ref(key)

    return SimpleNamespace(busy=lambda: bool((body.value or "").strip() or refs.value),
                           set_target=_set_target, insert=_insert, add_ref=_add_ref,
                           state=_state, restore=_restore)
