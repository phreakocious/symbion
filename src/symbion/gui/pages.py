"""Route registration. Each route re-reads the store: the GUI holds no
snapshot, so a page is stale only in the browser and any navigation fixes it."""
from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from nicegui import Client, app, ui
from starlette.exceptions import HTTPException

from .. import api, gitref
from .. import store as S
from .. import summary as summ
from . import arcs, filters, palette, servers
from .chrome import shell, store_name
from .notes import (NoteMarkdown, ago, demo_says, file_buttons, history, link_ids,
                    read_only_refused, reading, refusal_shown, render_note, target_href,
                    view_button)
from .theme import FAVICON, FONTS_DIR, FONTS_URL, PALETTE

# /notes renders this many rows before "show all": every row is a few dozen
# elements and a check a few git calls, and all 312 of one store's rows took
# 3.7s and a 1 MB page.
PAGE_ROWS = 100
# A read-only serve's visitors share one event loop: 30 rows render /notes
# in about half the time of 100.
DEMO_ROWS = 30


def _page_rows() -> int:
    return min(PAGE_ROWS, DEMO_ROWS) if S.read_only() else PAGE_ROWS

# A "recent <kind>" board holds verdict rows this young, and older ones still
# current at HEAD. It held the 25 newest of any age: suite runs from weeks
# back, nearly all `behind`, filled it (the user, 2026-10-02).
RECENT_DAYS = 14

# A board on `/` or `/targets` shows this many of its newest rows, and every
# row due soon or past due: `/` drew every open row, a second's render on a
# busy store and as much again in the browser (the user, 2026-10-09). The
# rest are a count in the heading, linked to the view that lists them all.
BOARD_ROWS = 10


def _board(ctx, title, rows, author, *, aside: str = "", more: str = "", tip: str = "",
           git_head=None, to: str = "", show_target: bool = True, since=None,
           seen=None) -> None:
    """A heading and its cards. An empty board is its heading alone, quieter:
    a box saying "none" took a card's room to say nothing. `aside` names the
    rows of this kind the board leaves out, and `more` links the view with
    them all: beside the sidebar's count of every open row, a heading of the
    others alone read as a miscount. `to` makes the title a link. `since` is
    gitref.commits_since for a page of boards, counted once (a count per
    board took /targets from 0.37 s to 0.62 s, 2026-10-05); a card it lacks
    is counted here. Past BOARD_ROWS, only a row due soon or past due is
    drawn: a date is an ask to be reminded, and a cut must not hide it. `seen`
    is the page's notes.reading."""
    due = {n.id for n, _ in summ.due_soon(rows)}
    shown = [n for i, n in enumerate(rows) if i < BOARD_ROWS or n.id in due]
    if len(shown) < len(rows):
        aside = f"· {len(rows) - len(shown)} more {aside}".rstrip()
    with ui.element("section").classes("sb-board" + ("" if rows else " sb-board-empty")) \
            .mark("board"):
        with ui.element("div").classes("sb-board-head"):
            (ui.link(title, to) if to else ui.label(title)).classes("sb-board-title") \
                .mark("board-title")
            ui.label(str(len(shown))).classes("sb-count").mark("board-count")
            if aside:
                # A board with no page of its own: a link to "" reloaded this one.
                link = (ui.link(aside, more) if more else ui.label(aside)) \
                    .classes("sb-board-aside").mark("board-aside")
                if tip:
                    link.tooltip(tip)
        since = since or {}
        since = since | gitref.commits_since(ctx.cfg, [n for n in shown if n.id not in since],
                                             git_head)
        for n in shown:
            render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                        show_target=show_target, compact=True, git_head=git_head,
                        since=since.get(n.id), seen=seen)


def _naming(notes) -> dict[tuple, list]:
    """Each (type, name) with every row on or citing it, in the rows' order,
    as /object counts a target's rows."""
    on: dict[tuple, list] = {}
    for n in notes:
        for t in dict.fromkeys([(n.target.type, n.target.name),
                                *((r.type, r.name) for r in n.refs)]):
            on.setdefault(t, []).append(n)
    return on


def _docs_board(ctx, title: str, docs, on, mark: str, all: str, aside: str = "") -> None:
    """A board of the Docs page: a row a file, with its entries and the open
    ones among them, and a read button where the viewer can show it. Past
    BOARD_ROWS, a link shows them all."""
    docs = sorted(docs, key=lambda t: (-len(on[t]), t[1]))
    shown = docs if all else docs[:BOARD_ROWS]
    with ui.element("section").classes("sb-board" + ("" if docs else " sb-board-empty")) \
            .mark(mark):
        with ui.element("div").classes("sb-board-head"):
            ui.label(title).classes("sb-board-title")
            ui.label(str(len(docs))).classes("sb-count")
            if aside:
                ui.label(aside).classes("sb-board-aside")
        with ui.element("div").classes("sb-docs"):
            for typ, name in shown:
                rows = on[(typ, name)]
                n_open = sum(S.read_status(n) == "open" and not n.spec.parked for n in rows)
                ui.icon("article")
                ui.link(name if typ in ("file", "doc") else f"{typ}: {name}",
                        target_href(typ, name)).classes("sb-docs-name").mark("doc-link")
                with ui.element("span").classes("sb-docs-n").mark("doc-count"):
                    ui.label(f"{len(rows)} ·" if n_open else summ._count(len(rows), "entry"))
                    if n_open:
                        ui.label(f"{n_open} open").classes("text-notable")
                view_button(ctx, name, ttype=typ) or ui.element("span")
            if len(shown) < len(docs):
                ui.link(f"+{len(docs) - len(shown)} more", "/docs?all=1") \
                    .classes("sb-docs-more").mark("docs-more")


def _wrap_board(ctx, wrap: dict) -> None:
    """The last wrap's commit message, the handoff `summary` prints an agent
    before any row: what the last session did, and what it left next."""
    with ui.element("section").classes("sb-board").mark("board"):
        with ui.element("div").classes("sb-board-head"):
            ui.label("last wrap").classes("sb-board-title").mark("board-title")
            ui.label(ago(wrap["at"])).classes("sb-count").tooltip(S.shown(wrap["at"]))
            if wrap["commits_since"] is not None:
                ui.label(f"· {summ._count(wrap['commits_since'], 'commit')} since") \
                    .classes("sb-board-aside")
        with ui.element("div").classes("sb-note").mark("wrap"):
            with ui.element("div").classes("sb-note-main"):
                NoteMarkdown(link_ids("\n\n".join(filter(None, (wrap["gist"], wrap["body"]))),
                                      ctx.store_dir))
                with ui.element("div").classes("sb-note-foot"):
                    with ui.element("span").classes("sb-note-by"):
                        ui.label(wrap["author"]).classes("sb-note-meta")


def _recent(ctx, n, since, git_head) -> bool:
    """Written since `since`, or current at HEAD. Only a row stamped at HEAD
    can be current, so the rest cost no git call."""
    sha = S.stamp_sha(n.provenance)
    if S.written_at(n) >= since:
        return True
    if not sha:
        return False
    if ctx.cfg.repos and (n.provenance or {}).get("repo") != ctx.cfg.here:
        # `git_head` is the serve's repo's, or "" when it stands in none: a
        # stamp of another repo is read against its own HEAD.
        # ponytail: one HEAD read per such row; pass a head per repo if
        # the page's time shows it.
        return api.verdict_state(ctx.cfg, n, git_head or None)[0] == "current"
    return bool(git_head and git_head.startswith(sha)
                and api.verdict_state(ctx.cfg, n, git_head)[0] == "current")


def _error_pages(ctx, author: str) -> None:
    """NiceGUI's own error pages: its sad-face art, and no header to get
    back from. These keep symbion's chrome and say what went wrong."""
    def body(code: int, what: str) -> None:
        shell(ctx, author, [("No such page" if code == 404 else "This page failed", None)])
        with ui.column().classes("sb-main gap-2"):
            ui.label(what).classes("sb-subtitle text-bad").mark("error-detail")
            ui.link("back to the notebook", "/").classes("text-body")

    # A read-only serve is a public demo: the text can hold a path on its
    # machine, and its visitors have no use for it. The log keeps it.
    app.on_page_exception(lambda e: body(500, type(e).__name__ if S.read_only()
                                         else f"{type(e).__name__}: {e}"))

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


def build_page(ctx, *, author: str) -> None:
    _error_pages(ctx, author)
    app.on_exception(read_only_refused)
    app.add_static_files(FONTS_URL, FONTS_DIR)

    # Makes the GUI an installable app. "tabbed" opens its pages as tabs in
    # one app window; a Chrome without tabbed mode (on desktop, behind
    # chrome://flags/#enable-desktop-pwas-tab-strip) falls back to standalone.
    # PNG icons, rendered from favicon.svg: Chrome on macOS installed the app
    # with a blank icon from the SVG alone (the user, 2026-10-15). They are
    # the page background, opaque (the user, same day), with the rings inside
    # the 40% circle a maskable icon keeps. To redraw: favicon.svg with a
    # viewBox of -4 -4 40 40 over a page-colour rect, screenshot by Chrome,
    # the SVG at the icon's pixel size: under Chrome's minimum window width,
    # one that fills the viewport renders larger and is cropped.
    icons = [{"src": app.add_static_file(local_file=FAVICON.parent / f"icon-{s}.png",
                                         url_path=f"/icon-{s}.png"),
              "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any maskable"}
             for s in (192, 512)]

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({
            "name": f"{store_name(ctx)} · symbion", "short_name": store_name(ctx),
            "id": "/", "start_url": "/", "scope": "/",
            "display": "standalone", "display_override": ["tabbed"],
            # Chrome paints a tabbed app's frame and inactive tabs in
            # theme_color. Its active tab was bg_page with background_color
            # bg_page and with bg_panel, the app relaunched (2026-10-16): it
            # follows the page's background, or a value stored at install. So the strip is the sidebar's colour, the
            # one that differs from the tab, or no tab reads as current.
            "theme_color": PALETTE.bg_panel, "background_color": PALETTE.bg_page,
            # A link Chrome sends to the app opens in a new tab of its window:
            # one reused would leave its page, a draft and all. A link another
            # program opens (a terminal's) stays in the browser; one option its
            # team weighed, 2026-09-08, captures those for a site that names a
            # client_mode (Chromium issue 330282442).
            "launch_handler": {"client_mode": "navigate-new"},
            # A link from another program reaches the app through its shim
            # only as a scheme registered here: the shim drops an http link
            # and launches the app on its start page (2026-10-17). A router
            # rewrites the store's link to web+symbion:, and /go sends it on.
            "protocol_handlers": [{"protocol": "web+symbion", "url": "/go?to=%s"}],
            "icons": icons,
        }, media_type="application/manifest+json")

    @app.get("/go")
    def go(request: Request, to: str = ""):
        """A web+symbion: link, as the manifest's protocol handler hands it
        here, sent on to its page. Only this serve's address and the other
        running serves' are followed: any page can make the app open a
        web+symbion: link, and this must not send it anywhere."""
        url = urlsplit(to)
        own = {request.url.netloc, *(urlsplit(r["url"]).netloc for r in servers.running())}
        if url.scheme != "web+symbion" or url.netloc not in own:
            raise HTTPException(404, f"not a web+symbion: link to a running serve: {to}")
        return RedirectResponse(urlunsplit(("http", url.netloc, url.path, url.query, url.fragment)),
                                status_code=302)

    # The ⌘K palette's rows for what is typed (palette.py). A plain def:
    # FastAPI runs it on a thread, off the loop every page's socket shares.
    @app.get("/palette")
    def palette_rows(q: str = ""):
        return JSONResponse(palette.rows(ctx, author, q))

    @ui.page("/")
    @gitref.heads_once()           # each repo's HEAD once a page
    def home():
        shell(ctx, author, [("Summary", None)], here="/")
        # One HEAD for the page: each check badge read it again (2026-10-01).
        git_head = gitref.head_sha(ctx.cfg)
        rd = reading(ctx)
        side, loaded, by_id = rd
        heads = S.heads(loaded)
        newest = S.newest_first(heads)
        # What `summary` puts first for an agent, for the person: what waits
        # on them, the last session's handoff, what is due. Each row shows
        # once, as there: the boards below leave these out and count them.
        # In a shared store those three are the person's side's; the boards
        # below are the store, both sides.
        mine = [n for n in newest if summ.on_side(by_id, n, side)]
        waiting = [n for n in mine if summ.asks(author, by_id, n)]
        due = [n for n, _ in summ.due_soon(mine) if n.id not in {w.id for w in waiting}]
        above = {n.id for n in waiting + due}

        def below(rows, aside=""):
            kept = [n for n in rows if n.id not in above]
            up = len(rows) - len(kept)
            return kept, " ".join(filter(None, (f"· {up} above" if up else "", aside)))
        # The boards' pool, counted once: all heads cost twice as much.
        since = gitref.commits_since(ctx.cfg, [n for n in newest if n.spec.verdict
                                               or S.read_status(n) == "open"
                                               or "priority" in n.tags], git_head)
        with ui.column().classes("sb-main gap-5"):
            # Read-only, the page is public: the store's name, not its path there.
            where = store_name(ctx) if S.read_only() else ctx.store_dir
            ui.label(f"{summ._count(len(heads), 'entry')} · {where}") \
                .classes("sb-subtitle sb-mono")
            bad = S.load_malformed(ctx.store_dir)
            if bad:
                ui.label(f"⚠ {len(bad)} malformed line(s) skipped — "
                         f"first at line {bad[0][0]}").classes("text-notable")
            if waiting:
                _board(ctx, "waiting on you", waiting, author, git_head=git_head, since=since,
                       seen=rd)
            if wrap := gitref.last_wrap(ctx.store_dir, ctx.cfg, side):
                _wrap_board(ctx, wrap)
            if due:
                _board(ctx, "due soon", due, author, git_head=git_head, since=since, seen=rd)
            active = {a.id for a in S.load_arcs(ctx.store_dir) if not a.archived}
            for label, k in ctx.kinds.items():
                if k.status and not k.parked:
                    every = summ.open_outside_arcs(newest, label, active)
                    in_arcs = len(summ.open_notes(newest, label)) - len(every)
                    rows, aside = below(every, f"· {in_arcs} in arcs" if in_arcs else "")
                    _board(ctx, f"open {label}", rows, author, aside=aside,
                           more=filters.href(kind=label, status="open"), git_head=git_head,
                           since=since, seen=rd)
            rows, aside = below(summ.starred(newest))
            _board(ctx, "priority", rows, author, aside=aside,
                   more=filters.href(tag="priority"), git_head=git_head, since=since, seen=rd)
            young = S.now_shown() - timedelta(days=RECENT_DAYS)
            for label, k in ctx.kinds.items():
                if k.verdict:
                    every = [n for n in newest if n.kind == label]
                    rows = [n for n in every if _recent(ctx, n, young, git_head)][:BOARD_ROWS]
                    left = len(every) - len(rows)
                    _board(ctx, f"recent {label}", rows, author,
                           aside=f"· {left} more" if left else "",
                           more=filters.href(kind=label),
                           tip=f"every {label}: this board holds the last {RECENT_DAYS} days "
                               f"and any still current at HEAD", git_head=git_head,
                           since=since, seen=rd)

    @ui.page("/notes")
    @gitref.heads_once()           # each repo's HEAD once a page
    def notes_page(request: Request):
        # The annotation is load-bearing: unannotated, FastAPI reads
        # `request` as a required QUERY parameter and every /notes URL
        # answers 422 instead of rendering.
        params = dict(request.query_params)
        kwargs = filters.from_params(params)
        q = (params.get("q") or "").strip()
        # What a chip or a search adds to. Not the id: one row has nothing
        # to narrow, and its kind chip opened that row again under
        # `id=… · kind=bug` (the user, 2026-10-06).
        base = {k: v for k, v in params.items()
                if k in filters.FILTER_KEYS and k != "id" and v}
        rd = reading(ctx)
        every = rd.loaded
        heads = S.heads(every)
        # The superseded ids that brought each current row: its card says
        # so, as a search for one showed the row and nothing of why (the
        # user, 2026-10-09).
        via: dict[str, list[str]] = {}

        def tip(i: str) -> str:
            t = S.chain_tip(every, i).id
            if t != i:
                via.setdefault(t, []).append(i)
            return t

        old = kwargs.get("id")
        # A tail, as rows and agents cite one, read "0 notes": take it as
        # `symbion show` does, and never pick one of several.
        hits = [n.id for n in every if filters.id_hit(n.id, old)] if old else []
        if len(hits) == 1:
            old = kwargs["id"] = hits[0]
        if old and not S.query(heads, id=old) and S.query(every, id=old):
            # A superseded id, as other rows cite them, read "0 notes": show
            # the row that replaced it, as `symbion show` does.
            kwargs["id"] = tip(old)
        if len(hits) > 1:
            tips = {tip(h) for h in hits}
            del kwargs["id"]
            rows = [n for n in S.query(heads, **kwargs) if n.id in tips]
        else:
            rows = S.query(heads, **kwargs)
        if q:
            # An id is not text the pattern reads: a pasted one joins the
            # hits, and a superseded one brings its current row.
            rest = S.query(heads, **{k: v for k, v in kwargs.items() if k != "grep"})
            tips = {tip(n.id) for n in every if filters.id_hit(n.id, q)}
            got = {n.id for n in rows}
            rows += [n for n in rest if n.id not in got and n.id in tips]
        rows = S.newest_first(rows)
        git_head = gitref.head_sha(ctx.cfg)
        shell(ctx, author, [(filters.describe(kwargs, q), None)], q=q,
              keep=base, here="" if base or old else "/notes", kind=kwargs.get("kind"))
        with ui.column().classes("sb-main gap-4"):
            if len(hits) > 1:
                ui.label(f"{len(hits)} entries end in -{old}; open the one you mean") \
                    .classes("text-notable").mark("ambiguous-id")
            # The demo is public: a page of every row held the loop for all.
            shown = rows if params.get("all") and not S.read_only() else rows[:_page_rows()]
            # The cut is named at the top, not only in the link at the end.
            cut = f"; newest {len(shown)} shown" if len(shown) < len(rows) else ""
            if q:
                # Name what was read, so a 0 is not taken for "never filed".
                ui.label(f"{len(rows)} of {summ._count(len(rest), 'entry')} match{cut} — "
                         f"searched body, target, checked, result, refs and tags of current "
                         f"entries, and every version's id").classes("sb-subtitle") \
                    .mark("result-count")
            else:
                ui.label(summ._count(len(rows), "entry") + cut).classes("sb-subtitle") \
                    .mark("result-count")
            if kwargs:
                ui.link("clear filters", "/notes").classes("text-body")
            since = gitref.commits_since(ctx.cfg, shown, git_head)
            by_id = rd.by_id
            for n in shown:
                # Held to its card: in the page's gap it read as the last one's.
                with ui.column().classes("w-full gap-1") if n.id in via else nullcontext():
                    for was in via.get(n.id, ()):
                        ui.label(f"{was} was superseded; this is its current entry") \
                            .classes("text-notable").mark("superseded-by")
                    # A row's own page marks where each amendment joined its body.
                    render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                                show_target=True, base=base, compact="id" not in kwargs,
                                hit=q.split(), git_head=git_head, since=since[n.id],
                                seams=summ.seams(by_id, n) if "id" in kwargs else (),
                                seen=rd)
            if len(shown) < len(rows) and not S.read_only():
                ui.link(f"+{len(rows) - len(shown)} older not shown — show all",
                        filters.href(**base, all="1")).classes("text-body").mark("show-all")
            if "id" in kwargs and len(rows) == 1:
                history(ctx, every, rows[0], author, git_head)

    @ui.page("/targets")
    @gitref.heads_once()           # each repo's HEAD once a page
    def targets_page(all: str = ""):
        shell(ctx, author, [("Targets", None)], here="/targets")
        git_head = gitref.head_sha(ctx.cfg)
        rd = reading(ctx)
        newest = S.newest_first(S.heads(rd.loaded))
        # first seen is newest, so the dict is in order of activity
        on = _naming(newest)
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

        since = gitref.commits_since(ctx.cfg, [n for t in own for n in live[t]], git_head)
        with ui.column().classes("sb-main gap-5"):
            ui.label(f"{summ._count(len(own), 'target')} with entries on them, open work first") \
                .classes("sb-subtitle")
            # A page of cards, as /notes draws: a board per file of a seeded
            # arc drew 1,500, on the loop every visitor shares.
            budget = None if all and not S.read_only() else _page_rows()
            order = sorted(own, key=lambda t: not live[t])     # stable: newest within
            drawn = 0
            for i, t in enumerate(order):
                if budget is not None and drawn >= budget:
                    if not S.read_only():
                        ui.link(f"+{len(order) - i} more targets not shown — show all",
                                "/targets?all=1").classes("text-body").mark("show-all")
                    break
                drawn += max(1, min(len(live[t]), BOARD_ROWS))
                to = target_href(*t)
                _board(ctx, title(*t), live[t], author,
                       to=to, aside=f"· {summ._count(len(on[t]), 'entry')}", more=to,
                       git_head=git_head, show_target=False, since=since, seen=rd)

    @ui.page("/tags")
    def tags_page():
        shell(ctx, author, [("Tags", None)], here="/tags")
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

    @ui.page("/docs")
    def docs_page(all: str = ""):
        """The user, 2026-10-16: "add Docs below Arcs in the sidebar and show
        all the documents we know about". Every markdown file a row is on or
        cites, the repo's, then the store's docs/, the most named first."""
        shell(ctx, author, [("Docs", None)], here="/docs")
        on = {t: rows for t, rows in _naming(S.heads(S.load(ctx.store_dir))).items()
              if t[1] and t[1].lower().endswith(".md")}
        named = len(on)
        # Then those no entry names (the user, 2026-10-27: "README.md in
        # particular"). An item named as a path opens the same file.
        for typ, cfg in [(t, ctx.cfg.for_type(t)) for t in ctx.cfg.repos] or [("file", ctx.cfg)]:
            for name in gitref.markdown_files(cfg):
                if (typ, name) not in on and ("item", name) not in on:
                    on[(typ, name)] = []
        for name in gitref.markdown_files(ctx.store_dir / "docs"):
            on.setdefault(("doc", name), [])
        docs, root = ctx.store_dir / "docs", ctx.cfg.project_root
        try:
            where = os.path.relpath(docs, root) if root else str(docs)
        except ValueError:           # Windows: no relative path to another drive
            where = str(docs)
        with ui.column().classes("sb-main gap-5"):
            ui.label(f"{summ._count(named, 'markdown file')} that entries name, the most "
                     "named first" + (f"; then {len(on) - named} that none do"
                                      if len(on) > named else "")).classes("sb-subtitle")
            _docs_board(ctx, "in the repo", [t for t in on if t[0] != "doc"], on, "docs-repo", all)
            _docs_board(ctx, "in the store", [t for t in on if t[0] == "doc"], on, "docs-store",
                        all, aside=f"· {where}")

    @ui.page("/object")
    @gitref.heads_once()           # each repo's HEAD once a page
    def object_page(type: str = "", name: str = ""):
        # Guard BEFORE the shell: rendering a header for ": " and then
        # navigating away paints a frame of a page that does not exist.
        if not type or not name:
            ui.navigate.to("/notes")
            return
        shell(ctx, author, [(f"{type}: {name}", None)],
              new=(type, name, None))
        # Open work first, then the newest; a resolved row is history and
        # reads clipped (one file's page held 50 rows in full, 2026-09-27).
        rd = reading(ctx)
        rows = S.heads_for(ctx.store_dir, type, name, loaded=rd.loaded)
        live = lambda n: S.read_status(n) == "open" and not n.spec.parked   # noqa: E731
        rows.sort(key=lambda n: not live(n))      # stable: newest within each
        git_head = gitref.head_sha(ctx.cfg)
        with ui.column().classes("sb-main gap-4"):
            with ui.row().classes("items-center gap-3"):
                ui.label(f"{summ._count(len(rows), 'entry')} on or referencing this object") \
                    .classes("sb-subtitle")
                view_button(ctx, name, "read the file", ttype=type)
                if (path := api.object_path(ctx, type, name)) and path.is_file():
                    file_buttons(path)
            since = gitref.commits_since(ctx.cfg, rows, git_head)
            for n in rows:
                # The page names the object: a row only referring to it names its own.
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            compact=not live(n) and bool(n.spec.status), git_head=git_head,
                            since=since[n.id], seen=rd,
                            show_target=(n.target.type, n.target.name) != (type, name))

    @ui.page("/arcs")
    def arcs_page():
        shell(ctx, author, [("Arcs", None)], here="/arcs")
        with ui.column().classes("sb-main gap-4"):
            arcs.index(ctx, author=author)

    @ui.page("/arc")
    @gitref.heads_once()           # each repo's HEAD once a page
    def arc_page(id: str = ""):
        if not id:
            ui.navigate.to("/arcs")
            return
        act = next((a for a in S.load_arcs(ctx.store_dir) if a.id == id), None)
        shell(ctx, author, [("Arcs", "/arcs"), (act.name if act else id, None)],
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
                rd = reading(ctx)
                done, total = S.arc_progress(rd.loaded, id)
                with ui.row().classes("items-baseline gap-3 w-full"):
                    ui.label(a.target_scope).classes("sb-chip sb-target")
                    ui.label(f"{done}/{total}").classes("sb-count")
                ui.linear_progress(value=(done / total if total else 0.0),
                                   show_value=False).props("rounded")

                with ui.row().classes("items-center gap-2 w-full sb-card"):
                    rename = ui.input("rename", value=a.name).props("dense outlined")

                    def _rename():
                        if demo_says("renames the arc"):
                            return
                        if (rename.value or "").strip():
                            with refusal_shown():
                                api.rename_arc(ctx, id, rename.value)
                                ui.navigate.reload()        # the name is in the top bar

                    ui.button("Save", on_click=_rename).props("flat dense").mark("arc-rename")
                    ui.space()

                    def _archive():
                        if demo_says("archives the arc"):
                            return
                        with refusal_shown():
                            api.archive_arc(ctx, id)
                            ui.navigate.to("/arcs")

                    if a.archived:
                        ui.label(f"archived {S.shown(a.archived_at) if a.archived_at else ''}").classes("sb-chip")
                    else:
                        ui.button("Archive", icon="archive", on_click=_archive) \
                            .props("flat dense color=warning").mark("arc-archive")

                    if a.target_scope in arcs.seed_all_scopes(ctx) and arcs.seedable(ctx):
                        def _seed():
                            if demo_says("adds a box per name"):
                                return
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

                arcs.checklist(ctx, id, body.refresh, author=author, seen=rd)

                # Notes ABOUT the arc (its target or a ref), not its boxes:
                # the page showed only the checklist (the gui spec).
                about = S.heads_for(ctx.store_dir, "arc", id, loaded=rd.loaded)
                if about:
                    ui.label("entries about this arc").classes("sb-board-title")
                    git_head = gitref.head_sha(ctx.cfg)
                    since = gitref.commits_since(ctx.cfg, about, git_head)
                    for n in about:
                        render_note(ctx, n, body.refresh, author=author, git_head=git_head,
                                    since=since[n.id], seen=rd)

            body()
