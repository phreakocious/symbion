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
from urllib.parse import quote

from nicegui import ui
from nicegui.elements.mixins.value_element import ValueElement
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
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


class NoteMarkdown(ui.markdown):
    """A row's body, read as `list` and `show` read it at a terminal: the
    same parser, so a list, a `~~strike~~` and a raw tag come out alike.
    ui.markdown's markdown2 read them otherwise, so this replaces its
    renderer, keeping its DOMPurify pass and its .codehilite CSS."""
    # ponytail: overrides a private nicegui hook; if it moves,
    # test_a_tag_in_a_body_prints_as_text fails, and ui.html takes the HTML.
    def _handle_content_change(self, content: str) -> None:
        self._props["innerHTML"] = _body_html(content)


# store.new_id's shape, or the 10-character tail SKILL.md says to cite.
# Bodies cite rows by id ("settled by <id>"), and the GUI had no way to
# follow one but pasting it into the search.
_NOTE_ID = re.compile(r"(?<![\w/=[-])(\d{8}-\d{6}-\d{6}-[0-9a-f]{3}|\d{6}-[0-9a-f]{3})(?![\w-])")
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)


@functools.lru_cache(maxsize=8)
def _tails(store_dir: str, _stamp) -> dict:
    """Each 10-character tail -> the one id in the store that ends in it, or
    None when several do. `_stamp` is the notes file's mtime and size, so a
    row written while the server runs is seen on the next render."""
    out = {}
    for n in S.load(store_dir):
        out[n.id[-10:]] = None if n.id[-10:] in out else n.id
    return out


def link_ids(body: str, store_dir) -> str:
    """Each full note id in `body`, and each tail of a row in this store, as
    a markdown link to that note, except inside code, where a link would
    print as literal brackets. A tail of no row here stays text: rows cite
    other stores' rows too."""
    def link(m):
        nid = m[1] if len(m[1]) > 10 else tails().get(m[1])
        return f"[{m[1]}](/notes?id={nid})" if nid else m[1]

    def tails():
        st = S.notes_path(store_dir).stat()
        return _tails(str(store_dir), (st.st_mtime_ns, st.st_size))

    return "".join(p if i % 2 else _NOTE_ID.sub(link, p)
                   for i, p in enumerate(_CODE.split(body)))


def kind_class(kind: str) -> str:
    """A kind's colour, as `list` prints it, for its chip and its row's edge;
    a kind the store declares shares one (2026-10-01; chips by bits before,
    which left `decision` and `idea` uncoloured)."""
    return f"sb-kind-{kind}" if kind in term.KIND else "sb-kind-own"


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
    if state not in ("behind", "ahead"):
        return at
    lines = gitref.oneline(cfg, f"{sha}..HEAD" if state == "behind" else f"HEAD..{sha}",
                           TIP_COMMITS)
    more = f"\n+{distance - len(lines)} more" if distance > len(lines) else ""
    since = (f"HEAD is {distance} commits past it" if state == "behind"
             else f"it is {distance} commits past HEAD")
    return f"{at}; {since}:\n" + "\n".join(lines) + more


def _check_badge(ctx, n, git_head=None) -> None:
    state, distance = api.verdict_state(ctx.cfg, n, git_head)
    text = f"{state} {distance}" if state in ("behind", "ahead") else state
    if state == "unverifiable":
        text = f"unverifiable — {api.why_unverifiable(n.provenance)}"
    elif state == "external":
        text = f"external — {summ.age_phrase(n.provenance['at'])}"
    elif state == "unstamped":
        text = f"unstamped — {summ.age_phrase(n.created_at)}"
    cls = {"current": "sb-chip-good", "behind": "sb-chip-notable", "external": "",
           "pending": "", "unstamped": "",
           "ahead": "sb-chip-notable",
           "diverged": "sb-chip-bad", "unverifiable": "sb-chip-bad"}[state]
    chip = ui.label(text).classes(f"sb-chip {cls}").mark("check-state")
    if not S.stamp_sha(n.provenance) or state in ("external", "pending"):
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
    return (f"{n.target.name} changed in {summ._count(len(shas), 'commit')} since this row:\n"
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


def md_path(ctx, name):
    """The checkout's markdown file `name` names, or None. A target name is
    typed text, so the file must resolve inside the checkout: `..` and a
    symlink out of it read nothing."""
    root = ctx.cfg.work_root
    if not root or not name or not name.lower().endswith(".md"):
        return None
    root = root.resolve()
    path = (root / name).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def md_button(ctx, name, label: str = ""):
    """A button that shows the markdown file `name` rendered, in a dialog
    (the owner, 2026-10-02); None when `name` is not one. Read on the click,
    so the dialog shows the file as it is then."""
    if md_path(ctx, name) is None:
        return None

    def _open():
        path = md_path(ctx, name)
        try:
            text = path.read_text(errors="replace") if path else None
        except OSError:
            text = None
        if text is None:
            ui.notify(f"cannot read {name}", type="warning")
            return
        with ui.dialog() as dialog, ui.card().classes("bg-panel sb-md-card"):
            with ui.row(wrap=False).classes("items-center w-full"):
                ui.label(name).classes("sb-subtitle sb-mono")     # a path: its case is its name
                ui.space()
                ui.button(icon="close", on_click=dialog.close) \
                    .props('flat dense round size=sm aria-label="close"')
            ui.markdown(text, extras=FILE_MD_EXTRAS).mark("md-view")
        dialog.open()

    return ui.button(label, icon="article", on_click=_open) \
        .props("flat dense no-caps color=muted aria-label=read"
               + ("" if label else " round size=xs")) \
        .tooltip(f"read {name}").mark("md-open")


def render_note(ctx, n, refresh, *, author: str, show_target: bool = False,
                base: dict | None = None, compact: bool = False, hit=(),
                actions: bool = True, git_head: str | None = None,
                since: int | None = None) -> None:
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
    the row's gitref.commits_since, counted once by a page of many cards."""
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
            _note_content(ctx, n, compact, hit, show_target)
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


def _note_content(ctx, n, compact: bool, hit, show_target: bool = False) -> None:
    """The body, the verdict, measurements and evidence: what the row says.
    A bare row says its target, or on that target's own page, its arc."""
    if bare(n):
        if show_target:
            target_headline(n)
        elif n.arc_id:
            arc_headline(ctx, n)
    if n.spec.verdict:
        # Clipped on a board like the body: a check's `checked` ran to
        # three lines and pushed the next row off the screen.
        fit = (lambda t: summ.clip(t, summ.HEAD_CHARS)) if compact else (lambda t: t)
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
    elif n.body:
        NoteMarkdown(link_ids(n.body, ctx.store_dir))   # sanitize=True default: DOMPurify
    if n.measurements:
        with ui.row().classes("gap-3 flex-wrap").mark("measurements"):
            for k, v in n.measurements.items():
                ui.label(f"{k}: {v}").classes("sb-note-meta")
    if n.evidence:
        # Text only. Serving store-relative paths over HTTP buys nothing a
        # file manager does not, and makes the store a static file host.
        ui.label("evidence: " + ", ".join(n.evidence)).classes("sb-note-meta")


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
            md_button(ctx, n.target.name)
        if since:
            _since_chip(ctx, n, since)
        if n.arc_id and (show_target or not bare(n)):
            ui.link(f"arc: {n.arc_id}", f"/arc?id={quote(n.arc_id)}") \
                .classes("sb-chip sb-target").mark("note-arc")
        for r in n.refs:
            ui.link(f"↗ {summ.ref_label(r)}",
                    f"/object?type={quote(r.type)}&name={quote(r.name or '', safe='')}") \
                .classes("sb-chip sb-ref").mark("note-ref")
            md_button(ctx, r.name)
        for t in n.tags:
            ui.link(f"#{t}", narrow(tag=t)).classes("sb-tag").mark(f"tag-{t}")
        with ui.element("span").classes("sb-note-by"):
            ui.link(n.author, narrow(author=n.author)).classes("sb-note-meta")
            ui.label(ago(n.created_at)).classes("sb-note-meta") \
                .tooltip(S.shown(n.created_at)).mark("note-age")
            id_link(n)


def edit_dialog(ctx, n, refresh, *, author: str, resolving: bool = False) -> None:
    """Supersede with edits. `resolving=True` is the resolve path for a
    status+verdict row: the result is required and the row closes on save."""
    with ui.dialog() as dialog, ui.card().classes("bg-panel"):
        ui.label("resolve — the result is the verdict" if resolving
                 else "edit — not yet committed, so the row itself changes"
                 if api.rewritable(ctx, n.id, author=author)
                 else "supersede — the old row is kept").classes("sb-stat-label")
        body = ui.textarea("body", value=n.body).props("outlined") \
            .classes("min-w-[420px]").mark("edit-body")
        # Most edits add to a row, and the body's end was a scroll away (the
        # owner, 2026-10-01). A resolve's field is the result: no focus here.
        more = ui.textarea("add after the body").props(
            "outlined autogrow" + ("" if resolving else " autofocus")) \
            .classes("w-full").mark("edit-append")
        # The star beside the tags, so an edit and a star are one write, not
        # two (the owner, 2026-10-02). The switch holds `priority`; the field
        # the rest, and a `priority` typed there still counts.
        with ui.row(wrap=False).classes("items-center gap-2 w-full"):
            tags = tags_field(ctx, ", ".join(t for t in n.tags if t != "priority")) \
                .classes("col").mark("edit-tags")
            pri = ui.switch("priority", value="priority" in n.tags).mark("edit-priority")
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
        due = ui.input("due", value=n.due or "",
                       placeholder="YYYY-MM-DD or 2026-10-01T22:30Z") \
            .props("dense outlined").classes("w-full").mark("edit-due") \
            if n.spec.status else None
        checked = res = None
        if n.spec.verdict:
            checked = ui.input("checked", value=n.checked or "") \
                .props("dense outlined").classes("w-full").mark("edit-checked")
            res = ui.input("result", value=n.result or "") \
                .props("dense outlined").classes("w-full").mark("edit-result")

        def _save():
            if resolving and not (res.value or "").strip():
                ui.notify("a result is required to resolve this kind", type="warning")
                return
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
            closing = {"status": "resolved"} if resolving else {}
            dated = {"due": (due.value or "").strip() or None} if due is not None else {}
            try:
                api.supersede(ctx, n.id, author=author, **text,
                              tags=list(dict.fromkeys(split_tags(tags.value) + harvested
                                                      + more_tags
                                                      + (["priority"] if pri.value else []))),
                              arc_id=arc.value or None, **verdict, **closing, **dated)
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
    used = [o for n in S.newest_first(S.load(ctx.store_dir)) for o in (n.target, *n.refs)
            if o.type == typ and o.name]
    return list(dict.fromkeys(o.name for o in used))


def add_form(ctx, target_type, target_name, refresh, *, author: str,
             arc_id: str | None = None, another=None) -> None:
    """Writes go through api.add, which canonicalizes the target name, stamps
    provenance on checks, and takes author keyword-only. `arc_id` is the arc
    the form starts on. With `another`, Shift Enter adds the note and keeps
    the form, cleared, for the next one, and calls `another` in place of
    `refresh`. The target starts on (target_type, target_name), and the
    header changes it: the GUI put a note only on the project or on the page's
    object (the owner, 2026-10-01). Not an arc: the arc select puts a note in one."""
    kinds = ctx.kinds
    labels = list(kinds)
    types = ["project", *sorted(ctx.target_types - {"project", "arc"})]
    if target_type not in types:
        types.append(target_type)
    with ui.row().classes("items-center gap-2 w-full"):
        ui.label("new note on").classes("sb-stat-label")
        on_type = ui.select(types, value=target_type) \
            .props('dense outlined options-dense aria-label="target type"').mark("note-on-type")
        # The name in its own case: a label's capitals rewrote a path. Any
        # part of a name matches, and a new one is typed freely.
        offer = lambda typ: target_names(ctx, typ) if typ != "project" else []   # noqa: E731
        on_name = Suggest(target_name or "", offer(target_type)) \
            .props('dense outlined options-dense placeholder=name aria-label="target name"') \
            .classes("col").style("min-width:160px").mark("note-on-name")
        on_name.bind_visibility_from(on_type, "value", backward=lambda v: v != "project")
        on_type.on_value_change(lambda e: on_name.set_options(offer(e.value)))
        ui.space()
        ui.label("⌘/Ctrl Enter adds it" + (" · Shift Enter adds another" if another else "")
                 + " · Esc closes").classes("sb-subtitle")
    # The body first, then one toolbar: what the note says is the field a
    # person came to type in, and the rest are settings on it.
    with ui.element("div").classes("sb-composer"):
        body = ui.textarea(placeholder="Write a note in markdown — #tag adds a tag, "
                                       "!task sets the kind") \
            .props('borderless autogrow autofocus aria-label="body"') \
            .classes("w-full sb-compose-body").mark("note-body")
        with ui.row().classes("items-center gap-2 w-full"):
            kind = ui.select(labels, value="note" if "note" in labels else labels[0]) \
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
            name = (on_name.value or "").strip() if on_type.value != "project" else None
            if name == "":
                ui.notify(f"name the {on_type.value} this note is on", type="warning")
                return
            row = {"kind": kind.value,
                   "target": {"type": on_type.value, "name": name},
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
