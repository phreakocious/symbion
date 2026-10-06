"""Arc index and per-object checklist. Toggling a row supersedes its
task with the flipped status; the superseding row's created_at is the
'when', and its author is the human who ticked it."""
from __future__ import annotations

from urllib.parse import quote

from nicegui import ui

from .. import api
from .. import store as S
from .. import summary as summ
from .notes import ago, bare, edit_dialog, id_link, kind_class, target_headline, target_link


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
    """Head tasks for an arc, not-done first, then by target name, then in
    the order written. Each row carries its clipped body: six boxes on one
    file drew six identical target lines (2026-09-27). A row is the
    notebook's card, its box the card's resolve box, ticked when done. A
    bare row (a seeded box) is one line led by its target, and the kind
    shows only when the arc holds more than one (every row said `task`)."""
    items = S.arc_items(S.load(ctx.store_dir), arc_id)
    items.sort(key=lambda n: (S.read_status(n) == "resolved",
                              (n.target.name or "").lower(), S.written_at(n), n.id))
    if not items:
        ui.label("no targets yet — seed a scope above").classes("text-muted")
        return
    mixed = len({fu.kind for fu in items}) > 1
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

        with ui.element("div").classes(f"sb-note w-full {kind_class(fu.kind)}"
                                       + (" sb-done" if done else "")
                                       + (" sb-box" if bare(fu) else "")) \
                .props(f"data-id={fu.id}").mark("checklist-row"):
            ui.button(icon="check", on_click=_toggle) \
                .props(f'flat round dense aria-label="{"reopen" if done else "resolve"}"') \
                .classes("sb-resolve" + (" sb-resolved" if done else "")) \
                .tooltip("reopen" if done else "resolve") \
                .mark("checklist-toggle", f"toggle-{fu.id}")   # per-row: find() returns a SET
            with ui.element("div").classes("sb-note-main"):
                if not bare(fu):
                    ui.label(summ.clip(fu.body, summ.HEAD_CHARS)).classes("sb-note-text") \
                        .mark("checklist-body")
                with ui.element("div").classes("sb-note-foot"):
                    if bare(fu):
                        target_headline(fu)
                    if mixed:
                        ui.label(fu.kind).classes(f"sb-chip sb-kindchip {kind_class(fu.kind)}") \
                            .mark("checklist-kind")
                    if not bare(fu):
                        target_link(fu)
                    with ui.element("span").classes("sb-note-by"):
                        if done:      # the tick's row: its author ticked it, then
                            ui.label(fu.author).classes("sb-note-meta")
                            ui.label(ago(fu.created_at)).classes("sb-note-meta") \
                                .tooltip(S.shown(fu.created_at))
                        id_link(fu)


def index(ctx, *, author: str) -> None:
    acts = S.load_arcs(ctx.store_dir)
    notes = S.load(ctx.store_dir)
    active = [a for a in acts if not a.archived]
    archived = [a for a in acts if a.archived]

    with ui.element("div").classes("sb-composer"):
        name = ui.input(placeholder="New arc") \
            .props('borderless aria-label="name"').classes("w-full sb-compose-body") \
            .mark("new-arc-name")
        with ui.row().classes("items-center gap-2 w-full"):
            scope = ui.select(sorted(ctx.arc_scopes), value="item",
                              label="scope").props("dense outlined options-dense") \
                .classes("min-w-[120px]").mark("new-arc-scope")
            desc = ui.input("description").props("dense outlined").classes("col") \
                .style("min-width:160px")
            create = ui.button("Create arc").props("unelevated color=primary") \
                .mark("new-arc-create")

        def _create():
            if not (name.value or "").strip():
                ui.notify("name required", type="warning")
                return
            act = api.create_arc(ctx, name.value, desc.value or "",
                                      scope.value, author=author)
            ui.navigate.to(f"/arc?id={quote(act.id)}")

        create.on_click(_create)

    for a in active:
        done, total = S.arc_progress(notes, a.id)
        with ui.element("div").classes("sb-card sb-arc w-full").mark("arc-card"):
            with ui.row().classes("items-baseline gap-2 w-full"):
                ui.link(a.name, f"/arc?id={quote(a.id)}").classes("sb-arc-name")
                ui.label(a.target_scope).classes("sb-chip sb-target")
                ui.space()
                ui.label(f"{done}/{total}").classes("sb-count")
            ui.linear_progress(value=(done / total if total else 0.0),
                               show_value=False).props("rounded")
            if a.description:
                ui.label(a.description).classes("sb-subtitle")

    if archived:
        with ui.expansion(f"archived ({len(archived)})").classes("w-full sb-card"):
            for a in archived:
                with ui.row().classes("items-center gap-2"):
                    ui.link(a.name, f"/arc?id={quote(a.id)}").classes("text-body") \
                        .mark("archived-arc")
                    ui.label(f"archived {S.shown(a.archived_at) if a.archived_at else '—'}").classes("sb-note-meta")
