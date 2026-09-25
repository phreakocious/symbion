"""Arc index and per-object checklist. Toggling a row supersedes its
task with the flipped status; the superseding row's created_at is the
'when', and its author is the human who ticked it."""
from __future__ import annotations

from urllib.parse import quote

from nicegui import ui

from .. import api
from .. import store as S
from .notes import target_link, edit_dialog


def seed_all_scopes(ctx) -> set:
    """Scopes a whole-catalog sweep can actually run over.

    ctx.seed_scopes carries `item` so explicit names can be seeded under it,
    but `item` has no catalog command by construction -- api.seed_names
    refuses it without names. Offering "seed all items" therefore produces a
    button whose only outcome is an error toast, which is the CLI's own
    `item scope requires at least one --name` friction rebuilt in a GUI."""
    return set(ctx.seed_scopes) - {"item"}


def seedable(ctx, kind: str = "task") -> bool:
    """Whether the arc page's "seed all" button has a kind to mint: `task`
    must be declared and eligible (status, neither parked nor verdict). A
    store without one seeds from the CLI with --kind; no selector here, since
    no store has that shape yet."""
    spec = ctx.kinds.get(kind)
    return bool(spec and spec.status and not spec.parked and not spec.verdict)


def checklist(ctx, arc_id: str, refresh, *, author: str) -> None:
    """Head tasks for an arc, not-done first then by target name."""
    items = S.arc_items(S.load(ctx.store_dir), arc_id)
    items.sort(key=lambda n: (S.read_status(n) == "resolved",
                              (n.target.name or "").lower()))
    if not items:
        ui.label("no targets yet — seed a scope above").classes("text-muted")
        return
    for fu in items:
        done = S.read_status(fu) == "resolved"

        def _toggle(fu=fu, done=done):
            if not done and fu.spec.verdict:
                # A pre-registration closes with its verdict; the store
                # refuses a bare resolve, so the tick opens the dialog.
                edit_dialog(ctx, fu, refresh, author=author, resolving=True)
                return
            api.supersede(ctx, fu.id, author=author,
                          status="open" if done else "resolved")
            refresh()

        with ui.row().classes("items-center gap-2 w-full sb-note").mark("checklist-row"):
            ui.button(icon="check_box" if done else "check_box_outline_blank",
                      on_click=_toggle) \
                .props("flat dense round" + (" color=positive" if done else "")) \
                .mark("checklist-toggle", f"toggle-{fu.id}")   # per-row: find() returns a SET
            target_link(fu)
            ui.space()
            if done:
                ui.label(fu.created_at.replace("T", " ")).classes("sb-note-meta")


def index(ctx, *, author: str) -> None:
    acts = S.load_arcs(ctx.store_dir)
    notes = S.load(ctx.store_dir)
    active = [a for a in acts if not a.archived]
    archived = [a for a in acts if a.archived]

    with ui.element("div").classes("sb-card w-full gap-2"):
        ui.label("new arc").classes("sb-stat-label")
        with ui.row().classes("items-center gap-2 w-full"):
            name = ui.input("name").props("dense outlined").mark("new-arc-name")
            scope = ui.select(sorted(ctx.arc_scopes), value="item",
                              label="scope").props("dense outlined") \
                .mark("new-arc-scope")
        desc = ui.input("description").props("dense outlined").classes("w-full")

        def _create():
            if not (name.value or "").strip():
                ui.notify("name required", type="warning")
                return
            act = api.create_arc(ctx, name.value, desc.value or "",
                                      scope.value, author=author)
            ui.navigate.to(f"/arc?id={quote(act.id)}")

        ui.button("create", on_click=_create).props("unelevated dense color=primary") \
            .mark("new-arc-create")

    for a in active:
        done, total = S.arc_progress(notes, a.id)
        with ui.element("div").classes("sb-card w-full gap-1").mark("arc-card"):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.link(a.name, f"/arc?id={quote(a.id)}").classes("text-body")
                ui.label(a.target_scope).classes("sb-chip")
                ui.space()
                ui.label(f"{done}/{total}").classes("sb-note-meta")
            ui.linear_progress(value=(done / total if total else 0.0),
                               show_value=False).props("rounded")
            if a.description:
                ui.label(a.description).classes("sb-note-meta")

    if archived:
        with ui.expansion(f"archived ({len(archived)})").classes("w-full sb-card"):
            for a in archived:
                ui.label(f"{a.name} · archived {a.archived_at or '—'}") \
                    .classes("sb-note-meta")
