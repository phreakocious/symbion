"""Route registration. Each route re-reads the store: the GUI holds no
snapshot, so a page is stale only in the browser and any navigation fixes it."""
from __future__ import annotations

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from nicegui import Client, app, ui
from starlette.exceptions import HTTPException

from .. import api
from .. import store as S
from .. import summary as summ
from . import arcs, filters
from .chrome import shell
from .notes import add_form, render_note

# /notes renders this many rows before "show all": every row is a few dozen
# elements and a check a few git calls, and all 312 of one store's rows took
# 3.7s and a 1 MB page.
PAGE_ROWS = 100


def _board(ctx, title, rows, author) -> None:
    with ui.element("div").classes("sb-card w-full gap-1"):
        ui.label(f"{title} ({len(rows)})").classes("sb-stat-label")
        if not rows:
            ui.label("none").classes("text-muted")
        for n in rows:
            render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                        show_target=True, compact=True)


def _error_pages(ctx, author: str) -> None:
    """NiceGUI's own error pages: its sad-face art, and no header to get
    back from. These keep symbion's chrome and say what went wrong."""
    def body(code: int, what: str) -> None:
        shell(ctx, author, [("home", "/"), (str(code), None)])
        with ui.column().classes("sb-main w-full gap-2"):
            ui.label("no such page" if code == 404 else "this page failed") \
                .classes("sb-title").mark("error-title")
            ui.label(what).classes("sb-subtitle text-bad").mark("error-detail")
            ui.link("back to the notebook", "/").classes("text-body")

    app.on_page_exception(lambda e: body(500, f"{type(e).__name__}: {e}"))

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Exception):
        # NiceGUI's own test: a real endpoint's 404 (its assets, an API)
        # answers as FastAPI does; only a path no route matched gets a page.
        if (request.scope.get("endpoint") not in (None, app)
                and not request.scope.get("nicegui_page_path")
                and isinstance(exc, HTTPException)):
            return await http_exception_handler(request, exc)
        with Client(ui.page(""), request=request) as client:
            body(404, f"nothing is served at {request.url.path}")
        return client.build_response(request, 404)


def _history(ctx, every, head, author: str) -> None:
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
        for old in chain:
            render_note(ctx, old, lambda: None, author=author, actions=False)


def build_page(ctx, *, author: str) -> None:
    _error_pages(ctx, author)

    @ui.page("/")
    def home():
        shell(ctx, author, [("home", None)])
        heads = S.heads(S.load(ctx.store_dir))
        newest = sorted(heads, key=lambda n: n.created_at, reverse=True)
        with ui.column().classes("sb-main w-full gap-4"):
            ui.label("Notebook").classes("sb-title")
            ui.label(f"{len(heads)} notes · {ctx.store_dir}").classes("sb-subtitle")
            with ui.element("div").classes("sb-card w-full gap-1"):
                ui.label("new project note").classes("sb-stat-label")
                add_form(ctx, "project", None, lambda: ui.navigate.reload(),
                         author=author)
            bad = S.load_malformed(ctx.store_dir)
            if bad:
                ui.label(f"⚠ {len(bad)} malformed line(s) skipped — "
                         f"first at line {bad[0][0]}").classes("text-notable")
            active = {a.id for a in S.load_arcs(ctx.store_dir) if not a.archived}
            for label, k in ctx.kinds.items():
                if k.status and not k.parked:
                    _board(ctx, f"open {label}",
                           summ.open_outside_arcs(newest, label, active), author)
            _board(ctx, "priority", summ.starred(newest), author)
            for label, k in ctx.kinds.items():
                if k.verdict:
                    _board(ctx, f"recent {label}",
                           [n for n in newest if n.kind == label][:25], author)

    @ui.page("/notes")
    def notes_page(request: Request):
        # The annotation is load-bearing: unannotated, FastAPI reads
        # `request` as a required QUERY parameter and every /notes URL
        # answers 422 instead of rendering.
        params = dict(request.query_params)
        kwargs = filters.from_params(params)
        q = (params.get("q") or "").strip()
        base = {k: v for k, v in params.items() if k in filters.FILTER_KEYS and v}
        shell(ctx, author, [("home", "/"), ("notes", None)], q=q, keep=base)
        every = S.load(ctx.store_dir)
        heads = S.heads(every)
        old = kwargs.get("id")
        if old and not S.query(heads, id=old) and S.query(every, id=old):
            # A superseded id, as other rows cite them, read "0 notes": show
            # the row that replaced it, as `symbion show` does.
            kwargs["id"] = api._chain_tip(every, old).id
        rows = S.query(heads, **kwargs)
        if q:
            # An id is not text the pattern reads: a pasted one joins the
            # hits, and a superseded one brings its current row.
            rest = S.query(heads, **{k: v for k, v in kwargs.items() if k != "grep"})
            tips = {api._chain_tip(every, n.id).id for n in every if filters.id_hit(n.id, q)}
            got = {n.id for n in rows}
            rows += [n for n in rest if n.id not in got and n.id in tips]
        rows.sort(key=lambda n: (n.created_at, n.id), reverse=True)
        with ui.column().classes("sb-main w-full gap-4"):
            ui.label(filters.describe(kwargs, q)).classes("sb-title")
            if kwargs.get("id", old) != old:
                ui.label(f"{old} was superseded; this is its current row") \
                    .classes("text-notable").mark("superseded-by")
            shown = rows if params.get("all") else rows[:PAGE_ROWS]
            # The cut is named at the top, not only in the link at the end.
            cut = f"; newest {len(shown)} shown" if len(shown) < len(rows) else ""
            if q:
                # Name what was read, so a 0 is not taken for "never filed".
                ui.label(f"{len(rows)} of {summ._count(len(rest), 'note')} match{cut} — "
                         f"searched body, target, checked, result, refs and ids of "
                         f"current rows").classes("sb-subtitle").mark("result-count")
            else:
                ui.label(summ._count(len(rows), "note") + cut).classes("sb-subtitle") \
                    .mark("result-count")
            if kwargs:
                ui.link("clear filters", "/notes").classes("text-body")
            for n in shown:
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            show_target=True, base=base, compact="id" not in kwargs,
                            hit=q.split())
            if len(shown) < len(rows):
                ui.link(f"+{len(rows) - len(shown)} older not shown — show all",
                        filters.href(**base, all="1")).classes("text-body").mark("show-all")
            if "id" in kwargs and len(rows) == 1:
                _history(ctx, every, rows[0], author)

    @ui.page("/tags")
    def tags_page():
        shell(ctx, author, [("home", "/"), ("tags", None)])
        counts = S.tag_counts(S.load(ctx.store_dir))
        with ui.column().classes("sb-main w-full gap-2"):
            ui.label("Tags").classes("sb-title")
            ui.label(f"{len(counts)} in use — near-synonyms are drift, not vocabulary") \
                .classes("sb-subtitle")
            # Name the drift the subtitle warns of: `--tag` matches exactly,
            # so each group splits one subject (summ.tag_key).
            for group in summ.near_tags(counts):
                ui.label(" · ".join(f"{t} ({counts[t]})" for t in group)) \
                    .classes("sb-chip sb-chip-notable").mark("tag-near")
            # A wrapped row, not one tag per line: 79 tags were a long scroll.
            with ui.row().classes("items-center gap-2 flex-wrap"):
                for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                    ui.link(f"#{tag} {n}", filters.href(tag=tag)).classes("sb-chip")

    @ui.page("/object")
    def object_page(type: str = "", name: str = ""):
        # Guard BEFORE the shell: rendering a header for ": " and then
        # navigating away paints a frame of a page that does not exist.
        if not type or not name:
            ui.navigate.to("/notes")
            return
        shell(ctx, author, [("home", "/"), (f"{type}: {name}", None)])
        # Open work first, then the newest; a resolved row is history and
        # reads clipped (one file's page held 50 rows in full, 2026-09-27).
        rows = sorted(S.heads_for(ctx.store_dir, type, name),
                      key=lambda n: (n.created_at, n.id), reverse=True)
        live = lambda n: S.read_status(n) == "open" and not n.spec.parked   # noqa: E731
        rows.sort(key=lambda n: not live(n))      # stable: newest within each
        with ui.column().classes("sb-main w-full gap-4"):
            ui.label(f"{type}: {name}").classes("sb-title")
            ui.label(f"{len(rows)} notes on or referencing this object") \
                .classes("sb-subtitle")
            with ui.element("div").classes("sb-card w-full gap-1"):
                ui.label(f"new note on {type}: {name}").classes("sb-stat-label")
                add_form(ctx, type, name, lambda: ui.navigate.reload(), author=author)
            for n in rows:
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            compact=not live(n) and bool(n.spec.status))

    @ui.page("/arcs")
    def arcs_page():
        shell(ctx, author, [("home", "/"), ("arcs", None)])
        with ui.column().classes("sb-main w-full gap-4"):
            ui.label("Arcs").classes("sb-title")
            arcs.index(ctx, author=author)

    @ui.page("/arc")
    def arc_page(id: str = ""):
        if not id:
            ui.navigate.to("/arcs")
            return
        shell(ctx, author, [("home", "/"), ("arcs", "/arcs"), (id, None)])
        act = next((a for a in S.load_arcs(ctx.store_dir) if a.id == id), None)
        if act is None:
            ui.label(f"no arc '{id}'").classes("sb-main text-bad")
            return

        with ui.column().classes("sb-main w-full gap-4"):
            @ui.refreshable
            def body():
                a = next((x for x in S.load_arcs(ctx.store_dir) if x.id == id), None)
                if a is None:                       # archived away under us
                    ui.navigate.to("/arcs")
                    return
                done, total = S.arc_progress(S.load(ctx.store_dir), id)
                with ui.row().classes("items-center gap-3 w-full"):
                    ui.label(a.name).classes("sb-title").mark("arc-title")
                    ui.label(f"{a.target_scope} · {done}/{total}").classes("sb-subtitle")
                ui.linear_progress(value=(done / total if total else 0.0),
                                   show_value=False).props("rounded")

                with ui.row().classes("items-center gap-2 w-full sb-card"):
                    rename = ui.input("rename", value=a.name).props("dense outlined")

                    def _rename():
                        if (rename.value or "").strip():
                            api.rename_arc(ctx, id, rename.value)
                            body.refresh()

                    ui.button("save", on_click=_rename).props("flat dense")
                    ui.space()

                    def _archive():
                        api.archive_arc(ctx, id)
                        ui.navigate.to("/arcs")

                    if a.archived:
                        ui.label(f"archived {a.archived_at or ''}").classes("sb-chip")
                    else:
                        ui.button("archive", icon="archive", on_click=_archive) \
                            .props("flat dense color=warning").mark("arc-archive")

                    if a.target_scope in arcs.seed_all_scopes(ctx) and arcs.seedable(ctx):
                        def _seed():
                            try:
                                # No names: a sweep. The catalog verbatim, no
                                # resolver; api.seed raises for an empty one.
                                made = api.seed(ctx, id, a.target_scope, None, author=author)
                            except (ValueError, RuntimeError) as e:
                                ui.notify(str(e), type="negative")
                                return
                            ui.notify(f"seeded {len(made)} new target(s)")
                            body.refresh()

                        ui.button(f"seed all {a.target_scope}s", icon="playlist_add",
                                  on_click=_seed).props("flat dense").mark("arc-seed")

                arcs.checklist(ctx, id, body.refresh, author=author)

                # Notes ABOUT the arc (its target or a ref), not its boxes:
                # the page showed only the checklist (the gui spec).
                about = sorted(S.heads_for(ctx.store_dir, "arc", id),
                               key=lambda n: (n.created_at, n.id), reverse=True)
                if about:
                    ui.label("notes about this arc").classes("sb-subtitle")
                    for n in about:
                        render_note(ctx, n, body.refresh, author=author)

            body()
