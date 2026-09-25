"""The note row, reused by every surface. Colors are CSS classes from theme.py
-- no hexes here, the hex audit forbids them outside theme.py.

Refreshables are defined PER call (per page/client). A module-level
@ui.refreshable accumulates instances across pages and its .refresh() then
touches a deleted client (RuntimeError 'client has been deleted').
"""
from __future__ import annotations

import re
from urllib.parse import quote

from nicegui import ui

from .. import api, gitref
from .. import summary as summ
from .. import store as S
from .filters import href

# Bodies carry code-style identifiers (snake_case_module). markdown2's default
# emphasis matches intra-word underscores and mangles them to <em>;
# `code-friendly` keeps *asterisk* emphasis and disables underscore emphasis.
NOTE_MD_EXTRAS = ["fenced-code-blocks", "tables", "code-friendly"]


def chip_class(spec) -> str:
    """Colour by bits, not label: a verdict kind takes the check colour, a
    status kind the task colour, parked and plain none. `decision` loses its
    own chip and reads like `note`; a per-kind colour is a later
    decision, not this module's."""
    if spec.verdict:
        return "sb-chip-check"
    if spec.parked:
        return ""
    if spec.status:
        return "sb-chip-task"
    return ""


def target_link(n) -> None:
    """Where this note points. Arcs go to their checklist; everything
    else to the ref-aware object view."""
    if n.target.type == "project" or not n.target.name:
        # `project` has no object page -- /object redirects when name is empty --
        # so this is the one row whose target cannot stand in for the note. It
        # links to the note itself, which is also the only way into a project
        # note filed under an arc: the checklist shows nothing else.
        ui.link("· project", href(id=n.id)).classes("sb-note-meta") \
            .mark("note-target")
        return
    if n.target.type == "arc":
        ui.link(f"arc: {n.target.name}",
                f"/arc?id={quote(n.target.name)}").classes("text-body") \
            .mark("note-target")
        return
    ui.link(f"{n.target.type}: {n.target.name}",
            f"/object?type={quote(n.target.type)}&name={quote(n.target.name, safe='')}") \
        .classes("text-body").mark("note-target")


def _check_badge(ctx, n) -> None:
    state, distance = gitref.check_state(ctx.cfg, n.provenance)
    text = f"{state} {distance}" if state == "behind" else state
    if state == "unverifiable":
        text = f"unverifiable — {api.why_unverifiable(n.provenance)}"
    cls = {"current": "sb-chip-good", "behind": "sb-chip-notable",
           "diverged": "sb-chip-bad", "unverifiable": "sb-chip-bad"}[state]
    ui.label(text).classes(f"sb-chip {cls}").mark("check-state")


def _due_chip(n, status) -> None:
    """Words while the row can still fire, the bare date once it cannot: a
    resolved row is never late."""
    state = S.due_state(n.due) if status == "open" else None
    if state is None:
        ui.label(f"due {n.due}").classes("sb-chip").mark("note-due")
        return
    past, days = state
    cls = "sb-chip-bad" if past else "sb-chip-notable" if days <= summ.DUE_DAYS else ""
    ui.label(summ.due_phrase(state)).classes(f"sb-chip {cls}").tooltip(n.due) \
        .mark("note-due")


def render_note(ctx, n, refresh, *, author: str, show_target: bool = False) -> None:
    """One note row. Every chip is a link into the filtered view -- that is the
    browse mechanism, not a decoration."""
    with ui.element("div").classes("sb-note w-full").mark("note-row"):
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.link(n.kind, href(kind=n.kind)) \
                .classes(f"sb-chip {chip_class(n.spec)}").mark("note-kind")
            status = S.read_status(n)
            if status:
                ui.link(status, href(status=status)).classes(f"sb-chip sb-chip-{status}")
            if n.due:
                _due_chip(n, status)
            if n.spec.verdict:
                _check_badge(ctx, n)
            ui.label(n.created_at.replace("T", " ")).classes("sb-note-meta")
            ui.link(n.author, href(author=n.author)).classes("sb-note-meta")
            if show_target:
                target_link(n)
            for t in n.tags:
                ui.link(f"#{t}", href(tag=t)).classes("sb-chip").mark(f"tag-{t}")
            for r in n.refs:
                ui.link(f"↗ {r.type}:{r.name}",
                        f"/object?type={quote(r.type)}&name={quote(r.name or '', safe='')}") \
                    .classes("sb-chip").mark("note-ref")
            ui.label(n.id).classes("sb-note-meta")
            ui.space()
            has_pri = "priority" in n.tags

            def _star(n=n, has_pri=has_pri):
                api.retag(ctx, n.id, add=() if has_pri else ("priority",),
                          rm=("priority",) if has_pri else (), author=author)
                refresh()

            ui.button(icon="star" if has_pri else "star_outline", on_click=_star) \
                .props("flat dense round" + (" color=warning" if has_pri else "")) \
                .tooltip("toggle #priority").mark("note-star")

            if n.spec.status and S.read_status(n) == "open":
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
                    .props("flat dense round").tooltip("resolve").mark("note-resolve")

            ui.button(icon="edit", on_click=lambda n=n: edit_dialog(ctx, n, refresh,
                                                                    author=author)) \
                .props("flat dense round").tooltip("supersede / edit").mark("note-edit")
        if n.spec.verdict:
            ui.label(f"checked: {n.checked or '—'}   →   result: {n.result or '—'}") \
                .classes("sb-note-meta")
        if n.measurements:
            with ui.row().classes("gap-3 flex-wrap").mark("measurements"):
                for k, v in n.measurements.items():
                    ui.label(f"{k}: {v}").classes("sb-note-meta")
        if n.evidence:
            # Text only. Serving store-relative paths over HTTP buys nothing a
            # file manager does not, and makes the store a static file host.
            ui.label("evidence: " + ", ".join(n.evidence)).classes("sb-note-meta")
        if n.body:
            ui.markdown(n.body, extras=NOTE_MD_EXTRAS)   # sanitize=True default


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


def arc_options(ctx) -> dict:
    """{id: label} for a select, unarchived only. An archived arc is a
    closed ticket; offering it invites reopening one by accident."""
    opts = {"": "— none —"}
    for a in S.load_arcs(ctx.store_dir):
        if not a.archived:
            opts[a.id] = a.name
    return opts


def add_form(ctx, target_type, target_name, refresh, *, author: str) -> None:
    """Writes go through api.add, which canonicalizes the target name, stamps
    provenance on checks, and takes author keyword-only."""
    kinds = ctx.kinds
    labels = list(kinds)
    with ui.element("div").classes("w-full gap-1"):
        with ui.row().classes("items-center gap-2 w-full"):
            kind = ui.select(labels, value="note" if "note" in labels else labels[0]) \
                .props("dense outlined").mark("note-kind-select")
            arc = ui.select(arc_options(ctx), value="", label="arc") \
                .props("dense outlined").classes("min-w-[180px]") \
                .mark("note-arc-select")
            checked = ui.input("checked").props("dense outlined") \
                .mark("note-checked")
            result = ui.input("result").props("dense outlined") \
                .mark("note-result")
            checked.bind_visibility_from(kind, "value", backward=lambda v: kinds[v].verdict)
            result.bind_visibility_from(kind, "value", backward=lambda v: kinds[v].verdict)
            due = ui.input("due", placeholder="YYYY-MM-DD").props("dense outlined") \
                .mark("note-due")
            due.bind_visibility_from(kind, "value", backward=lambda v: kinds[v].status)
        body = ui.textarea("body (markdown) — #hashtags become tags") \
            .props("outlined").classes("w-full").mark("note-body")
        tags = ui.input("tags").props("dense outlined").classes("w-full") \
            .mark("note-tags")

        # Harvested tags are ADDED to the field, never silently replace what was
        # typed there; `seen` is what the last harvest contributed, so a tag the
        # author deleted by hand does not reappear on the next keystroke.
        seen: list = []

        def _sync():
            found, _ = api.harvest_hashtags(body.value or "")
            kept = [x for x in split_tags(tags.value) if x not in seen or x in found]
            tags.value = ", ".join(dict.fromkeys(found + kept))
            seen[:] = found

        body.on_value_change(lambda _: _sync())

        def _submit():
            if not (body.value or "").strip() and not kinds[kind.value].verdict:
                ui.notify("empty note", type="warning")
                return
            harvested, cleaned = api.harvest_hashtags(body.value or "")
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

        ui.button("add note", on_click=_submit) \
            .props("unelevated dense color=primary").mark("note-add")
