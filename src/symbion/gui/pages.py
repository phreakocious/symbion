"""Route registration. Each route re-reads the store: the GUI holds no
snapshot, so a page is stale only in the browser and any navigation fixes it."""
from __future__ import annotations

from fastapi import Request
from nicegui import ui

from .. import api
from .. import store as S
from .. import summary as summ
from . import arcs, filters
from .chrome import shell
from .notes import add_form, render_note


def _board(ctx, title, rows, author) -> None:
    with ui.element("div").classes("sb-card w-full gap-1"):
        ui.label(f"{title} ({len(rows)})").classes("sb-stat-label")
        if not rows:
            ui.label("none").classes("text-muted")
        for n in rows:
            render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                        show_target=True)


def build_page(ctx, *, author: str) -> None:
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
            for label, k in ctx.kinds.items():
                if k.status and not k.parked:
                    _board(ctx, f"open {label}", summ.open_outside_arcs(newest, label), author)
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
        shell(ctx, author, [("home", "/"), ("notes", None)])
        heads = S.heads(S.load(ctx.store_dir))
        rows = sorted(S.query(heads, **kwargs),
                      key=lambda n: n.created_at, reverse=True)
        with ui.column().classes("sb-main w-full gap-4"):
            ui.label(filters.describe(kwargs)).classes("sb-title")
            ui.label(f"{len(rows)} notes").classes("sb-subtitle").mark("result-count")
            if kwargs:
                ui.link("clear filters", "/notes").classes("text-body")
            for n in rows:
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            show_target=True)

    @ui.page("/tags")
    def tags_page():
        shell(ctx, author, [("home", "/"), ("tags", None)])
        counts = S.tag_counts(S.load(ctx.store_dir))
        with ui.column().classes("sb-main w-full gap-2"):
            ui.label("Tags").classes("sb-title")
            ui.label(f"{len(counts)} in use — near-synonyms are drift, not vocabulary") \
                .classes("sb-subtitle")
            for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                with ui.row().classes("items-center gap-2"):
                    ui.link(f"#{tag}", filters.href(tag=tag)).classes("sb-chip")
                    ui.label(str(n)).classes("sb-note-meta")

    @ui.page("/object")
    def object_page(type: str = "", name: str = ""):
        # Guard BEFORE the shell: rendering a header for ": " and then
        # navigating away paints a frame of a page that does not exist.
        if not type or not name:
            ui.navigate.to("/notes")
            return
        shell(ctx, author, [("home", "/"), (f"{type}: {name}", None)])
        rows = S.heads_for(ctx.store_dir, type, name)
        with ui.column().classes("sb-main w-full gap-4"):
            ui.label(f"{type}: {name}").classes("sb-title")
            ui.label(f"{len(rows)} notes on or referencing this object") \
                .classes("sb-subtitle")
            with ui.element("div").classes("sb-card w-full gap-1"):
                ui.label(f"new note on {type}: {name}").classes("sb-stat-label")
                add_form(ctx, type, name, lambda: ui.navigate.reload(), author=author)
            for n in rows:
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author)

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

            body()
