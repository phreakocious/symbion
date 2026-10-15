"""The note row, reused by every surface. Colors are CSS classes from theme.py
-- no hexes here, the hex audit forbids them outside theme.py.

Refreshables are defined PER call (per page/client). A module-level
@ui.refreshable accumulates instances across pages and its .refresh() then
touches a deleted client (RuntimeError 'client has been deleted').
"""
from __future__ import annotations

import functools
import html
import re
from contextlib import nullcontext
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import quote

from nicegui import app, ui
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
# keeps *asterisk* emphasis and disables underscore emphasis. A row body is
# read in term.body_parser instead, as at a terminal.
FILE_MD_EXTRAS = ["fenced-code-blocks", "tables", "code-friendly"]


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


_BODY_MD = term.body_parser(_highlight)


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
    row read as no change at all (the owner, 2026-10-09). The body, checked
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
    out += [f for f in ("body", "checked", "result") if getattr(old, f) != getattr(new, f)]
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


@functools.lru_cache(maxsize=8)
def _tails(store_dir: str, _stamp) -> dict:
    """summary.cite_ids of the store. `_stamp` is the notes file's mtime and
    size, so a row written while the server runs is seen on the next render."""
    return summ.cite_ids([n.id for n in S.load(store_dir)])


def link_ids(body: str, store_dir) -> str:
    """Each full note id in `body`, and each tail of a row in this store, as
    a markdown link to that note, except inside code, where a link would
    print as literal brackets. A tail of no row here stays text: rows cite
    other stores' rows too."""
    def link(m):
        nid = m[1] if len(m[1]) > 17 else tails().get(m[1])
        return f"[{m[1]}](/notes?id={nid})" if nid else m[1]

    def tails():
        st = S.notes_path(store_dir).stat()
        return _tails(str(store_dir), (st.st_mtime_ns, st.st_size))

    return "".join(p if i % 2 else _NOTE_ID.sub(link, p)
                   for i, p in enumerate(_CODE.split(body)))


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


def check_tip(cfg, prov, state, distance) -> str:
    """What the badge was measured against. The chip lit up on hover and a
    click did nothing (the owner, 2026-09-08); `behind N` asks the reader to
    go and look at N commits, so the tip lists them."""
    sha = S.stamp_sha(prov)
    if not sha or state == "external":
        return ""
    at = f"stamped at {sha[:7]}"
    if state == "current":
        return f"{at}, this checkout's HEAD"
    if state == "diverged":
        return f"{at}, on another line of development than HEAD"
    if state == "dangling":
        return (f"{at}, which no branch or tag holds: an amend, a rebase or a deleted "
                f"branch left it behind. Re-run the check to stamp a live commit")
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
    # An age with no tip read as vague (the owner, 2026-10-07): say what it dates.
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
    cls = {"current": "sb-chip-good", "behind": "sb-chip-notable", "external": "",
           "pending": "", "unstamped": "",
           "ahead": "sb-chip-notable",
           "diverged": "sb-chip-bad", "dangling": "sb-chip-bad",
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


def _due_chip(n, status) -> None:
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
    secs = max(0, ((now or datetime.now().astimezone()) - then).total_seconds())
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
        .mark("note-id").tooltip(f"{n.id}\nthis note alone")

    def _copy():
        ui.clipboard.write(n.id)
        ui.notify(f"copied {n.id}")

    ui.button(icon="content_copy", on_click=_copy, color=None) \
        .props('flat dense round size=xs aria-label="copy id"').classes("sb-copy") \
        .tooltip("copy the full id").mark("note-copy-id")


@functools.lru_cache(maxsize=256)
def _lexer(filename: str):
    """pygments' lexer for a file name, or None."""
    try:
        return get_lexer_for_filename(filename)
    except ClassNotFound:
        return None


def view_path(ctx, name, ttype=None):
    """The checkout's file `name` names, when the viewer shows its kind:
    markdown, or a file pygments names a language for; else None. A target
    name is typed text, so the file must resolve inside the checkout: `..`
    and a symlink out of it read nothing. `ttype`, a row's target or ref
    type, picks the repo in a store with [repos]."""
    root = ctx.store_dir if ttype == "doc" else \
        ctx.cfg.for_type(ttype).work_root           # a repo's row: its repo's file
    if not root or not name:
        return None
    root = root.resolve()
    path = (root / name).resolve()
    if not (path.is_relative_to(root) and path.is_file()):
        return None
    return path if path.suffix.lower() == ".md" or _lexer(path.name) else None


# ponytail: the whole file in one dialog; page it if a large one is slow.
VIEW_CHARS = 500_000


def view_button(ctx, name, label: str = "", ttype=None):
    """A button that shows the file `name` in a dialog: markdown rendered
    (the owner, 2026-10-02), anything else pygments knows coloured, code
    most of all (the owner, 2026-10-11). None when the viewer cannot show
    it. Read on the click, so the dialog shows the file as it is then."""
    path = view_path(ctx, name, ttype)
    if path is None:
        return None
    md = path.suffix.lower() == ".md"

    def _open():
        path = view_path(ctx, name, ttype)
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if path else None
        except OSError:
            text = None
        if text is None:
            ui.notify(f"cannot read {name}", type="warning")
            return
        if "\0" in text or len(text) > VIEW_CHARS:
            ui.notify(f"{name} is not text, or is over {VIEW_CHARS:,} characters",
                      type="warning")
            return
        with ui.dialog() as dialog, ui.card().classes("bg-panel sb-md-card"):
            with ui.row(wrap=False).classes("items-center w-full"):
                ui.label(name).classes("sb-subtitle sb-mono")     # a path: its case is its name
                ui.space()
                ui.button(icon="close", on_click=dialog.close) \
                    .props('flat dense round size=sm aria-label="close"')
            if md:
                ui.markdown(text, extras=FILE_MD_EXTRAS).mark("file-view")
            else:
                # One fenced block, as a body's is coloured. A fence longer
                # than any run of backticks in the file is one it cannot close.
                fence = "`" * max(3, 1 + max(map(len, re.findall("`+", text)), default=0))
                lang = (_lexer(path.name).aliases or ["text"])[0]
                NoteMarkdown(f"{fence}{lang}\n{text}\n{fence}").mark("file-view")
        dialog.open()

    return ui.button(label, icon="article" if md else "code", on_click=_open) \
        .props("flat dense no-caps color=muted aria-label=read"
               + ("" if label else " round size=xs")) \
        .tooltip(f"read {name}").mark("file-open")


def render_note(ctx, n, refresh, *, author: str, show_target: bool = False,
                base: dict | None = None, compact: bool = False, hit=(),
                actions: bool = True, git_head: str | None = None,
                since: int | None = None, seams=(), diff_to=None) -> None:
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
    changed checked or result, and shows a rewritten body as a diff."""
    def narrow(**kv):
        return href(**{**(base or {}), **kv})

    status = S.read_status(n)
    has_pri = "priority" in n.tags
    resolvable = actions and n.spec.status and status == "open"
    cls = f"sb-note w-full {kind_class(n.kind)}"
    if actions:
        cls += " sb-has-actions" + (" sb-starred" if has_pri else "")
    # data-id: the page filter matches a pasted id the card does not print
    with ui.element("div").classes(cls + (" sb-resolvable" if resolvable else "")) \
            .props(f"data-id={n.id}").mark("note-row"):
        with ui.element("div").classes("sb-note-main"):
            _note_content(ctx, n, compact, hit, show_target, seams, diff_to)
            _note_foot(ctx, n, status, narrow, show_target, git_head, since)
        # On the right, after the text: on the left it pushed an open row's
        # text in past a closed row's (the owner, 2026-10-01).
        if resolvable:
            if n.spec.verdict:
                # A pre-registration closes with its verdict: the store
                # refuses a bare resolve, so the button opens the dialog
                # with the result field rather than showing the refusal.
                on_resolve = lambda n=n: edit_dialog(ctx, n, refresh, author=author,
                                                     resolving=True)
            else:
                on_resolve = lambda n=n: (api.supersede(ctx, n.id, author=author,
                                                        status="resolved"),
                                          refresh())
            ui.button(icon="check", on_click=on_resolve) \
                .props('flat round dense aria-label="resolve"').classes("sb-resolve") \
                .tooltip("resolve").mark("note-resolve")
        if actions:
            with ui.element("div").classes("sb-actions"):
                def _star(n=n, has_pri=has_pri):
                    api.retag(ctx, n.id, add=() if has_pri else ("priority",),
                              rm=("priority",) if has_pri else (), author=author)
                    refresh()

                ui.button(icon="star" if has_pri else "star_outline", color=None,
                          on_click=_star) \
                    .props("flat dense round size=sm" + (" color=warning" if has_pri else "")
                           + ' aria-label="toggle priority"') \
                    .tooltip("toggle #priority").mark("note-star")
                ui.button(icon="edit", color=None,
                          on_click=lambda n=n: edit_dialog(ctx, n, refresh, author=author)) \
                    .props('flat dense round size=sm aria-label="edit"') \
                    .tooltip("supersede / edit").mark("note-edit")


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
        # under each one said it again (the owner, 2026-10-01).
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
            _due_chip(n, status)
        ui.link(n.kind, narrow(kind=n.kind)) \
            .classes(f"sb-chip sb-kindchip {kind_class(n.kind)}").mark("note-kind")
        if status:
            ui.link(status, narrow(status=status)).classes(f"sb-chip sb-chip-{status}")
        if n.spec.verdict:
            _check_badge(ctx, n, git_head)
        if show_target:
            if not bare(n):                 # else the headline says it
                target_link(n)
            view_button(ctx, n.target.name, ttype=n.target.type)
        if since:
            _since_chip(ctx, n, since)
        if n.arc_id and (show_target or not bare(n)):
            ui.link(f"arc: {n.arc_id}", f"/arc?id={quote(n.arc_id)}") \
                .classes("sb-chip sb-target").mark("note-arc")
        for r in n.refs:
            ui.link(f"↗ {summ.ref_label(r)}",
                    f"/object?type={quote(r.type)}&name={quote(r.name or '', safe='')}") \
                .classes("sb-chip sb-ref").mark("note-ref")
            view_button(ctx, r.name, ttype=r.type)
        for t in n.tags:
            ui.link(f"#{t}", narrow(tag=t)).classes("sb-tag").mark(f"tag-{t}")
        with ui.element("span").classes("sb-note-by"):
            ui.link(n.author, narrow(author=n.author)).classes("sb-note-meta")
            ui.label(ago(n.created_at)).classes("sb-note-meta") \
                .tooltip(S.shown(n.created_at)).mark("note-age")
            id_link(n)


_SAID = "sb-said"


def say_after_reload(text: str) -> None:
    """A notice for the page a reload brings: one made before the reload
    died with the page, so `committed …` never showed (2026-10-13)."""
    app.storage.tab[_SAID] = text


def show_said() -> None:
    """Once the page connects, what the write before its reload said."""
    client = ui.context.client

    def _show():
        with client:
            if said := app.storage.tab.pop(_SAID, None):
                ui.notify(said)
    client.on_connect(_show)


def undo_dialog(ctx, author: str) -> None:
    """The top bar's undo asks first, with what it changes: each row the
    last write added goes, and each row it changed gets its earlier version
    back (the owner, 2026-10-13). It replaced a timed notice, which set a
    deadline the store does not have, and whose undo did nothing as it
    faded."""
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
                 else "edit — this version is not yet committed, so it changes in place"
                 if api.rewritable(ctx, n.id, author=author)
                 else "supersede — the old entry is kept").classes("sb-stat-label")
        # What the entry is on, picked as the new-entry form's header picks
        # it: the dialog could change neither (a tester, 2026-10-15). An
        # untouched target is not sent, so a departed one never resolves again.
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("on").classes("sb-stat-label")
            on_type, on_name = target_picker(ctx, n.target.type, n.target.name,
                                             marks=("edit-on-type", "edit-on-name"))
            refs = refs_field(ctx, n.refs).classes("col").style("min-width:160px") \
                .mark("edit-refs")
        body = ui.textarea("body", value=n.body).props("outlined") \
            .classes("min-w-[420px]").mark("edit-body")
        # Most edits add to a row, and the body's end was a scroll away (the
        # owner, 2026-10-01). A resolve's field is the result: no focus here.
        more = ui.textarea("add after the body").props(
            "outlined autogrow" + ("" if resolving else " autofocus")) \
            .classes("w-full").mark("edit-append")
        # The star beside the tags, so an edit and a star are one write, not
        # two (the owner, 2026-10-02). The switch holds `priority`; the field
        # the rest, and a `priority` typed there still counts. So with
        # closing: an open status row resolves here, with its message (the
        # owner, 2026-10-13), and a row with no status retires, which only a
        # typed tag reached (the owner, 2026-10-14).
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
            on = {"target": target} if (target["type"], target["name"]) \
                != (n.target.type, n.target.name) else {}
            if list(refs.value or ()) != [ref_key(r) for r in n.refs]:
                on["refs"] = list(refs.value or ())
            harvested, cleaned = api.harvest_hashtags(body.value or "")
            more_tags, added = api.harvest_hashtags(more.value or "")
            if not added.strip():
                text = {"body": cleaned}
            elif body.value == n.body:
                # After the CURRENT row's body, read under the lock, as
                # `supersede --append` does: a revision made since this
                # dialog opened is kept.
                text = {"append_body": added}
            else:
                text = {"body": S.appended(cleaned, added)}
            verdict = {"checked": checked.value or None,
                       "result": res.value or None} if checked is not None else {}
            closing = {"status": "resolved"} if resolve else {}
            retired = ["retired"] if not n.spec.status and close.value else []
            dated = {"due": (due.value or "").strip() or None} if due is not None else {}
            try:
                api.supersede(ctx, n.id, author=author, **text,
                              tags=list(dict.fromkeys(split_tags(tags.value) + harvested
                                                      + more_tags
                                                      + (["priority"] if pri.value else [])
                                                      + retired)),
                              arc_id=arc.value or None, **verdict, **closing, **dated, **on)
            except ValueError as e:
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
    stayed on screen though the save dropped it (the owner, 2026-10-01).
    A `#` key never lands at all: until the blur, a person could not tell
    whether it would be part of the tag (the owner, again)."""
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

    def __init__(self, value: str, options: list, *, tokens: bool = False) -> None:
        super().__init__(value=value, on_value_change=None)
        self._props["options"] = options
        self._props["tokens"] = tokens

    def set_options(self, options: list) -> None:
        self._props["options"] = options
        self.update()


def tags_field(ctx, value: str = ""):
    """A tags field that offers the store's tags as a word is typed, the
    most used first (the owner, 2026-10-01)."""
    counts = S.tag_counts(S.load(ctx.store_dir))
    field = Suggest(value, sorted(counts, key=lambda t: (-counts[t], t)), tokens=True) \
        .props("dense outlined options-dense label=tags")
    tidy_tags(field)
    return field


def date_menu(field, marker: str) -> None:
    """A calendar on a due field, dates only (the owner, 2026-10-12). The
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


def target_names(ctx, typ: str) -> list:
    """What the name field offers for `typ`: the catalog's names for a catalog
    type, else the names this store's rows have used for it, newest first. A
    catalog that fails offers the store's names: the write reports its error."""
    # ponytail: runs the catalog on the event loop, as api.add does at every
    # GUI write: 22 ms for `git ls-files`, 170 ms for the slowest adopter
    # catalog (2026-10-02). If one is slow, run both under `run.io_bound`.
    if typ in ctx.cfg.catalogs:
        try:
            return catalog.names(ctx.cfg, typ, allow_empty=True)
        except catalog.CatalogError:
            pass
    return [o.name for o in used_objects(ctx) if o.type == typ]


def used_objects(ctx) -> list:
    """The objects this store's entries are on, as target or ref, newest
    first, once each."""
    seen: dict = {}
    for n in S.newest_first(S.load(ctx.store_dir)):
        for o in (n.target, *n.refs):
            if o.name:
                seen.setdefault((o.type, o.name), o)
    return list(seen.values())


def target_picker(ctx, typ: str, name: str | None, marks=("note-on-type", "note-on-name")):
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
    offer = lambda t: target_names(ctx, t) if t != "project" else []   # noqa: E731
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


def refs_field(ctx, refs=()):
    """`also on`: the objects an entry is on beside its target, its refs, as
    chips. It offers the objects this store's entries are on, and takes a
    new TYPE:NAME typed: the GUI put an entry on one object (a tester,
    2026-10-15). A value is the name as stored, so a kept ref resolves to
    itself; the chip shows a commit's short sha."""
    # ponytail: a changed field sends every ref, as `supersede --ref` does,
    # so a kept ref whose name left its catalog resolves again; send only
    # the change if that bites.
    options = {ref_key(o): summ.ref_label(o) for o in (*refs, *used_objects(ctx))}
    return ui.select(options, value=[ref_key(r) for r in refs], multiple=True,
                     new_value_mode="add-unique", label="also on") \
        .props("dense outlined options-dense use-chips")


def ref_key(o) -> str:
    """An object as `refs_field` holds it: TYPE:NAME, the name as stored."""
    return f"{o.type}:{o.name or ''}"


def add_form(ctx, target_type, target_name, refresh, *, author: str,
             arc_id: str | None = None, another=None, start_kind: str | None = None) -> None:
    """Writes go through api.add, which canonicalizes the target name, stamps
    provenance on checks, and takes author keyword-only. `arc_id` is the arc
    the form starts on. With `another`, Shift Enter adds the note and keeps
    the form, cleared, for the next one, and calls `another` in place of
    `refresh`. The target starts on (target_type, target_name), and the
    header changes it: the GUI put a note only on the project or on the page's
    object (the owner, 2026-10-01). Not an arc: the arc select puts a note in one.
    `start_kind` is the kind it starts as, when this store has it."""
    kinds = ctx.kinds
    labels = list(kinds)
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label("new entry on").classes("sb-stat-label")
        on_type, on_name = target_picker(ctx, target_type, target_name)
        refs = refs_field(ctx).classes("col").style("min-width:160px").mark("note-refs")
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
                ui.notify("empty note", type="warning")
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
            try:
                added = api.add(ctx, row, author=author)
            except ValueError as e:            # ambiguous name, bad ref type
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
