"""Route registration. Each route re-reads the store: the GUI holds no
snapshot, so a page is stale only in the browser and any navigation fixes it."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from nicegui import Client, app, ui
from starlette.exceptions import HTTPException

from .. import api, gitref
from .. import store as S
from .. import summary as summ
from . import arcs, filters
from .chrome import shell
from .notes import md_button, render_note, target_href
from .theme import FONTS_DIR, FONTS_URL

# /notes renders this many rows before "show all": every row is a few dozen
# elements and a check a few git calls, and all 312 of one store's rows took
# 3.7s and a 1 MB page.
PAGE_ROWS = 100

# A "recent <kind>" board holds verdict rows this young, and older ones still
# current at HEAD. It held the 25 newest of any age: suite runs from weeks
# back, nearly all `behind`, filled it (the owner, 2026-10-02).
RECENT_DAYS = 14


def _board(ctx, title, rows, author, *, aside: str = "", more: str = "", tip: str = "",
           git_head=None, to: str = "", show_target: bool = True, since=None) -> None:
    """A heading and its cards. An empty board is its heading alone, quieter:
    a box saying "none" took a card's room to say nothing. `aside` names the
    rows of this kind the board leaves out, and `more` links the view with
    them all: beside the sidebar's count of every open row, a heading of the
    others alone read as a miscount. `to` makes the title a link. `since` is
    gitref.commits_since for a page of boards, counted once (a count per
    board took /targets from 0.37 s to 0.62 s, 2026-10-05); a card it lacks
    is counted here."""
    with ui.element("section").classes("sb-board" + ("" if rows else " sb-board-empty")) \
            .mark("board"):
        with ui.element("div").classes("sb-board-head"):
            (ui.link(title, to) if to else ui.label(title)).classes("sb-board-title") \
                .mark("board-title")
            ui.label(str(len(rows))).classes("sb-count").mark("board-count")
            if aside:
                link = ui.link(aside, more).classes("sb-board-aside").mark("board-aside")
                if tip:
                    link.tooltip(tip)
        since = since or {}
        since = since | gitref.commits_since(ctx.cfg, [n for n in rows if n.id not in since])
        for n in rows:
            render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                        show_target=show_target, compact=True, git_head=git_head,
                        since=since.get(n.id))


def _recent(ctx, n, since, git_head) -> bool:
    """Written since `since`, or current at HEAD. Only a row stamped at HEAD
    can be current, so the rest cost no git call."""
    sha = S.stamp_sha(n.provenance)
    return S.written_at(n) >= since or bool(
        sha and git_head and git_head.startswith(sha)
        and api.verdict_state(ctx.cfg, n, git_head)[0] == "current")


def _error_pages(ctx, author: str) -> None:
    """NiceGUI's own error pages: its sad-face art, and no header to get
    back from. These keep symbion's chrome and say what went wrong."""
    def body(code: int, what: str) -> None:
        shell(ctx, author, [("Notebook", "/"),
                            ("no such page" if code == 404 else "this page failed", None)])
        with ui.column().classes("sb-main gap-2"):
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


def _history(ctx, every, head, author: str, git_head=None) -> None:
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
            # with its target: a rename is a version that changed nothing else
            render_note(ctx, old, lambda: None, author=author, actions=False,
                        git_head=git_head, show_target=True)


def build_page(ctx, *, author: str) -> None:
    _error_pages(ctx, author)
    app.add_static_files(FONTS_URL, FONTS_DIR)

    @ui.page("/")
    def home():
        shell(ctx, author, [("Notebook", None)], here="/")
        # One HEAD for the page: each check badge read it again (2026-10-01).
        git_head = gitref.head_sha(ctx.cfg)
        heads = S.heads(S.load(ctx.store_dir))
        newest = S.newest_first(heads)
        # The boards' pool, counted once: all heads cost twice as much.
        since = gitref.commits_since(ctx.cfg, [n for n in newest if n.spec.verdict
                                               or S.read_status(n) == "open"
                                               or "priority" in n.tags])
        with ui.column().classes("sb-main gap-5"):
            ui.label(f"{summ._count(len(heads), 'note')} · {ctx.store_dir}") \
                .classes("sb-subtitle sb-mono")
            bad = S.load_malformed(ctx.store_dir)
            if bad:
                ui.label(f"⚠ {len(bad)} malformed line(s) skipped — "
                         f"first at line {bad[0][0]}").classes("text-notable")
            active = {a.id for a in S.load_arcs(ctx.store_dir) if not a.archived}
            for label, k in ctx.kinds.items():
                if k.status and not k.parked:
                    rows = summ.open_outside_arcs(newest, label, active)
                    in_arcs = len(summ.open_notes(newest, label)) - len(rows)
                    _board(ctx, f"open {label}", rows, author,
                           aside=f"· {in_arcs} in arcs" if in_arcs else "",
                           more=filters.href(kind=label, status="open"), git_head=git_head,
                           since=since)
            _board(ctx, "priority", summ.starred(newest), author, git_head=git_head,
                   since=since)
            young = datetime.now().astimezone() - timedelta(days=RECENT_DAYS)
            for label, k in ctx.kinds.items():
                if k.verdict:
                    every = [n for n in newest if n.kind == label]
                    rows = [n for n in every if _recent(ctx, n, young, git_head)][:25]
                    left = len(every) - len(rows)
                    _board(ctx, f"recent {label}", rows, author,
                           aside=f"· {left} more" if left else "",
                           more=filters.href(kind=label),
                           tip=f"every {label}: this board holds the last {RECENT_DAYS} days "
                               f"and any still current at HEAD", git_head=git_head,
                           since=since)

    @ui.page("/notes")
    def notes_page(request: Request):
        # The annotation is load-bearing: unannotated, FastAPI reads
        # `request` as a required QUERY parameter and every /notes URL
        # answers 422 instead of rendering.
        params = dict(request.query_params)
        kwargs = filters.from_params(params)
        q = (params.get("q") or "").strip()
        # What a chip or a search adds to. Not the id: one row has nothing
        # to narrow, and its kind chip opened that row again under
        # `id=… · kind=bug` (the owner, 2026-10-06).
        base = {k: v for k, v in params.items()
                if k in filters.FILTER_KEYS and k != "id" and v}
        every = S.load(ctx.store_dir)
        heads = S.heads(every)
        old = kwargs.get("id")
        # A tail, as rows and agents cite one, read "0 notes": take it as
        # `symbion show` does, and never pick one of several.
        hits = [n.id for n in every if filters.id_hit(n.id, old)] if old else []
        if len(hits) == 1:
            old = kwargs["id"] = hits[0]
        if old and not S.query(heads, id=old) and S.query(every, id=old):
            # A superseded id, as other rows cite them, read "0 notes": show
            # the row that replaced it, as `symbion show` does.
            kwargs["id"] = api._chain_tip(every, old).id
        if len(hits) > 1:
            tips = {api._chain_tip(every, h).id for h in hits}
            del kwargs["id"]
            rows = [n for n in S.query(heads, **kwargs) if n.id in tips]
        else:
            rows = S.query(heads, **kwargs)
        if q:
            # An id is not text the pattern reads: a pasted one joins the
            # hits, and a superseded one brings its current row.
            rest = S.query(heads, **{k: v for k, v in kwargs.items() if k != "grep"})
            tips = {api._chain_tip(every, n.id).id for n in every if filters.id_hit(n.id, q)}
            got = {n.id for n in rows}
            rows += [n for n in rest if n.id not in got and n.id in tips]
        rows = S.newest_first(rows)
        git_head = gitref.head_sha(ctx.cfg)
        shell(ctx, author, [("Notebook", "/"), (filters.describe(kwargs, q), None)], q=q,
              keep=base, here="" if base or old else "/notes")
        with ui.column().classes("sb-main gap-4"):
            if len(hits) > 1:
                ui.label(f"{len(hits)} rows end in -{old}; open the one you mean") \
                    .classes("text-notable").mark("ambiguous-id")
            elif kwargs.get("id", old) != old:
                ui.label(f"{old} was superseded; this is its current row") \
                    .classes("text-notable").mark("superseded-by")
            shown = rows if params.get("all") else rows[:PAGE_ROWS]
            # The cut is named at the top, not only in the link at the end.
            cut = f"; newest {len(shown)} shown" if len(shown) < len(rows) else ""
            if q:
                # Name what was read, so a 0 is not taken for "never filed".
                ui.label(f"{len(rows)} of {summ._count(len(rest), 'note')} match{cut} — "
                         f"searched body, target, checked, result, refs, tags and ids of "
                         f"current rows").classes("sb-subtitle").mark("result-count")
            else:
                ui.label(summ._count(len(rows), "note") + cut).classes("sb-subtitle") \
                    .mark("result-count")
            if kwargs:
                ui.link("clear filters", "/notes").classes("text-body")
            since = gitref.commits_since(ctx.cfg, shown)
            for n in shown:
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            show_target=True, base=base, compact="id" not in kwargs,
                            hit=q.split(), git_head=git_head, since=since[n.id])
            if len(shown) < len(rows):
                ui.link(f"+{len(rows) - len(shown)} older not shown — show all",
                        filters.href(**base, all="1")).classes("text-body").mark("show-all")
            if "id" in kwargs and len(rows) == 1:
                _history(ctx, every, rows[0], author, git_head)

    @ui.page("/targets")
    def targets_page():
        shell(ctx, author, [("Notebook", "/"), ("Targets", None)], here="/targets")
        git_head = gitref.head_sha(ctx.cfg)
        newest = S.newest_first(S.heads(S.load(ctx.store_dir)))
        # Each target with every row on or referencing it, as /object counts
        # them; first seen is newest, so the dict is in order of activity.
        on: dict[tuple, list] = {}
        for n in newest:
            for t in dict.fromkeys([(n.target.type, n.target.name),
                                    *((r.type, r.name) for r in n.refs)]):
                on.setdefault(t, []).append(n)
        # The cards are open rows on the target itself: a ref's row is
        # counted, and is a card on its own target's board.
        live = {t: [n for n in rows if (n.target.type, n.target.name) == t
                    and S.read_status(n) == "open" and not n.spec.parked]
                for t, rows in on.items()}
        # A board for each target a row is ON. One only referenced is a chip
        # on the rows that cite it: a board each was 165 bare commit shas.
        on_it = {(n.target.type, n.target.name) for n in newest}
        own = [t for t in on if t in on_it]
        subj = gitref.subjects(ctx.cfg, [name for typ, name in own if typ == "commit"])

        def title(typ, name):
            if typ == "commit" and name in subj:
                return f"commit: {name[:7]} {summ.clip(subj[name], 60)}"
            return f"{typ}: {name}" if name else typ

        since = gitref.commits_since(ctx.cfg, [n for t in own for n in live[t]])
        with ui.column().classes("sb-main gap-5"):
            ui.label(f"{summ._count(len(own), 'target')} with notes on them, open work first") \
                .classes("sb-subtitle")
            for t in sorted(own, key=lambda t: not live[t]):     # stable: newest within
                to = target_href(*t)
                _board(ctx, title(*t), live[t], author,
                       to=to, aside=f"· {summ._count(len(on[t]), 'note')}", more=to,
                       git_head=git_head, show_target=False, since=since)

    @ui.page("/tags")
    def tags_page():
        shell(ctx, author, [("Notebook", "/"), ("Tags", None)], here="/tags")
        counts = S.tag_counts(S.load(ctx.store_dir))
        with ui.column().classes("sb-main gap-2"):
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
        shell(ctx, author, [("Notebook", "/"), (f"{type}: {name}", None)],
              new=(type, name, None))
        # Open work first, then the newest; a resolved row is history and
        # reads clipped (one file's page held 50 rows in full, 2026-09-27).
        rows = S.heads_for(ctx.store_dir, type, name)
        live = lambda n: S.read_status(n) == "open" and not n.spec.parked   # noqa: E731
        rows.sort(key=lambda n: not live(n))      # stable: newest within each
        git_head = gitref.head_sha(ctx.cfg)
        with ui.column().classes("sb-main gap-4"):
            with ui.row().classes("items-center gap-3"):
                ui.label(f"{summ._count(len(rows), 'note')} on or referencing this object") \
                    .classes("sb-subtitle")
                md_button(ctx, name, "read the file")
            since = gitref.commits_since(ctx.cfg, rows)
            for n in rows:
                # The page names the object: a row only referring to it names its own.
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            compact=not live(n) and bool(n.spec.status), git_head=git_head,
                            since=since[n.id],
                            show_target=(n.target.type, n.target.name) != (type, name))

    @ui.page("/arcs")
    def arcs_page():
        shell(ctx, author, [("Notebook", "/"), ("Arcs", None)], here="/arcs")
        with ui.column().classes("sb-main gap-4"):
            arcs.index(ctx, author=author)

    @ui.page("/arc")
    def arc_page(id: str = ""):
        if not id:
            ui.navigate.to("/arcs")
            return
        act = next((a for a in S.load_arcs(ctx.store_dir) if a.id == id), None)
        shell(ctx, author, [("Notebook", "/"), ("Arcs", "/arcs"), (act.name if act else id, None)],
              here="/arcs", new=("project", None, id))
        if act is None:
            ui.label(f"no arc '{id}'").classes("sb-main text-bad")
            return

        with ui.column().classes("sb-main gap-4"):
            @ui.refreshable
            def body():
                a = next((x for x in S.load_arcs(ctx.store_dir) if x.id == id), None)
                if a is None:                       # archived away under us
                    ui.navigate.to("/arcs")
                    return
                done, total = S.arc_progress(S.load(ctx.store_dir), id)
                with ui.row().classes("items-baseline gap-3 w-full"):
                    ui.label(a.target_scope).classes("sb-chip sb-target")
                    ui.label(f"{done}/{total}").classes("sb-count")
                ui.linear_progress(value=(done / total if total else 0.0),
                                   show_value=False).props("rounded")

                with ui.row().classes("items-center gap-2 w-full sb-card"):
                    rename = ui.input("rename", value=a.name).props("dense outlined")

                    def _rename():
                        if (rename.value or "").strip():
                            api.rename_arc(ctx, id, rename.value)
                            ui.navigate.reload()        # the name is in the top bar

                    ui.button("Save", on_click=_rename).props("flat dense")
                    ui.space()

                    def _archive():
                        api.archive_arc(ctx, id)
                        ui.navigate.to("/arcs")

                    if a.archived:
                        ui.label(f"archived {S.shown(a.archived_at) if a.archived_at else ''}").classes("sb-chip")
                    else:
                        ui.button("Archive", icon="archive", on_click=_archive) \
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

                        ui.button(f"Seed all {a.target_scope}s", icon="playlist_add",
                                  on_click=_seed).props("flat dense").mark("arc-seed")

                arcs.checklist(ctx, id, body.refresh, author=author)

                # Notes ABOUT the arc (its target or a ref), not its boxes:
                # the page showed only the checklist (the gui spec).
                about = S.heads_for(ctx.store_dir, "arc", id)
                if about:
                    ui.label("notes about this arc").classes("sb-board-title")
                    git_head = gitref.head_sha(ctx.cfg)
                    since = gitref.commits_since(ctx.cfg, about)
                    for n in about:
                        render_note(ctx, n, body.refresh, author=author, git_head=git_head,
                                    since=since[n.id])

            body()
