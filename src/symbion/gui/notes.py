"""The note row, reused by every surface. Colors are CSS classes from theme.py
-- no hexes here, the hex audit forbids them outside theme.py.

Refreshables are defined PER call (per page/client). A module-level
@ui.refreshable accumulates instances across pages and its .refresh() then
touches a deleted client (RuntimeError 'client has been deleted').
"""
from __future__ import annotations

import functools
import html
import itertools
import os
import re
import subprocess
import sys
from contextlib import contextmanager, nullcontext, suppress
from datetime import datetime
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import NamedTuple
from urllib.parse import quote

from markdown_it.token import Token
from nicegui import app, run, ui
from nicegui.elements.mixins.value_element import ValueElement
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

from .. import api, catalog, gitref, term
from .. import summary as summ
from .. import store as S
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


_BODY_MD = term.body_parser(_highlight)
_BODY_MD.core.ruler.push("sb_linkify", _linkify)


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


def link_ids(body: str, store_dir) -> str:
    """Each full note id in `body`, and each tail of a row in this store, as
    a markdown link to that note, except inside code, where a link would
    print as literal brackets. A tail of no row here stays text: rows cite
    other stores' rows too. A link to a resolved entry is struck through,
    as a done item is, and its title says so (the user, 2026-10-16)."""
    def link(m):
        nid = m[1] if len(m[1]) > 17 else tails().get(m[1])
        if not nid:
            return m[1]
        if nid in _closed(str(store_dir), stamp()):
            return f'[~~{m[1]}~~](/notes?id={nid} "resolved")'
        return f"[{m[1]}](/notes?id={nid})"

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


def target_link(n) -> None:
    """Where this note points. Arcs go to their checklist; everything
    else to the ref-aware object view."""
    if n.target.type == "project" or not n.target.name:
        # `project` has no object page -- /object redirects when name is empty --
        # so this is the one row whose target cannot stand in for the note. It
        # links to the note itself, which is also the only way into a project
        # note filed under an arc: the checklist shows nothing else.
        ui.link("project", href(id=n.id)).classes("sb-chip sb-target") \
            .mark("note-target")
        return
    ui.link(f"{n.target.type}: {n.target.name}", target_href(n.target.type, n.target.name)) \
        .classes("sb-chip sb-target").mark("note-target")


def bare(n) -> bool:
    """A row with nothing to say, no body and no verdict: a seeded box. What
    it targets is its whole content, so that leads instead of a blank line."""
    return not n.body.strip() and not n.spec.verdict


@functools.lru_cache(maxsize=8)
def _arc_names(store_dir: str, _stamp) -> dict:
    """Arc id -> name. `_stamp` is arcs.jsonl's mtime and size, as `_tails`'s."""
    return {a.id: a.name for a in S.load_arcs(store_dir)}


def _headline(url: str, kind: str, text: str, *marks) -> None:
    with ui.link(target=url).classes("sb-note-open").mark("note-headline", *marks):
        ui.html(f'<span class="sb-headline-type">{html.escape(kind)}</span>'
                f'{html.escape(text)}', sanitize=False).classes("sb-note-text")


def target_headline(n) -> None:
    """A bare row's target as its text: the name bright, the type dim before
    it. It stands in for `target_link`'s chip, so it carries that marker."""
    if n.target.type == "project" or not n.target.name:
        _headline(href(id=n.id), "", "project", "note-target")
    else:
        _headline(target_href(n.target.type, n.target.name), n.target.type,
                  n.target.name, "note-target")


def arc_headline(ctx, n) -> None:
    """A bare row on its own object's page: the target is the page, so the
    arc is what is left to say, by its name."""
    p = S.arcs_path(ctx.store_dir)
    st = p.stat() if p.exists() else None
    name = _arc_names(str(ctx.store_dir), st and (st.st_mtime_ns, st.st_size)) \
        .get(n.arc_id, n.arc_id)
    _headline(f"/arc?id={quote(n.arc_id)}", "arc", name)


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


def _check_badge(ctx, n, git_head=None) -> None:
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
    chip = ui.label(text).classes(f"sb-chip {cls}").mark("check-state")
    if fixed_tip:
        with chip:
            ui.tooltip(fixed_tip)
        return
    if not S.stamp_sha(n.provenance) or state == "pending":
        return
    # Filled on first hover: a `git log` per badge at render time doubled
    # the cost of a board of checks (45 badges: 1.5s to 2.9s).
    with chip:
        tip = ui.tooltip("")

    def _fill():
        if not tip.text:
            tip.text = check_tip(ctx.cfg, n.provenance, state, distance)

    chip.on("mouseenter", _fill)


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


def _since_chip(ctx, n, since: int) -> None:
    """Neutral, not a warning: on a busy file nearly every row has one."""
    chip = ui.label(f"{summ._count(since, 'commit')} since").classes("sb-chip") \
        .mark("note-since")
    with chip:
        tip = ui.tooltip("")

    def _fill():                    # on hover, as the check badge's: a walk per card
        if not tip.text:
            tip.text = since_tip(ctx.cfg, n)

    chip.on("mouseenter", _fill)


def due_chip(n, status) -> None:
    """Words while the row can still fire, the bare date once it cannot: a
    resolved row is never late."""
    state = S.due_state(n.due) if status == "open" else None
    if state is None:
        ui.label(f"due {S.shown(n.due)}").classes("sb-chip").mark("note-due")
        return
    past, days = state
    cls = "sb-chip-bad" if past else "sb-chip-notable" if days <= summ.DUE_DAYS else ""
    ui.label(summ.due_phrase(state)).classes(f"sb-chip {cls}").tooltip(S.shown(n.due)) \
        .mark("note-due")


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


def id_link(n) -> None:
    """The id's tail, a link to the note alone, and a button that copies the
    whole id: an agent is told about a row by pasting its id."""
    ui.link(short_id(n.id), href(id=n.id)).classes("sb-note-meta sb-id") \
        .mark("note-id").tooltip(f"{n.id}\nthis entry alone")

    def _copy():
        ui.clipboard.write(n.id)
        ui.notify(f"copied {n.id}")

    ui.button(icon="content_copy", on_click=_copy, color=None) \
        .props('flat dense round size=xs aria-label="copy id"').classes("sb-copy") \
        .tooltip("copy the full id").mark("note-copy-id")


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
        if S.read_only() and ttype != "doc" and not gitref.tracked(ctx.cfg.for_type(ttype), path):
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
    did nothing. The rest of the block is skipped."""
    try:
        yield
    except (ValueError, catalog.CatalogError) as e:
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


def viewer(title: str, fill, *, mono: bool = False, reveal=None) -> None:
    """The file viewer's dialog, opened: `title` and a close button over
    what `fill()` renders. The help opens in it too. `reveal`, a file on
    disk, adds `file_buttons` (the user, 2026-10-16: "is open in finder
    even a possibility?")."""
    # The card is what scrolls, so it takes the focus: Quasar's dialog focuses
    # its first [autofocus][tabindex], else its own node, the card's parent,
    # where Page Up/Down scrolled nothing (the user, 2026-10-16).
    with ui.dialog() as dialog, \
            ui.card().classes("bg-panel sb-md-card").props("autofocus tabindex=-1"):
        with ui.row(wrap=False).classes("items-center w-full"):
            ui.label(title).classes("sb-subtitle" + (" sb-mono" if mono else ""))
            ui.space()
            if reveal:
                file_buttons(reveal)
            ui.button(icon="close", on_click=dialog.close) \
                .props('flat dense round size=sm aria-label="close"')
        fill()
    dialog.open()


def view_button(ctx, name, label: str = "", ttype=None):
    """A button that shows the file `name` in a dialog: markdown rendered
    (the user, 2026-10-02), anything else pygments knows coloured, code
    most of all (the user, 2026-10-11). None when the viewer cannot show
    it. Read on the click, so the dialog shows the file as it is then. A
    shift-click shows a file on disk in the file manager instead (the
    user, 2026-10-16)."""
    src = view_path(ctx, name, ttype)
    if src is None:
        return None
    branch = src[1]
    md = (src[0].name if src[0] else name).lower().endswith(".md")
    revealable = src[0] is not None and _reveal_cmd(src[0]) is not None

    def _click(e):
        path = (view_path(ctx, name, ttype) or (None,))[0]
        cmd = path and (e.args or {}).get("shiftKey") and _reveal_cmd(path)
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

    return ui.button(label, icon="article" if md else "code") \
        .on("click", _click, ["shiftKey"]) \
        .props("flat dense no-caps color=muted aria-label=read"
               + ("" if label else " round size=xs")) \
        .tooltip(f"read {name}" + (f", as on {branch}" if branch else "")
                 + (f" · shift-click: {_reveal_label()}" if revealable else "")).mark("file-open")


def _waits_on_you(n, author, rd: Reading) -> bool:
    """summary.asks for the person at this page, on their side's rows: the
    other side's question waits on that side's person."""
    return summ.on_side(rd.by_id, n, rd.side) and (n.supersedes is None
                                                   or summ.asks(author, rd.by_id, n))


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
    page gives none."""
    def narrow(**kv):
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
    # data-id: the page filter matches a pasted id the card does not print.
    # Set as one prop: .props() parses its string, and a `:`-prop runs as JS.
    with ui.element("div").classes(cls + (" sb-resolvable" if resolvable else "")) \
            .mark("note-row") as card:
        card._props["data-id"] = n.id
        with ui.element("div").classes("sb-note-main"):
            _note_content(ctx, n, compact, hit, show_target, seams, diff_to)
            if answering:
                _answer_row(ctx, n, author, refresh)
            elif n.options:
                _options_row(n)
            _note_foot(ctx, n, status, narrow, show_target, git_head, since)
        # On the right, after the text: on the left it pushed an open row's
        # text in past a closed row's (the user, 2026-10-01).
        if resolvable:
            if n.spec.verdict:
                # A pre-registration closes with its verdict: the store
                # refuses a bare resolve, so the button opens the dialog
                # with the result field rather than showing the refusal.
                on_resolve = lambda n=n: edit_dialog(ctx, n, refresh, author=author,
                                                     resolving=True)
            else:
                def on_resolve(n=n):
                    if demo_says("resolves the entry"):
                        return
                    with refusal_shown():
                        api.supersede(ctx, n.id, author=author, status="resolved")
                        refresh()
            ui.button(icon="check", on_click=on_resolve) \
                .props('flat round dense aria-label="resolve"').classes("sb-resolve") \
                .tooltip("resolve").mark("note-resolve")
        if actions:
            actions_bar(ctx, n, refresh, author=author)


def actions_bar(ctx, n, refresh, *, author: str) -> None:
    """Star and edit over a card's corner; a row that has it carries
    `sb-has-actions`, and `sb-starred` when tagged priority."""
    has_pri = "priority" in n.tags
    with ui.element("div").classes("sb-actions"):
        def _star():
            if demo_says("unstars the entry" if has_pri else "stars the entry"):
                return
            with refusal_shown():
                api.retag(ctx, n.id, add=() if has_pri else ("priority",),
                          rm=("priority",) if has_pri else (), author=author)
                refresh()

        ui.button(icon="star" if has_pri else "star_outline", color=None,
                  on_click=_star) \
            .props("flat dense round size=sm" + (" color=warning" if has_pri else "")
                   + ' aria-label="toggle priority"') \
            .classes("sb-star").tooltip("toggle #priority").mark("note-star")
        ui.button(icon="edit", color=None,
                  on_click=lambda: edit_dialog(ctx, n, refresh, author=author)) \
            .props('flat dense round size=sm aria-label="edit"').classes("sb-edit") \
            .tooltip("supersede / edit").mark("note-edit")


def _answer_row(ctx, n, author: str, refresh) -> None:
    """One-click answers to a question someone else wrote last: a button per
    option its asker gave (`--option`, the default first, marked), and the
    person's own words. The answer goes below the question, as the person;
    its asker acts on it and resolves it. Answers took the edit dialog, and
    most open questions had none from the person (2026-10-16). The buttons
    were parsed out of `Default: … Flip: say "…"` prose for a day, until the
    user called that a fragile contract and the entries it bred robotic."""
    def answer(text: str) -> None:
        if not (text := text.strip()):
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

    tip = f"added below the question, as {author}"
    with ui.element("div").classes("sb-answer").mark("answer-row"):
        for i, o in enumerate(n.options or ()):
            ui.button(o, icon="check" if i == 0 else None, color=None,
                      on_click=lambda o=o: answer(o)) \
                .props("outline dense no-caps").classes("px-2").mark("answer-option") \
                .tooltip(f"“{o}”{', the default' if i == 0 else ''}, {tip}")
        field = ui.input(placeholder="or answer in your words · Enter") \
            .props('outlined dense aria-label="answer this question"') \
            .classes("sb-answer-field").mark("answer-field")
        field.on("keydown.enter", lambda: answer(field.value or ""))
        _kept_answer(field, n.id)


def _options_row(n) -> None:
    """A question's options where no buttons draw (the asker's own, one
    answered, one resolved): as measurements show, so the answer below reads
    against its alternatives, and the card shows what `show` prints."""
    # One line, the CLI's: three phrases parted by gaps alone read as one run
    # of words in a browser shot (2026-10-17).
    ui.label(f"options: {summ.options_line(n.options)}").classes("sb-note-meta").mark("options")


def _note_content(ctx, n, compact: bool, hit, show_target: bool = False, seams=(),
                  diff_to=None) -> None:
    """The body, the verdict and measurements: what the row says.
    A bare row says its target, or on that target's own page, its arc."""
    if diff_to:
        what = changes(n, diff_to)
        ui.label(f"{diff_to.author} edited this {summ.when_added(diff_to.created_at)}"
                 + (f": {', '.join(what)}" if what else "")) \
            .classes("sb-note-meta").tooltip(diff_to.created_at).mark("changes")
    if bare(n):
        if show_target:
            target_headline(n)
        elif n.arc_id:
            arc_headline(ctx, n)
    if n.spec.verdict:
        # Clipped on a board like the body: a check's `checked` ran to
        # three lines and pushed the next row off the screen.
        fit = (lambda t: summ.clip(t, summ.HEAD_CHARS)) if compact else (lambda t: t)
        if diff_to and (n.checked, n.result) != (diff_to.checked, diff_to.result):
            ui.html(f"checked: {word_diff(n.checked, diff_to.checked)}   →   "
                    f"result: {word_diff(n.result, diff_to.result)}", sanitize=False) \
                .classes("sb-verdict").mark("note-verdict")
        else:
            ui.label(f"checked: {fit(n.checked) or '—'}   →   result: {fit(n.result) or '—'}") \
                .classes("sb-verdict").mark("note-verdict")
    if n.body and compact and (hit or len(summ.flatten(n.body)) > summ.BODY_CHARS):
        # A list to scan, not a page to read: one bug's four paragraphs
        # filled the boards (seen in the browser, 2026-09-27). A search
        # result is text too, even a short one, so every hit is marked.
        text = snippet(n.body, hit)
        # A clipped body is the link to the whole note: a "full note" link
        # under each one said it again (the user, 2026-10-01).
        with (ui.link(target=href(id=n.id)).classes("sb-note-open").mark("note-full")
              if text != summ.flatten(n.body) else nullcontext()):
            if hit:
                ui.html(marked(text, hit), sanitize=False) \
                    .classes("sb-note-text sb-snippet").mark("note-snippet")
            else:
                ui.label(text).classes("sb-note-text")
    elif diff_to and edited(n.body, diff_to.body):
        for op, text in diff_blocks(n.body, diff_to.body):
            md = NoteMarkdown(link_ids(text, ctx.store_dir))
            if op != "=":
                side = "del" if op == "-" else "ins"
                md.classes(f"sb-diff-{side}").mark(f"diff-{side}")
    elif n.body:
        # Each piece on its own, a rule in the kind's colour between: an
        # append's text after its seam, a prepend's before.
        start = 0
        for at, who, when, above, rewrote in seams:
            NoteMarkdown(link_ids(n.body[start:at], ctx.store_dir))
            said = (f"{who} {'edited the text above and ' if rewrote else ''}added "
                    f"{'the above ' if above else ''}{summ.when_added(when)}")
            with ui.element("div").classes("sb-seam"):
                ui.label(said).tooltip(when).mark("seam")
            start = at
        NoteMarkdown(link_ids(n.body[start:], ctx.store_dir))   # sanitize=True: DOMPurify
    if n.measurements:
        with ui.row().classes("gap-3 flex-wrap").mark("measurements"):
            for k, v in n.measurements.items():
                ui.label(f"{k}: {v}").classes("sb-note-meta")


def _note_foot(ctx, n, status, narrow, show_target: bool, git_head, since=None) -> None:
    """One wrapping line: what kind and state the row is and where it points
    on the left; who wrote it, when, and its id on the right."""
    with ui.element("div").classes("sb-note-foot"):
        if n.due:
            due_chip(n, status)
        ui.link(n.kind, narrow(kind=n.kind)) \
            .classes(f"sb-chip sb-kindchip {kind_class(n.kind)}").mark("note-kind")
        if status:
            ui.link(status, narrow(status=status)).classes(f"sb-chip sb-chip-{status}")
        if n.spec.verdict:
            _check_badge(ctx, n, git_head)
        if show_target:
            with ui.element("span").classes("sb-pair"):
                if not bare(n):             # else the headline says it
                    target_link(n)
                view_button(ctx, n.target.name, ttype=n.target.type)
        if since:
            _since_chip(ctx, n, since)
        if n.arc_id and (show_target or not bare(n)):
            ui.link(f"arc: {n.arc_id}", f"/arc?id={quote(n.arc_id)}") \
                .classes("sb-chip sb-target").mark("note-arc")
        for r in n.refs:
            with ui.element("span").classes("sb-pair"):
                ui.link(f"↗ {summ.ref_label(r)}", target_href(r.type, r.name)) \
                    .classes("sb-chip sb-ref").mark("note-ref")
                view_button(ctx, r.name, ttype=r.type)
        for t in n.tags:
            ui.link(f"#{t}", narrow(tag=t)).classes("sb-tag").mark(f"tag-{t}")
        with ui.element("span").classes("sb-note-by"):
            ui.link(n.author, narrow(author=n.author)).classes("sb-note-meta")
            ui.label(ago(n.created_at)).classes("sb-note-meta") \
                .tooltip(S.shown(n.created_at)).mark("note-age")
            id_link(n)


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


def _kept_answer(field, nid: str) -> None:
    """Words typed in an answer field outlive the reload another card's write
    brings (audit, 2026-10-28): kept per tab, put back once the page connects."""
    client = ui.context.client

    def keep(e) -> None:
        kept = app.storage.tab.setdefault(_ANSWERS, {})
        if e.value:
            kept[nid] = e.value
        else:
            kept.pop(nid, None)

    def back() -> None:
        with client:
            if text := app.storage.tab.get(_ANSWERS, {}).get(nid):
                field.value = text
    field.on_value_change(keep)
    client.on_connect(back)


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
            what = changes(now[i], was) if i in now else []
            ui.label(f"{short_id(i)} · {', '.join(what) or 'its earlier version'} · "
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


def edit_dialog(ctx, n, refresh, *, author: str, resolving: bool = False) -> None:
    """Supersede with edits. `resolving=True` is the resolve path for a
    status+verdict row: the result is required and the row closes on save."""
    with ui.dialog() as dialog, ui.card().classes("bg-panel"):
        ui.label("resolve — the result is the verdict" if resolving
                 else "edit — its earlier version is kept").classes("sb-stat-label")
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
        # Attaching an EXISTING note to an arc has no other route: seeding
        # mints new tasks, and target/provenance are the only two fields
        # supersede refuses. This is the one.
        arc = ui.select(arc_options(ctx), value=n.arc_id or "",
                             label="arc").props("dense outlined") \
            .classes("w-full").mark("edit-arc")
        # A verdict is the part `list` prints and queries, so a typo in it is
        # the one thing the body edit above cannot reach. Rendered only on a
        # verdict kind: the edited row's kind is fixed, so this needs no
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

        def _save():
            resolve = resolving or bool(n.spec.status and close is not None and close.value)
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
            edited = body.value != n.body
            if edited and tip.body != n.body:
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
            closing = {"status": "resolved"} if resolve else {}
            retired = ["retired"] if not n.spec.status and close.value else []
            due_v = (due.value or "").strip() or None if due is not None else n.due
            dated = {"due": due_v} if due_v != n.due else {}
            final = list(dict.fromkeys(split_tags(tags.value) + harvested + more_tags
                                       + (["priority"] if pri.value else []) + retired))
            gone = [t for t in n.tags if t not in final]
            kept = [t for t in tip.tags if t not in gone]
            arc_v = arc.value or None
            if demo_says("saves the edit"):
                return
            try:
                api.supersede(ctx, n.id, author=author, **text,
                              tags=kept + [t for t in final if t not in n.tags and t not in kept],
                              arc_id=arc_v if arc_v != n.arc_id else tip.arc_id,
                              **verdict, **closing, **dated, **on)
            except (ValueError, catalog.CatalogError) as e:    # a closed catalog's refusal
                ui.notify(str(e), type="negative")
                return
            dialog.close()
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
             start_kind: str | None = None) -> SimpleNamespace:
    """Writes go through api.add, which canonicalizes the target name, stamps
    provenance on checks, and takes author keyword-only. `arc_id` is the arc
    the form starts on. With `another`, Shift Enter adds the note and keeps
    the form, cleared, for the next one, and calls `another` in place of
    `refresh`. The target starts on (target_type, target_name), and the
    header changes it: the GUI put a note only on the project or on the page's
    object (the user, 2026-10-01). Not an arc: the arc select puts a note in one.
    `start_kind` is the kind it starts as, when this store has it. It
    returns a handle a dropped file drives (`chrome._dropped`)."""
    kinds = ctx.kinds
    labels = list(kinds)
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label("new entry on").classes("sb-stat-label")
        commits = commit_names(ctx)                # once: each picker read it, 70 ms each
        on_type, on_name = target_picker(ctx, target_type, target_name, commits=commits)
        refs = refs_field(ctx, commits=commits).classes("col").style("min-width:160px") \
            .mark("note-refs")
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
