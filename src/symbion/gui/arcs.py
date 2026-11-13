"""Arc index and per-object checklist. Toggling a row supersedes its
task with the flipped status; the superseding row's created_at is the
'when', and its author is the human who ticked it."""
from __future__ import annotations

import json
from urllib.parse import quote

from nicegui import ui

from .. import api
from .. import store as S
from .. import summary as summ
from .chrome import own_write
from .filters import href
from .notes import (Card, _due, ago, bare, demo_says, edit_dialog, heading, kind_class,
                    reading, refusal_shown, short_id, target_href, toggle_star)


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


def checklist(ctx, act, refresh, *, author: str, seen=None, limit=None) -> int:
    """Head tasks for an arc in board order (store.arc_board: each phase's
    boxes, then done; write order where none is placed). Each row carries its clipped body: six boxes on one
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
    board = S.arc_board((seen or reading(ctx)).loaded, act)
    items = [(fu, None if name == S.DONE else rows) for name, rows in board for fu in rows]
    if not items:
        ui.label("no targets yet — seed a scope above").classes("text-muted")
        return 0
    mixed = len({fu.kind for fu, _ in items}) > 1
    # One container, so chrome's _BOARD_JS knows a list row from the arc's other cards.
    with ui.column().classes("sb-list w-full").mark("arc-list"):
        for fu, col in items[:limit]:
            _box(ctx, act, fu, col, refresh, author=author, mixed=mixed)
    return len(items[limit:]) if limit is not None else 0


def scope_chip(act) -> None:
    """The arc's scope, which `arc seed` sweeps, named as one; none for
    `mixed`, the default, which says nothing. A bare `mixed` read as the
    arc's target (a browser agent's review, 2026-11-11)."""
    if act.target_scope != "mixed":
        ui.label(f"scope: {act.target_scope}").classes("sb-chip").mark("arc-scope") \
            ._props["data-tip"] = "what this arc's entries are about; `arc seed` adds a box per name"


def board(ctx, act, refresh, *, author: str, seen=None, limit=None) -> None:
    """The arc's columns side by side, in board order (store.arc_board): each
    phase, or `open` on an arc without phases, then done. Each box is the
    checklist's card, draggable: chrome's _BOARD_JS moves it in the page and
    sends the drop as `sbMove`, which the arc page writes. Its foot names who
    last moved it and when. A column is a board: its phase and count, then
    `limit` cards and how many it left out, as a page says it. Parked
    entries are never boxes: a link under the board names them."""
    rd = seen or reading(ctx)
    cols = S.arc_board(rd.loaded, act)
    mixed = len({fu.kind for _, rows in cols for fu in rows}) > 1
    held = {p["id"]: p for p in act.placement or ()}
    with ui.element("div").classes("sb-board sb-phases").mark("arc-board"):
        for name, rows in cols:
            with ui.element("div").classes("sb-phase").mark("board-column") as column:
                column._props["data-phase"] = name or "open"    # one prop, as a box's id
                with ui.element("div").classes("sb-board-head"):
                    heading(2, name or "open").classes("sb-phase-name")
                    ui.label(str(len(rows))).classes("sb-count").mark("board-count")
                shown = rows[:limit]
                with ui.element("div").classes("sb-phase-cards").mark("board-cards"):
                    for fu in shown:
                        _box(ctx, act, fu, None if name == S.DONE else rows, refresh,
                             author=author, mixed=mixed, placed=held.get(fu.id, {}))
                if len(rows) > len(shown):
                    more = f"+{summ._count(len(rows) - len(shown), 'more entry')} not shown"
                    (ui.label(more) if S.read_only()
                     else ui.link(f"{more} — show all", f"{target_href('arc', act.id)}&all=1")) \
                        .classes("sb-board-aside").mark("board-more")
    parked = [n for n in S.heads(rd.loaded) if n.arc_id == act.id and n.spec.parked]
    if parked:
        # The arc's entries, narrowed to the parked kind when there is one.
        kinds = {n.kind for n in parked}
        ui.link(f"{len(parked)} parked",
                href(arc=act.id, kind=kinds.pop() if len(kinds) == 1 else None)) \
            .classes("sb-board-aside").mark("board-parked")


def phase_bar(bar, notes, act) -> None:
    """Fill `bar`, an arc's progress, with its boxes by phase: done first, then
    the board's columns right to left, each as wide as its count and in its
    column strip's colour (theme: .sb-phase-seg). A segment's tip names its
    phase and count; a screen reader reads the whole as one progressbar."""
    cols = S.arc_board(notes, act)
    done, total = len(cols[-1][1]), sum(len(rows) for _, rows in cols)
    rest = ", ".join(f"{name or 'open'} {len(rows)}" for name, rows in cols[-2::-1] if rows)
    bar.clear()
    bar._style["--sb-cols"] = str(len(cols))
    bar._props.update({"role": "progressbar", "aria-valuemin": 0, "aria-valuemax": total,
                       "aria-valuenow": done,
                       "aria-valuetext": f"{done} of {total} done" + (f"; {rest}" if rest else "")})
    with bar:
        for i, (name, rows) in reversed(list(enumerate(cols, 1))):
            if rows:
                seg = ui.element("div").classes("sb-phase-seg").mark("arc-bar-seg") \
                    .style(f"flex: {len(rows)} 1 0; --i: {i}")
                seg._props["data-phase"] = name or "open"
                seg._props["data-tip"] = f"{name or 'open'} {len(rows)}"
    bar.update()


def _when(stamp: str) -> str:
    """`ago` as words after a verb: `3h ago`, `just now`, or a date."""
    t = ago(stamp)
    return "just now" if t == "now" else t if len(t) == 10 and t[4] == "-" else f"{t} ago"


def when_line(line, notes, act) -> None:
    """Fill `line` with the arc's age and its last change, each with its full
    stamp as the tip, so a glance at Arcs tells a moving arc from a quiet one.
    A last change that is the arc's creation is not said twice."""
    at, by = S.arc_last_change(notes, act)
    line.clear()
    with line:
        ui.label(f"started {_when(act.created_at)} by {act.author}") \
            ._props["data-tip"] = S.shown(act.created_at)
        if (at, by) != (act.created_at, act.author):
            ui.label("·").classes("sb-sep")
            ui.label(f"last change {_when(at)}" + (f" by {by}" if by else "")) \
                .mark("arc-last-change")._props["data-tip"] = S.shown(at)


def redraw_box(ctx, arc_id: str, nid: str, refresh, *, author: str) -> None:
    """After a drop the browser has drawn and the store took: the cards of the
    column it left and the column it entered made again as the store now has
    them, so the box's tick, chip and foot say what the store says, and so do
    its neighbours' up and down: with the box alone redrawn, a neighbour's
    `down` moved it up. The other columns are not redrawn: a redrawn board
    flickered and lost its scroll place. With no board, or the box gone from
    it, `refresh`. Only the board's own elements: the peek and the entries
    about the arc draw cards with the same id."""
    board = next((e for e in ui.context.client.elements.values()
                  if "arc-board" in e._markers), None)
    els = list(board.descendants()) if board else []
    act = next((a for a in S.load_arcs(ctx.store_dir) if a.id == arc_id), None)
    cols = S.arc_board(reading(ctx).loaded, act) if act else []
    name, rows = next(((n, r) for n, r in cols if any(b.id == nid for b in r)), (None, []))
    column = next((e for e in els if "board-column" in e._markers
                   and e._props.get("data-phase") == (name or "open")), None)
    into = column and next(c for c in column.default_slot.children if "board-cards" in c._markers)
    if not rows or into is None:
        refresh()
        return
    # The server's tree still holds the card where it was drawn: its column is the one it left.
    left = [e.parent_slot.parent for e in els if isinstance(e, Card) and e._props.get("data-id") == nid]
    by_phase = {n or "open": r for n, r in cols}
    mixed = len({fu.kind for _, r in cols for fu in r}) > 1
    held = {p["id"]: p for p in act.placement or ()}
    for box in dict.fromkeys([into, *left]):
        phase = box.parent_slot.parent._props.get("data-phase")
        drawn = {c._props.get("data-id") for c in box.default_slot.children} - {nid}
        if box is into:
            drawn.add(nid)
        box.clear()
        col = by_phase.get(phase, [])
        with box:
            for fu in col:
                if fu.id in drawn:
                    _box(ctx, act, fu, None if phase == S.DONE else col, refresh,
                         author=author, mixed=mixed, placed=held.get(fu.id, {}))
        # Its count too: it read as the page drew it (audit, 2026-11-08).
        next(e for e in box.parent_slot.parent.descendants() if "board-count" in e._markers) \
            .set_text(str(len(col)))


def _moved(act, placed: dict) -> dict | None:
    """Who last moved a board box, and when; None for a box nobody moved,
    whose "not moved" on every card said nothing (the user, 2026-11-07). On a
    phased board the column names where the box is, so a narrow card keeps
    where it came from; the tip has the whole move."""
    if not (placed.get("by") and placed.get("at")):
        return None         # an order-only placement holds a place and names no mover
    how = f"{placed['from']} → {placed['phase']}" if placed.get("from") else "moved"
    whole = f"{how} · {placed['by']} · {ago(placed['at'])}"
    short = f"from {placed['from']}" if placed.get("from") else "moved"
    return {"t": f"{short} · {ago(placed['at'])}" if act.phases else whole,
            "tip": f"{whole}\n{S.shown(placed['at'])}", "m": "board-moved"}


def _moves(act, fu, col, placed: dict) -> dict | None:
    """A board box's last line: up and down in its column, and on a phased arc
    left and right to the phase beside, then who moved it. An arrow that
    cannot move is drawn disabled, so each keeps its place card to card. The
    "order" chip read oddly as a control (the user, 2026-11-07). The demo
    draws no arrows; None when there is nothing to draw."""
    arrows = []
    if col is not None and not S.read_only():
        i, ph = col.index(fu), list(act.phases or ())
        here = ph.index(S.box_phase(act, fu)) if ph else 0
        prev, nxt = (ph[here - 1] if here else None,
                     ph[here + 1] if here + 1 < len(ph) else None) if ph else (None, None)

        def arrow(d, k, ok, tip, to=""):
            return {"d": d, "k": k, "p": to, "ok": ok, "tip": tip, "m": f"board-{d}"}
        arrows = [arrow("up", "up", i > 0, "move up"),
                  arrow("down", "down", i < len(col) - 1, "move down")]
        if ph:
            arrows = [arrow("left", "phase", prev is not None,
                            f"move to {prev}" if prev else "the first phase", prev or ""),
                      *arrows,
                      arrow("right", "phase", nxt is not None,
                            f"move to {nxt}" if nxt else "the last phase: resolve the box to finish it",
                            nxt or "")]
    moved = _moved(act, placed)
    return {"arrows": arrows, "moved": moved} if arrows or moved else None


def _box(ctx, act, fu, col, refresh, *, author: str, mixed: bool, placed=None) -> Card:
    """`col` is the open column the box sits in (None once done): up and down
    move it past its neighbour there. `placed` is the box's placement on a
    board ({} for none), None in the list."""
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
    if col is not None and not S.read_only() and placed is None:
        # After the kind, as a card's status chip: the List's moves, for a
        # keyboard or a finger, which never drags (the design system's
        # alignment, 2026-11-07). A board card moves by its arrows.
        i = col.index(fu)
        steps = [{"t": w, "k": w} for w, ok in (("up", i > 0), ("down", i < len(col) - 1)) if ok]
        menu = [*({"t": p, "k": "phase"} for p in act.phases or ()), *steps]
        if act.phases:
            foot.append({"t": S.box_phase(act, fu), "c": "sb-chip", "m": "checklist-phase",
                         "tip": "move to a phase", "menu": menu})
        elif steps:
            foot.append({"t": "order", "c": "sb-chip", "m": "checklist-order",
                         "tip": "move up or down", "menu": steps})
    if not bare(fu):
        foot.append({"t": f"{to[1]}: {to[2]}" if to[1] else to[2], "h": to[0],
                     "c": "sb-chip sb-target", "m": "note-target"})
    foot += [{"t": f"#{g}", "h": href(tag=g), "c": "sb-tag", "m": f"tag-{g}"} for g in fu.tags]
    # The text is the link to the whole note, clipped or not: only the id
    # linked, and a reader did not find it (GitHub issue 1).
    main = [] if bare(fu) else [{"k": "text", "t": summ.clip(fu.body, summ.HEAD_CHARS),
                                 "h": href(id=fu.id), "m": "checklist-body"}]
    # The list drags an open row by its grip, so its words still select (the
    # phases design's List); a board card drags whole. The demo drags too, so
    # a visitor sees it; its drop only says what it does (pages._moved).
    grip = placed is None and col is not None
    card = {"main": main, "foot": foot, "grip": grip,
            "move": None if placed is None or done else _moves(act, fu, col, placed),
            # the tick's row: its author ticked it, then
            "by": {"t": fu.author, "h": href(author=fu.author)} if done else None,
            "age": {"t": ago(fu.created_at), "tip": S.shown(fu.created_at)} if done else None,
            "sid": {"t": short_id(fu.id), "h": href(id=fu.id),     # its age's stamp, as notes.py's
                    "tip": f"{fu.id}\n" + (f"written {S.shown(fu.created_at)}\n" if done else "")
                    + "open this entry on its own page"},
            "resolve": "reopen" if done else "resolve", "star": "priority" in fu.tags}
    el = Card(card, []).classes(f"sb-note w-full {kind_class(fu.kind)} sb-has-actions sb-resolvable"
                                + (" sb-starred" if "priority" in fu.tags else "")
                                + (" sb-done" if done else "") + (" sb-box" if bare(fu) else "")) \
        .mark("note-row", "checklist-row")
    el._props["data-id"] = fu.id   # one prop: .props() would parse the id
    if placed is not None:
        el._props["draggable"] = "true"
    if grip:            # a drop beside this row takes its phase: `open` on an arc without
        el._props["data-phase"] = S.box_phase(act, fu) or "open"

    def toggle():
        if not done and fu.spec.verdict:
            # A pre-registration closes with its verdict; the store refuses a
            # bare resolve, so the tick opens the dialog.
            edit_dialog(ctx, fu, refresh, author=author, resolving=True)
            return
        if demo_says("reopens the box" if done else "resolves the box"):
            return
        client = ui.context.client      # the redraw deletes this card's slot
        with refusal_shown():
            with own_write(ctx, author, [fu.id]):
                api.supersede(ctx, fu.id, author=author, status="open" if done else "resolved")
            refresh()
        # As a move: the redraw dropped focus at the page's top (the GUI review, 2026-11-07).
        client.run_javascript(f'sbFocusMove({json.dumps(fu.id)}, ".sb-resolve")')

    def move(how: str, name: str) -> None:
        if demo_says("moves the box"):
            return
        i = col.index(fu) if col is not None else 0
        to = {"phase": {"phase": name}, "up": {"before": col[max(i - 1, 0)].id},
              "down": {"after": col[min(i + 1, len(col) - 1)].id}}.get(how)
        if to is None:
            return
        client = ui.context.client      # the redraw deletes this card's slot
        with refusal_shown():
            with own_write(ctx, author):
                api.move_box(ctx, fu.id, author=author, **to)
            refresh()
        # The redraw dropped keyboard focus at the page's top, and the move
        # menu is the keyboard's way to move a box: focus goes back to its card.
        # On a board, to the arrow pressed, else one still enabled: ↓ to the
        # column's foot disables it.
        if placed is None:
            client.run_javascript(f'sbFocusMove({json.dumps(fu.id)})')
            return
        if how == "phase":
            ph = list(act.phases or ())
            how = "right" if name in ph and ph.index(name) > ph.index(S.box_phase(act, fu)) \
                else "left"
        client.run_javascript(f'sbFocusMove({json.dumps(fu.id)}, ".sb-mv-{how}:not(.disabled)", '
                              f'".sb-move .q-btn:not(.disabled)")')

    def on_act(e) -> None:
        # As render_note's: a name this box does not offer does nothing.
        args = e.args if isinstance(e.args, list) else [e.args]
        what = args[:1]
        if what == ["move"] and col is not None and not S.read_only() and len(args) == 3:
            move(args[1], args[2])
        elif what == ["resolve"]:
            toggle()
        elif what == ["star"]:
            client = ui.context.client
            with own_write(ctx, author, [fu.id]):
                toggle_star(ctx, fu, refresh, author=author)
            client.run_javascript(f'sbFocusMove({json.dumps(fu.id)}, ".sb-star")')
        elif what == ["edit"]:
            edit_dialog(ctx, fu, refresh, author=author)
        elif what == ["copy"]:
            ui.clipboard.write(fu.id)
            ui.notify(f"copied {fu.id}")

    el.on("act", on_act)
    return el


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
            scope = ui.select(sorted(ctx.arc_scopes), value="mixed",
                              label="scope").props("dense outlined options-dense") \
                .classes("min-w-[120px]").mark("new-arc-scope")
            desc = ui.input("description").props("dense outlined").classes("col") \
                .style("min-width:160px")
            # Outlined, as an answer's buttons are: New entry is the page's one
            # filled button (the GUI review, 2026-11-07).
            create = ui.button("Create arc", color=None).props("outline") \
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

    # the most recently changed first: moving arcs rise, quiet ones sink
    last = {a.id: S.written_at(None, S.arc_last_change(notes, a)[0]) for a in active}
    for a in sorted(active, key=lambda a: last[a.id], reverse=True):
        done, total = S.arc_progress(notes, a.id)
        finished = bool(total) and done == total
        with ui.element("div").classes("sb-card sb-arc sb-filterable w-full" +
                                       (" sb-done" if finished else "")).mark("arc-card"):
            with ui.row().classes("items-baseline gap-2 w-full"):
                ui.link(a.name, f"/arc?id={quote(a.id)}").classes("sb-arc-name")
                scope_chip(a)
                ui.space()
                ui.label(f"{done}/{total}" + (" done" if finished else "")).classes("sb-count") \
                    .mark("arc-count")
            phase_bar(ui.element("div").classes("sb-phase-bar").mark("arc-bar"), notes, a)
            when_line(ui.row().classes("sb-arc-when sb-note-meta").mark("arc-when"), notes, a)
            if a.description:
                ui.label(a.description).classes("sb-subtitle")

    if archived:
        with ui.expansion(f"archived ({len(archived)})").classes("w-full sb-card"):
            for a in archived:
                with ui.row().classes("items-center gap-2"):
                    ui.link(a.name, f"/arc?id={quote(a.id)}").classes("text-body") \
                        .mark("archived-arc")
                    ui.label(f"archived {S.shown(a.archived_at) if a.archived_at else '—'}").classes("sb-note-meta")
