"""The note row, reused by every surface. Colors are CSS classes from theme.py
-- no hexes here, the hex audit forbids them outside theme.py.

Refreshables are defined PER call (per page/client). A module-level
@ui.refreshable accumulates instances across pages and its .refresh() then
touches a deleted client (RuntimeError 'client has been deleted').
"""
from __future__ import annotations

import html
import re
from contextlib import nullcontext
from datetime import datetime
from urllib.parse import quote

from nicegui import ui

from .. import api, gitref, term
from .. import summary as summ
from .. import store as S
from .filters import href

# Bodies carry code-style identifiers (snake_case_module). markdown2's default
# emphasis matches intra-word underscores and mangles them to <em>;
# `code-friendly` keeps *asterisk* emphasis and disables underscore emphasis.
NOTE_MD_EXTRAS = ["fenced-code-blocks", "tables", "code-friendly"]

# store.new_id's shape. Bodies cite rows by id ("settled by <id>"), and the
# GUI had no way to follow one but pasting it into the search.
_NOTE_ID = re.compile(r"(?<![\w/=[-])(\d{8}-\d{6}-\d{6}-[0-9a-f]{3})(?![\w-])")
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)


def link_ids(body: str) -> str:
    """Each full note id in `body` as a markdown link to that note, except
    inside code, where a link would print as literal brackets."""
    return "".join(p if i % 2 else _NOTE_ID.sub(r"[\1](/notes?id=\1)", p)
                   for i, p in enumerate(_CODE.split(body)))


def kind_class(kind: str) -> str:
    """A kind's colour, as `list` prints it, for its chip and its row's edge;
    a kind the store declares shares one (2026-10-01; chips by bits before,
    which left `decision` and `idea` uncoloured)."""
    return f"sb-kind-{kind}" if kind in term.KIND else "sb-kind-own"


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
    if n.target.type == "arc":
        ui.link(f"arc: {n.target.name}",
                f"/arc?id={quote(n.target.name)}").classes("sb-chip sb-target") \
            .mark("note-target")
        return
    ui.link(f"{n.target.type}: {n.target.name}",
            f"/object?type={quote(n.target.type)}&name={quote(n.target.name, safe='')}") \
        .classes("sb-chip sb-target").mark("note-target")


TIP_COMMITS = 8


def check_tip(cfg, prov, state, distance) -> str:
    """What the badge was measured against. The chip lit up on hover and a
    click did nothing (the owner, 2026-09-08); `behind N` asks the reader to
    go and look at N commits, so the tip lists them."""
    sha = (prov or {}).get("sha")
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


def _check_badge(ctx, n) -> None:
    state, distance = api.verdict_state(ctx.cfg, n)
    text = f"{state} {distance}" if state in ("behind", "ahead") else state
    if state == "unverifiable":
        text = f"unverifiable — {api.why_unverifiable(n.provenance)}"
    elif state == "external":
        text = f"external — {summ.age_phrase(n.provenance['at'])}"
    cls = {"current": "sb-chip-good", "behind": "sb-chip-notable", "external": "",
           "pending": "",
           "ahead": "sb-chip-notable",
           "diverged": "sb-chip-bad", "unverifiable": "sb-chip-bad"}[state]
    chip = ui.label(text).classes(f"sb-chip {cls}").mark("check-state")
    if not (n.provenance or {}).get("sha") or state in ("external", "pending"):
        return
    # Filled on first hover: a `git log` per badge at render time doubled
    # the cost of a board of checks (45 badges: 1.5s to 2.9s).
    with chip:
        tip = ui.tooltip("")

    def _fill():
        if not tip.text:
            tip.text = check_tip(ctx.cfg, n.provenance, state, distance)

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

    ui.button(icon="content_copy", on_click=_copy) \
        .props('flat dense round size=xs aria-label="copy id"').classes("sb-copy") \
        .tooltip("copy the full id").mark("note-copy-id")


def render_note(ctx, n, refresh, *, author: str, show_target: bool = False,
                base: dict | None = None, compact: bool = False, hit=(),
                actions: bool = True) -> None:
    """One note row: a card. What it says first, then one line of where it
    points and what it is, the resolve ring on its left and star and edit
    over its corner. Every chip is a link into the filtered view -- that is
    the browse mechanism, not a decoration. `base` is the filtered view's own
    params: a chip there adds one to them, so a click narrows (the gui spec;
    it dropped them, 2026-09-25). Off a filtered view a chip starts one.
    `hit` is the search's words: a compact body then shows and marks them.
    `actions=False` drops the buttons: an earlier version is history, and an
    edit made from it would land on the current row."""
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
            _note_content(n, compact, hit)
            _note_foot(ctx, n, status, narrow, show_target)
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

                ui.button(icon="star" if has_pri else "star_outline", on_click=_star) \
                    .props("flat dense round size=sm" + (" color=warning" if has_pri else "")
                           + ' aria-label="toggle priority"') \
                    .tooltip("toggle #priority").mark("note-star")
                ui.button(icon="edit", on_click=lambda n=n: edit_dialog(ctx, n, refresh,
                                                                        author=author)) \
                    .props('flat dense round size=sm aria-label="edit"') \
                    .tooltip("supersede / edit").mark("note-edit")


def _note_content(n, compact: bool, hit) -> None:
    """The body, the verdict, measurements and evidence: what the row says."""
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
        ui.markdown(link_ids(n.body), extras=NOTE_MD_EXTRAS)   # sanitize=True default
    if n.measurements:
        with ui.row().classes("gap-3 flex-wrap").mark("measurements"):
            for k, v in n.measurements.items():
                ui.label(f"{k}: {v}").classes("sb-note-meta")
    if n.evidence:
        # Text only. Serving store-relative paths over HTTP buys nothing a
        # file manager does not, and makes the store a static file host.
        ui.label("evidence: " + ", ".join(n.evidence)).classes("sb-note-meta")


def _note_foot(ctx, n, status, narrow, show_target: bool) -> None:
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
            _check_badge(ctx, n)
        if show_target:
            target_link(n)
        if n.arc_id:
            ui.link(f"arc: {n.arc_id}", f"/arc?id={quote(n.arc_id)}") \
                .classes("sb-chip sb-target").mark("note-arc")
        for r in n.refs:
            ui.link(f"↗ {summ.ref_label(r)}",
                    f"/object?type={quote(r.type)}&name={quote(r.name or '', safe='')}") \
                .classes("sb-chip sb-ref").mark("note-ref")
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
                 else "supersede — the old row is kept").classes("sb-stat-label")
        body = ui.textarea("body", value=n.body).props("outlined") \
            .classes("min-w-[420px]").mark("edit-body")
        tags = ui.input("tags", value=", ".join(n.tags)).props("dense outlined") \
            .classes("w-full").mark("edit-tags")
        tidy_tags(tags)
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
            verdict = {"checked": checked.value or None,
                       "result": res.value or None} if checked is not None else {}
            closing = {"status": "resolved"} if resolving else {}
            dated = {"due": (due.value or "").strip() or None} if due is not None else {}
            try:
                api.supersede(ctx, n.id, author=author, body=cleaned,
                              tags=list(dict.fromkeys(split_tags(tags.value) + harvested)),
                              arc_id=arc.value or None, **verdict, **closing, **dated)
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            refresh()

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
    stayed on screen though the save dropped it (the owner, 2026-10-01)."""
    field.on("blur", lambda: field.set_value(", ".join(split_tags(field.value))))


def arc_options(ctx) -> dict:
    """{id: label} for a select, unarchived only. An archived arc is a
    closed ticket; offering it invites reopening one by accident."""
    opts = {"": "— none —"}
    for a in S.load_arcs(ctx.store_dir):
        if not a.archived:
            opts[a.id] = a.name
    return opts


def add_form(ctx, target_type, target_name, refresh, *, author: str,
             arc_id: str | None = None) -> None:
    """Writes go through api.add, which canonicalizes the target name, stamps
    provenance on checks, and takes author keyword-only. `arc_id` is the arc
    the form starts on."""
    kinds = ctx.kinds
    labels = list(kinds)
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
            tags = ui.input("tags").props("dense outlined").classes("col sb-compose-tags") \
                .style("min-width:120px").mark("note-tags")
            tidy_tags(tags)
            add = ui.button("Add note").props("unelevated color=primary").mark("note-add")

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

        def _submit():
            named, cleaned = api.harvest_kind(body.value or "", kinds)
            kind.value = named or kind.value
            harvested, cleaned = api.harvest_hashtags(cleaned)
            if not cleaned and not kinds[kind.value].verdict:   # `!task` alone says nothing
                ui.notify("empty note", type="warning")
                return
            row = {"kind": kind.value,
                   "target": {"type": target_type, "name": target_name},
                   "body": cleaned,
                   "arc_id": arc.value or None,
                   "tags": list(dict.fromkeys(split_tags(tags.value) + harvested))}
            if kinds[kind.value].verdict:
                row["checked"] = checked.value or None
                row["result"] = result.value or None
            if kinds[kind.value].status:
                row["due"] = (due.value or "").strip() or None
            try:
                api.add(ctx, row, author=author)
            except ValueError as e:            # ambiguous name, bad ref type
                ui.notify(str(e), type="negative")
                return
            body.value = checked.value = result.value = tags.value = due.value = ""
            seen.clear()
            refresh()

        add.on_click(_submit)
        add.tooltip("or ⌘/Ctrl + Enter in the body")
        body.on("keydown.meta.enter", _submit)
        body.on("keydown.ctrl.enter", _submit)
