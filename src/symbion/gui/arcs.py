"""Arc index and per-object checklist. Toggling a row supersedes its
task with the flipped status; the superseding row's created_at is the
'when', and its author is the human who ticked it."""
from __future__ import annotations

from urllib.parse import quote

from nicegui import ui

from .. import api
from .. import store as S
from .. import summary as summ
from .filters import href
from .notes import (Card, _due, ago, bare, demo_says, edit_dialog, kind_class, reading,
                    refusal_shown, short_id, target_href, toggle_star)


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


def checklist(ctx, arc_id: str, refresh, *, author: str, seen=None, limit=None) -> int:
    """Head tasks for an arc, not-done first, then by target name, then in
    the order written. Each row carries its clipped body: six boxes on one
    file drew six identical target lines (2026-09-27). A row is the
    notebook's card: its box the card's resolve box on the right, ticked
    when done, and the card's star and edit, due date and tags (on the left
    with none of those, a starred box looked like any other: the user,
    2026-10-27). A bare row (a seeded box) is one line led by its target,
    and the kind shows only when the arc holds more than one (every row said
    `task`). `seen` is the page's notes.reading. Each box is one Card, as a
    note is: as server elements a seeded arc of a few hundred boxes took a
    second or more to draw (2026-10-30). `limit` caps the boxes drawn, open
    first: 2,000 boxes took 0.59 s (audit 2026-11-02). Returns how many it
    left out."""
    items = S.arc_items((seen or reading(ctx)).loaded, arc_id)
    items.sort(key=lambda n: (S.read_status(n) == "resolved",
                              (n.target.name or "").lower(), S.written_at(n), n.id))
    if not items:
        ui.label("no targets yet — seed a scope above").classes("text-muted")
        return 0
    mixed = len({fu.kind for fu in items}) > 1
    for fu in items[:limit]:
        _box(ctx, fu, refresh, author=author, mixed=mixed)
    return len(items[limit:]) if limit is not None else 0


def _box(ctx, fu, refresh, *, author: str, mixed: bool) -> None:
    done = S.read_status(fu) == "resolved"
    t = fu.target
    # `project` has no object page, so its box links to the note itself: the
    # only way into a project note filed under an arc.
    to = (href(id=fu.id), "", "project") if t.type == "project" or not t.name \
        else (target_href(t.type, t.name), t.type, t.name)
    foot = [{"head": True, "h": to[0], "type": to[1], "t": to[2],
             "m": ["note-headline", "note-target"]}] if bare(fu) else []
    if fu.due:
        text, cls, tip = _due(fu, S.read_status(fu))
        foot.append({"t": text, "c": cls, "tip": tip, "m": "note-due"})
    if mixed:
        foot.append({"t": fu.kind, "c": f"sb-chip sb-kindchip {kind_class(fu.kind)}",
                     "m": "checklist-kind"})
    if not bare(fu):
        foot.append({"t": f"{to[1]}: {to[2]}" if to[1] else to[2], "h": to[0],
                     "c": "sb-chip sb-target", "m": "note-target"})
    foot += [{"t": f"#{g}", "h": href(tag=g), "c": "sb-tag", "m": f"tag-{g}"} for g in fu.tags]
    # The text is the link to the whole note, clipped or not: only the id
    # linked, and a reader did not find it (GitHub issue 1).
    main = [] if bare(fu) else [{"k": "text", "t": summ.clip(fu.body, summ.HEAD_CHARS),
                                 "h": href(id=fu.id), "m": "checklist-body"}]
    card = {"main": main, "foot": foot,
            # the tick's row: its author ticked it, then
            "by": {"t": fu.author, "h": href(author=fu.author)} if done else None,
            "age": {"t": ago(fu.created_at), "tip": S.shown(fu.created_at)} if done else None,
            "sid": {"t": short_id(fu.id), "h": href(id=fu.id), "tip": f"{fu.id}\nthis entry alone"},
            "resolve": "reopen" if done else "resolve", "star": "priority" in fu.tags}
    el = Card(card, []).classes(f"sb-note w-full {kind_class(fu.kind)} sb-has-actions sb-resolvable"
                                + (" sb-starred" if "priority" in fu.tags else "")
                                + (" sb-done" if done else "") + (" sb-box" if bare(fu) else "")) \
        .mark("note-row", "checklist-row")
    el._props["data-id"] = fu.id   # one prop: .props() would parse the id

    def toggle():
        if not done and fu.spec.verdict:
            # A pre-registration closes with its verdict; the store refuses a
            # bare resolve, so the tick opens the dialog.
            edit_dialog(ctx, fu, refresh, author=author, resolving=True)
            return
        if demo_says("reopens the box" if done else "resolves the box"):
            return
        with refusal_shown():
            api.supersede(ctx, fu.id, author=author, status="open" if done else "resolved")
            refresh()

    def act(e) -> None:
        # As render_note's: a name this box does not offer does nothing.
        what = (e.args if isinstance(e.args, list) else [e.args])[:1]
        if what == ["resolve"]:
            toggle()
        elif what == ["star"]:
            toggle_star(ctx, fu, refresh, author=author)
        elif what == ["edit"]:
            edit_dialog(ctx, fu, refresh, author=author)
        elif what == ["copy"]:
            ui.clipboard.write(fu.id)
            ui.notify(f"copied {fu.id}")

    el.on("act", act)


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
            if demo_says("makes the arc"):
                return
            with refusal_shown():
                act = api.create_arc(ctx, name.value, desc.value or "",
                                     scope.value, author=author)
                ui.navigate.to(f"/arc?id={quote(act.id)}")

        create.on_click(_create)

    for a in active:
        done, total = S.arc_progress(notes, a.id)
        with ui.element("div").classes("sb-card sb-arc sb-filterable w-full").mark("arc-card"):
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
