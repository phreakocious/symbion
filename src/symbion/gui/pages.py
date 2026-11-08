"""Route registration. Each route re-reads the store: the GUI holds no
snapshot, so a page is stale only in the browser and any navigation fixes it."""
from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from nicegui import Client, app, ui
from starlette.exceptions import HTTPException

from .. import api, gitref
from .. import term as T
from .. import store as S
from .. import summary as summ
from . import arcs, diffview, filters, palette, servers, treemap
from .chrome import ABOUT, SOURCE, own_write, shell, source_href, store_name
from .notes import (NoteMarkdown, ago, commit_meta, demo_says, edit_dialog, file_buttons,
                    forge_buttons, heading, history, link_ids,
                    kind_class, read_only_refused, reading, refusal_shown, render_note,
                    short_id, target_href, view_button)
from .theme import (FAVICON, FONTS_DIR, FONTS_URL, MAP_EMPTY, MAP_NEW, MAP_OPEN, MAP_TEXT,
                    PALETTE)

# /notes renders this many rows before "show all": every row is a few dozen
# elements and a check a few git calls, and all 312 of one store's rows took
# 3.7s and a 1 MB page.
PAGE_ROWS = 100
# A read-only serve's visitors share one event loop: 30 rows render /notes
# in about half the time of 100.
DEMO_ROWS = 30


def _page_rows() -> int:
    return min(PAGE_ROWS, DEMO_ROWS) if S.read_only() else PAGE_ROWS


def _not_shown(n: int, noun: str, page: str) -> None:
    """After a capped list: the `n` it left out, and a link to the whole
    list on `page`, which a read-only serve never draws."""
    if n <= 0:
        return
    what = f"+{summ._count(n, 'more ' + noun)} not shown"
    if S.read_only():
        ui.label(what).classes("sb-subtitle").mark("not-shown")
    else:
        ui.link(f"{what} — show all", f"{page}&all=1").classes("text-body").mark("show-all")

# A "recent <kind>" board holds verdict rows this young, and older ones still
# current at HEAD. It held the 25 newest of any age: suite runs from weeks
# back, nearly all `behind`, filled it (the user, 2026-10-02).
RECENT_DAYS = 14

# A board on `/` or `/targets` shows this many of its newest rows, and every
# row due soon or past due: `/` drew every open row, a second's render on a
# busy store and as much again in the browser (the user, 2026-10-09). The
# rest are a count in the heading, linked to the view that lists them all.
BOARD_ROWS = 10
# A "recent <kind>" board on `/` shows this many of its runs, the rest a count
# linked to the kind's list: at BOARD_ROWS, two such boards took more than half
# the page (the design critique, 2026-11-07).
RECORD_ROWS = 3

# The map's panel lists this many of a tile's entries, then links to a page
# that lists them all, where one does.
PANEL_ROWS = 60

# Each focus of the map is an entry in the tab's history, so back undoes it
# (the user, 2026-10-30); the URL stays the view's, as the map spec keeps the
# zoom out of it. An entry holds the focus as its labels from the top, not a
# tile id: back to the map from an entry it listed draws the page anew, and
# finds the group by them if it is still there. `sbMapChart` is set beside it.
_MAP_JS = """<script>
function sbMapTree(c) { return c.getModel().getSeriesByIndex(0).getData().tree; }
function sbMapPush(node) {
  const to = [];
  for (; node.parentNode; node = node.parentNode) to.unshift(node.name);
  const at = (history.state && history.state.sbMap) || [];
  if (JSON.stringify(to) !== JSON.stringify(at)) history.pushState({sbMap: to}, '');
}
function sbMapGo(node) {
  getElement(sbMapChart).chart.dispatchAction({type: 'treemapRootToNode', targetNode: node});
  sbMapPush(node);
}
function sbMapClick(e) {
  const tree = sbMapTree(getElement(sbMapChart).chart);
  if (e.data && e.data.children) sbMapGo(tree.getNodeById(String(e.data.id)));
  else if (e.selfType === 'breadcrumb') sbMapPush(tree.getNodeByDataIndex(e.nodeData.dataIndex));
}
// The panel lists a tile's entries, or says what a click does for the whole map.
function sbMapTile(node) { emitEvent('sbMapTile', node.parentNode ? Number(node.getId()) : null); }
function sbMapShow() {
  const c = getElement(sbMapChart).chart, tree = sbMapTree(c);
  let node = tree.root;
  for (const name of (history.state && history.state.sbMap) || [])
    node = (node && node.children.find(n => n.name === name)) || null;
  node = node || tree.root;
  c.dispatchAction({type: 'treemapRootToNode', targetNode: node});
  sbMapTile(node);
}
// The canvas takes no keyboard and says nothing to a screen reader (the design
// critique, 2026-11-07), so the tiles in view are buttons too, redrawn on every
// change of focus: a press acts as a click on the tile, and "up" as the breadcrumb.
// One tab stop, arrows within, as a toolbar: a group can hold hundreds of tiles.
function sbMapList() {
  const box = document.querySelector('.sb-map-tiles');
  const root = getElement(sbMapChart).chart.getModel().getSeriesByIndex(0).getViewRoot();
  const item = (text, go) => {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = text; b.onclick = go; b.tabIndex = -1; return b;
  };
  const items = root.children.map(n => item(`${n.name}: ${n.getModel().get('tip')}`, () => {
    if (n.children.length) sbMapGo(n);
    sbMapTile(n);
  }));
  const up = root.parentNode;
  if (up) items.unshift(item(`up to ${up.parentNode ? up.name : 'the whole map'}`,
                             () => { sbMapGo(up); sbMapTile(up); }));
  if (items.length) items[0].tabIndex = 0;
  const had = box.contains(document.activeElement);
  box.replaceChildren(...items);
  if (had && items.length) items[0].focus();
}
function sbMapKey(e) {
  const bs = [...e.currentTarget.children], i = bs.indexOf(document.activeElement);
  const j = {ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1,
             Home: 0, End: bs.length - 1}[e.key];
  if (j === undefined || i < 0) return;
  e.preventDefault();
  const b = bs[Math.max(0, Math.min(bs.length - 1, j))];
  bs[i].tabIndex = -1; b.tabIndex = 0; b.focus();
}
let sbMapDrawn = false;
function sbMapLoad() {
  if (sbMapDrawn) return;
  sbMapDrawn = true;
  const c = getElement(sbMapChart).chart;
  c.on('treemaproottonode', sbMapList);
  document.querySelector('.sb-map-tiles').addEventListener('keydown', sbMapKey);
  sbMapList();
  if (history.state && history.state.sbMap && history.state.sbMap.length) sbMapShow();
}
addEventListener('popstate', sbMapShow);
</script>"""


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
            with heading(2, "" if to else title) as h:
                (ui.link(title, to) if to else h).classes("sb-board-title").mark("board-title")
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
        for t in treemap.objects(n):
            on.setdefault(t, []).append(n)
    return on


def _commit_card(ctx, sha: str, loaded, every: bool = False) -> None:
    """A commit's page leads with the commit: its subject, who and when, its
    message, its buttons, and each file it changed with its counts and the
    entries on it, as the Docs page lists a file's (the diff viewer design,
    2026-10-30). A page of files unless `every`: a 2,000-file commit drew
    14,161 elements (audit 2026-11-02)."""
    cfg = gitref.commit_cfg(ctx.cfg, sha)
    got = gitref.commit_show(cfg, sha, patch=False)
    if got is None:
        ui.label("not a commit in this checkout").classes("sb-subtitle").mark("commit-none")
        return
    author, date, parents, message, _ = got
    subject, _, body = message.partition("\n")
    files = gitref.commit_files(cfg, sha)
    ftype = gitref.holder(ctx.cfg, sha) or "file"     # over several repos, the repo's type
    on = _naming(S.heads(loaded))
    with ui.card().classes("bg-panel w-full sb-commit-card").mark("commit-card"):
        ui.label(subject).classes("sb-commit-subject")
        commit_meta(sha, author, date, parents, body)
        with ui.row().classes("items-center gap-1"):
            view_button(ctx, sha, "read the diff", ttype="commit")
            forge_buttons(cfg, sha)
            ui.button(icon="content_copy",
                      on_click=lambda: (ui.clipboard.write(sha), ui.notify(f"copied {sha}"))) \
                .props('flat dense round size=sm color=muted aria-label="copy the full sha"') \
                .tooltip(f"copy {sha}")
        plus, minus = (sum(f[i] or 0 for f in files) for i in (1, 2))
        shown = files if every else files[:_page_rows()]
        cut = f"; the first {len(shown)} shown" if len(shown) < len(files) else ""
        ui.label(f"{summ._count(len(files), 'file')} · +{plus} −{minus} · the entries on "
                 f"each{cut}").classes("sb-subtitle").mark("commit-stat")
        with ui.element("div").classes("sb-docs sb-commit-files"):
            for path, added, deleted in shown:
                rows = on.get((ftype, path), [])
                n_open = sum(map(treemap.live, rows))
                with ui.element("div").classes("sb-docs-row").mark("commit-file"):
                    ui.icon("description")
                    # the whole path, as a tip, where an ellipsis cuts it (375 px)
                    ui.link(path, target_href(ftype, path)).classes("sb-docs-name") \
                        ._props["data-tip-cut"] = path
                    ui.html(diffview.counts(added, deleted), sanitize=False)
                    with ui.element("span").classes("sb-docs-n").mark("commit-file-entries"):
                        if rows:
                            ui.label(f"{len(rows)} ·" if n_open else summ._count(len(rows), "entry"))
                        if n_open:
                            ui.label(f"{n_open} open").classes("text-notable")
                    view_button(ctx, path, ttype=ftype) or ui.element("span")
        _not_shown(len(files) - len(shown), "file", target_href("commit", sha))


def _docs_board(ctx, title: str, docs, on, mark: str, all: str, aside: str = "") -> None:
    """A board of the Docs page: a row a file, with its entries and the open
    ones among them, and a read button where the viewer can show it. Past
    BOARD_ROWS, a link shows them all. Those rows are drawn hidden, for the
    search box's filter to find (chrome.py, `sbFilter`)."""
    docs = sorted(docs, key=lambda t: (-len(on[t]), t[1]))
    with ui.element("section").classes("sb-board" + ("" if docs else " sb-board-empty")) \
            .mark(mark):
        with ui.element("div").classes("sb-board-head"):
            heading(2, title).classes("sb-board-title")
            ui.label(str(len(docs))).classes("sb-count")
            if aside:
                ui.label(aside).classes("sb-board-aside")
        with ui.element("div").classes("sb-docs"):
            # shortcut: draws every row, about six elements each; a repo of
            # thousands of markdown files wants the filter on the server.
            for i, (typ, name) in enumerate(docs):
                rows = on[(typ, name)]
                n_open = sum(S.read_status(n) == "open" and not n.spec.parked for n in rows)
                past = not all and i >= BOARD_ROWS
                with ui.element("div").classes("sb-docs-row sb-filterable" +
                                               (" sb-past" if past else "")).mark("doc-row"):
                    ui.icon("article")
                    shown = name if typ in ("file", "doc") else f"{typ}: {name}"
                    # the whole name, as a tip, where an ellipsis cuts it (the
                    # design critique, 2026-11-07: long names were cut at 375 px)
                    ui.link(shown, target_href(typ, name)).classes("sb-docs-name") \
                        .mark("doc-link")._props["data-tip-cut"] = shown
                    with ui.element("span").classes("sb-docs-n").mark("doc-count"):
                        ui.label(f"{len(rows)} ·" if n_open else summ._count(len(rows), "entry"))
                        if n_open:
                            ui.label(f"{n_open} open").classes("text-notable")
                    view_button(ctx, name, ttype=typ) or ui.element("span")
            if not all and len(docs) > BOARD_ROWS:
                ui.link(f"+{len(docs) - BOARD_ROWS} more", "/docs?all=1") \
                    .classes("sb-docs-more").mark("docs-more")


def _wrap_board(ctx, wrap: dict) -> None:
    """The last wrap's commit message, the handoff `summary` prints an agent
    before any row: what the last session did, and what it left next."""
    with ui.element("section").classes("sb-board").mark("board"):
        with ui.element("div").classes("sb-board-head"):
            heading(2, "last wrap").classes("sb-board-title").mark("board-title")
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
        shell(ctx, author, [("No such page" if code == 404 else "This page failed", None)],
              cards=False)
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

    @app.get("/go/github")
    def go_github():
        """The source, by way of this serve (`chrome.source_href`). A 302 is
        not cached, so each click reaches the access log."""
        return RedirectResponse(SOURCE + "#readme", status_code=302)

    # The ⌘K palette's rows for what is typed (palette.py). A plain def:
    # FastAPI runs it on a thread, off the loop every page's socket shares.
    @app.get("/palette")
    def palette_rows(q: str = ""):
        return JSONResponse(palette.rows(ctx, author, q))

    # What another serve's palette asks of this store: each serve reads its own.
    @app.get("/palette/here")
    def palette_here(q: str = ""):
        return JSONResponse(palette.answer(ctx, q))

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
            if S.read_only():
                # A visitor from a social post lands here before the README, and
                # most left from this page alone: it says what symbion is and what
                # this store is, with no click (the user, 2026-11-05). Its link is
                # in prose: a filled button beside New entry made two (the user,
                # 2026-11-07).
                with ui.card().classes("bg-panel w-full gap-3 sb-demo-intro").mark("demo-intro"):
                    ui.label(f"{ABOUT} Each new agent session starts with what is open, "
                             f"from plain JSONL in a git repo beside yours.") \
                        .classes("text-emph")
                    with ui.element("div").classes("text-body"):
                        ui.label(f"This is {store_name(ctx)}'s store, as its agents and owner "
                                 f"keep it. You see what its owner sees; nothing you click "
                                 f"here writes. Source, install and docs: ").classes("inline")
                        ui.link("symbion on GitHub", source_href("intro"), new_tab=True) \
                            .mark("demo-intro-link")
                        ui.label(".").classes("inline")
            # Read-only, the page is public: the store's name, not its path there.
            where = store_name(ctx) if S.read_only() else ctx.store_dir
            ui.label(f"{summ._count(len(heads), 'entry')} · {where}") \
                .classes("sb-subtitle sb-mono")
            bad = S.load_malformed(ctx.store_dir)
            if bad:
                ui.label(f"⚠ {len(bad)} malformed line(s) skipped — "
                         f"first at line {bad[0][0]}").classes("text-notable")
            if waiting:
                # Read-only, the reader is a visitor, not the owner it waits on
                _board(ctx, "waiting on the owner" if S.read_only() else "waiting on you",
                       waiting, author, git_head=git_head, since=since, seen=rd)
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
            runs = [label for label, k in ctx.kinds.items() if k.verdict]
            if runs:
                # A rule between the boards to act on and the records of runs:
                # one weight for both left the work's end unmarked (the design
                # critique, 2026-11-07). A rule, not a "records" head: the
                # sidebar's records are the kinds with no status.
                ui.element("hr").classes("sb-boards-rule").mark("runs-rule")
            for label in runs:
                every = [n for n in newest if n.kind == label]
                rows = [n for n in every if _recent(ctx, n, young, git_head)][:RECORD_ROWS]
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
        # One entry's page names the entry: its title and tab read `id=…`,
        # alike for every entry, over "1 entry" (the GUI review, 2026-11-07).
        one = rows[0] if "id" in kwargs and len(rows) == 1 else None
        # An id no version has, whole or as a tail, read "id=nope · 0 entries"
        # (the design critique, 2026-11-07)
        unknown = bool(old) and not hits and not S.query(every, id=old)
        crumb = f"{one.kind} {short_id(one.id)}" if one else f"no entry {old}" if unknown else None
        shell(ctx, author, [("All entries", "/notes"), (crumb, None)]
              if crumb else [(filters.title(kwargs, q), None)],
              tab=f"{one.kind} · {summ.clip(one.body or one.checked or one.result or '', 60)}"
              if one else None, q=q,
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
            elif unknown:
                ui.label(f"No entry has an id that is or ends in “{old}”.") \
                    .classes("sb-subtitle").mark("no-such-id")
            elif not one:
                ui.label(summ._count(len(rows), "entry") + cut).classes("sb-subtitle") \
                    .mark("result-count")
            if base:
                # Each filter a chip whose × takes off that one: "clear filters"
                # took all of them at once (the GUI review, 2026-11-07).
                with ui.row().classes("items-center gap-2").mark("filter-chips"):
                    for k, v in base.items():
                        with ui.link(target=filters.href(**{**base, k: None})) \
                                .classes("sb-chip sb-filter-chip").mark("filter-chip") as chip:
                            ui.label(f"{'search' if k == 'q' else k} {v}")
                            ui.icon("close").classes("sb-filter-x")
                        # set, not parsed: the value comes from the URL
                        chip._props["aria-label"] = f"take off the filter {k} {v}"
                    ui.link("clear all", "/notes").classes("sb-board-aside").mark("clear-filters")
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
        shell(ctx, author, [("Tags", None)], here="/tags", cards=False)
        counts = S.tag_counts(S.load(ctx.store_dir))
        with ui.column().classes("sb-main gap-2"):
            ui.label(f"{len(counts)} in use — near-synonyms are drift, not vocabulary") \
                .classes("sb-subtitle")
            # Name the drift the subtitle warns of: `--tag` matches exactly,
            # so each group splits one subject (summ.tag_key).
            for group in summ.near_tags(counts):
                ui.label(" · ".join(f"{t} ({counts[t]})" for t in group)) \
                    .classes("sb-chip sb-chip-notable sb-filterable").mark("tag-near")
            # A wrapped row, not one tag per line: 79 tags were a long scroll.
            with ui.row().classes("items-center gap-2 flex-wrap"):
                for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                    ui.link(f"#{tag} {n}", filters.href(tag=tag)) \
                        .classes("sb-chip sb-filterable").mark("tag-chip")

    @ui.page("/docs")
    def docs_page(all: str = ""):
        """The user, 2026-10-16: "add Docs below Arcs in the sidebar and show
        all the documents we know about". Every markdown file a row is on or
        cites, the repo's, then the store's docs/, the most named first."""
        shell(ctx, author, [("Docs", None)], here="/docs", cards=False)
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

    @ui.page("/map")
    def map_page(by: str = "", show: str = "", color: str = "", q: str = ""):
        """The spec, 2026-10-29: the store's entries as a treemap, to see "where
        the work and history pile up, at a glance". Each control is a link
        to another view, as a filter chip on /notes is, and so is a search:
        the box's Enter maps only the entries it finds (the user, 2026-10-29)."""
        view = treemap.view_from({"by": by, "show": show, "color": color, "q": q})
        whole = treemap.href(replace(view, q=""))
        shell(ctx, author, [("Map", whole), (filters.describe({}, view.q), None)] if view.q
              else [("Map", None)], q=view.q, here="/map",
              search=lambda text: treemap.href(replace(view, q=text or "")))
        # as `reading` gives every page: another side's agent is someone else
        root = treemap.build(reading(ctx).loaded, view,
                             arcs={a.id: a.name for a in S.load_arcs(ctx.store_dir)},
                             path_types=frozenset({"file", "doc", *ctx.cfg.repos}))

        def axis(i: int, value: str) -> None:
            by = [*view.by, *[""] * treemap.MAX_AXES][:treemap.MAX_AXES]
            by[i] = value or ""
            ui.navigate.to(treemap.href(replace(view, by=tuple(dict.fromkeys(a for a in by if a)))))

        with ui.column().classes("sb-main gap-3"):
            with ui.row().classes("items-center gap-3 flex-wrap"):
                for i, label in enumerate(("group by", "then", "then")):
                    ui.select({**({} if i == 0 else {"": "—"}), **treemap.AXES},
                              value=view.by[i] if i < len(view.by) else "", label=label,
                              on_change=lambda e, i=i: axis(i, e.value)) \
                        .props("dense outlined options-dense").classes("min-w-[150px]") \
                        .mark(f"map-by-{i + 1}")
                ui.select(treemap.SHOWS, value=view.show, label="show",
                          on_change=lambda e: ui.navigate.to(treemap.href(replace(view, show=e.value)))) \
                    .props("dense outlined options-dense").classes("min-w-[190px]").mark("map-show")
                ui.select(treemap.COLOURS, value=view.color, label="colour",
                          on_change=lambda e: ui.navigate.to(treemap.href(replace(view, color=e.value)))) \
                    .props("dense outlined options-dense").classes("min-w-[150px]").mark("map-color")
            twice = [w for a, w in (("target", "on each object it is on or cites"),
                                    ("tag", "under each of its tags")) if a in view.by]
            if twice:
                ui.label("An entry counts once " + " and once ".join(twice) +
                         ", so the tiles add up to more than the store.") \
                    .classes("sb-subtitle").mark("map-multi")
            if not treemap.count(root):
                ui.label("no entries in this view").classes("sb-subtitle").mark("map-empty")
                return
            opts, tiles = treemap.options(
                root, view, kind_hex=lambda k: T.kind_hex(k, ctx.kinds.get(k)), palette=PALETTE,
                ramps=(MAP_EMPTY, MAP_TEXT, MAP_OPEN, MAP_NEW), now=S.now_shown())
            chart = ui.echart(opts).classes("w-full sb-map").mark("map-chart")
            ui.add_head_html(f"<script>const sbMapChart = {chart.id}</script>{_MAP_JS}")

            @ui.refreshable
            def panel(tile=None) -> None:
                if tile is None:
                    ui.label("A click on a tile lists its entries here; a click on a group "
                             "also fills the map with it.") \
                        .classes("sb-subtitle")
                    return
                rows = treemap.entries(tile)
                heading(2, f"{' › '.join(s[2] for s in tile.steps)}: "
                           f"{summ._count(len(rows), 'entry')}, open first") \
                    .classes("sb-board-title")
                with ui.element("div").classes("sb-map-rows"):
                    for n in rows[:PANEL_ROWS]:
                        with ui.link(target=filters.href(id=n.id)).classes("sb-map-row").mark("map-row"):
                            ui.label(n.kind).classes(f"sb-map-kind {kind_class(n.kind)}")
                            ui.label(treemap.status(n)).classes("sb-map-meta")
                            ui.label(f"{n.target.type}: {n.target.name}" if n.target.name
                                     else n.target.type).classes("sb-map-meta")
                            ui.label(summ.clip(" ".join(n.body.split()), 140))
                if len(rows) > PANEL_ROWS:
                    to = treemap.see_all(tile, view)
                    if to is None:
                        how = "click a tile in it" if tile.kids else "search or pick another axis"
                        ui.label(f"+{len(rows) - PANEL_ROWS} more: {how} to narrow it") \
                            .classes("sb-subtitle").mark("map-zoom")
                    else:
                        kind, q = to
                        ui.link(f"all {len(rows)} on their page",
                                target_href(q["type"], q["name"]) if kind == "object"
                                else filters.href(**q)).classes("text-body").mark("map-all")

            def tile(i):
                # the breadcrumb, the whole map and a stale key (a page drawn
                # before a reload) send no tile
                return tiles[i] if isinstance(i, int) and 0 <= i < len(tiles) else None

            def pick(e) -> None:
                if (t := tile(e.args)) is not None:
                    panel.refresh(t)

            # a group fills the map; a tile without one stays put, since the map
            # filled with one tile shows nothing the panel does not
            chart.on("componentClick", pick,
                     js_handler="(e) => { sbMapClick(e); emit(e.data ? e.data.id : null) }")
            chart.on("chart:finished", js_handler="() => sbMapLoad()")
            # back, or a press in the tile list: the tile lists its entries, as a click does
            ui.on("sbMapTile", lambda e: panel.refresh(tile(e.args)))
            # _MAP_JS fills it; out of sight until a keyboard reaches it
            ui.element("div").classes("sb-map-tiles") \
                .props('role=toolbar aria-label="map tiles"').mark("map-tiles")
            with ui.element("section").classes("sb-board w-full").mark("map-panel"):
                panel()

    @ui.page("/object")
    @gitref.heads_once()           # each repo's HEAD once a page
    def object_page(type: str = "", name: str = "", all: str = ""):
        # Guard BEFORE the shell: rendering a header for ": " and then
        # navigating away paints a frame of a page that does not exist.
        if not type or not name:
            ui.navigate.to("/notes")
            return
        rd = reading(ctx)
        # A typed or pasted name reads as `context --target` reads it: a short
        # sha finds the full one rows store. A stored name, which every link
        # here carries, skips the catalog run.
        if name not in S.stored_names(rd.loaded, type):
            with refusal_shown():
                type, name = gitref.read_target(ctx.cfg, type, name, rd.loaded)
        # A commit by its short sha, as Targets names it: the whole sha left the
        # top bar's search 120px (the design critique, 2026-11-07).
        shell(ctx, author, [(f"{type}: {name[:7] if type == 'commit' else name}", None)],
              new=(type, name, None))
        # Open work first, then the newest; a resolved row is history and
        # reads clipped (one file's page held 50 rows in full, 2026-09-27).
        rows = S.heads_for(ctx.store_dir, type, name, loaded=rd.loaded)
        live = lambda n: S.read_status(n) == "open" and not n.spec.parked   # noqa: E731
        rows.sort(key=lambda n: not live(n))      # stable: newest within each
        git_head = gitref.head_sha(ctx.cfg)
        # A page of cards, as /notes draws: 2,000 rows on one object took
        # 0.88 s on the loop every visitor shares (audit 2026-11-02).
        every = bool(all) and not S.read_only()
        shown = rows if every else rows[:_page_rows()]
        cut = f"; {len(shown)} shown, open work first" if len(shown) < len(rows) else ""
        with ui.column().classes("sb-main gap-4"):
            if type == "commit":
                _commit_card(ctx, name, rd.loaded, every)
            with ui.row().classes("items-center gap-3"):
                ui.label(f"{summ._count(len(rows), 'entry')} on or referencing this object{cut}") \
                    .classes("sb-subtitle")
                if type != "commit":
                    view_button(ctx, name, "read the file", ttype=type)
                    if (path := api.object_path(ctx, type, name)) and path.is_file():
                        file_buttons(path)
            since = gitref.commits_since(ctx.cfg, shown, git_head)
            for n in shown:
                # The page names the object: a row only referring to it names its own.
                render_note(ctx, n, lambda: ui.navigate.reload(), author=author,
                            compact=not live(n) and bool(n.spec.status), git_head=git_head,
                            since=since[n.id], seen=rd,
                            show_target=(n.target.type, n.target.name) != (type, name))
            _not_shown(len(rows) - len(shown), "entry", target_href(type, name))

    @ui.page("/arcs")
    def arcs_page():
        shell(ctx, author, [("Arcs", None)], here="/arcs", cards=False)
        with ui.column().classes("sb-main gap-4"):
            arcs.index(ctx, author=author)

    @ui.page("/arc")
    @gitref.heads_once()           # each repo's HEAD once a page
    def arc_page(id: str = "", all: str = ""):
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
            # List or Board, kept per arc in this tab. The page opens on List and
            # takes the tab's Board when the page script says the window is
            # wider than NARROW (`sbArcWide`): the server cannot see the width,
            # and a row of phase columns does not fit a phone.
            view, key, slot = {"board": False, "edit": False}, f"sb-arc-view:{id}", ui.context.slot
            header = {}                             # body's count and bar, which a drop updates

            def _progress() -> None:
                done, total = S.arc_progress(reading(ctx).loaded, id)
                header["count"].set_text(f"{done}/{total}")
                header["bar"].set_value(done / total if total else 0.0)

            def _view(board: bool) -> None:
                with slot:                          # the tab is the page's (chrome's _DRAFT)
                    app.storage.tab[key] = "board" if board else "list"
                view["board"] = board
                body.refresh()

            def _edit() -> None:
                # Shown in place: a refresh redrew every box to show one form
                view["edit"] = not view["edit"]
                header["form"].set_visibility(view["edit"] or header["archived"])
                header["edit"].props(f"aria-expanded={str(view['edit']).lower()}")

            def _wide(_) -> None:
                with slot:
                    board = app.storage.tab.get(key) == "board"
                if board and not view["board"]:
                    view["board"] = True
                    body.refresh()

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
                    header["count"] = ui.label(f"{done}/{total}").classes("sb-count") \
                        .mark("arc-count")
                    ui.space()
                    # The arc's own settings behind one quiet button: they came
                    # before every box, and a visit is to read and move boxes
                    # (the design critique, 2026-11-07).
                    header["edit"] = ui.button("Edit arc", icon="edit", color=None, on_click=_edit) \
                        .props(f"flat dense no-caps aria-expanded={str(view['edit']).lower()}") \
                        .classes("self-center text-body").mark("arc-edit")
                    # The diff dialog's toggle, drawn as Quasar's button toggle
                    # draws it: one unelevated group, the pick filled. Labels
                    # lower case, as that toggle's.
                    with ui.button_group().props("unelevated").classes("self-center sb-seg") \
                            .mark("arc-view"):
                        for word in ("list", "board"):
                            on = view["board"] == (word == "board")
                            ui.button(word, on_click=lambda w=word: _view(w == "board"),
                                      color="primary" if on else None) \
                                .props(f"unelevated dense no-caps aria-pressed={str(on).lower()}") \
                                .mark(f"arc-view-{word}")
                header["bar"] = ui.linear_progress(value=(done / total if total else 0.0),
                                                   show_value=False).props("rounded") \
                    .mark("arc-progress")

                # Shown by Edit arc, and always for an archived arc: Unarchive is in it.
                with ui.row().classes("items-center gap-2 w-full sb-card").mark("arc-form") as form:
                    rename = ui.input("rename", value=a.name).props("dense outlined")

                    def _rename():
                        if demo_says("renames the arc"):
                            return
                        if (rename.value or "").strip():
                            with refusal_shown():
                                api.rename_arc(ctx, id, rename.value)
                                ui.navigate.reload()        # the name is in the top bar

                    ui.button("Save", on_click=_rename).props("flat dense").mark("arc-rename")
                    desc = ui.input("description", value=a.description) \
                        .props("dense outlined").classes("col").style("min-width:160px") \
                        .mark("arc-description")

                    def _describe():
                        if demo_says("describes the arc"):
                            return
                        with refusal_shown():
                            with own_write(ctx, author):
                                api.describe_arc(ctx, id, desc.value or "")
                            body.refresh()

                    ui.button("Save", on_click=_describe).props("flat dense").mark("arc-describe")
                    ui.space()

                    def _archive():
                        if demo_says("archives the arc"):
                            return
                        with refusal_shown():
                            api.archive_arc(ctx, id)
                            ui.navigate.to("/arcs")

                    def _unarchive():
                        if demo_says("unarchives the arc"):
                            return
                        with refusal_shown():
                            with own_write(ctx, author):
                                api.unarchive_arc(ctx, id)
                            view["edit"] = True         # the form stays under the hand
                            body.refresh()

                    if a.archived:
                        ui.label(f"archived {S.shown(a.archived_at) if a.archived_at else ''}").classes("sb-chip")
                        ui.button("Unarchive", icon="unarchive", on_click=_unarchive) \
                            .props("flat dense").mark("arc-unarchive")
                    else:
                        # Quiet: notable means "look here", not "this one destroys"
                        ui.button("Archive", icon="archive", on_click=_archive, color=None) \
                            .props("flat dense").classes("text-body").mark("arc-archive")

                    if a.target_scope in arcs.seed_all_scopes(ctx) and arcs.seedable(ctx):
                        def _seed():
                            if demo_says("adds a box per name"):
                                return
                            try:
                                # No names: a sweep. The catalog verbatim, no
                                # resolver; api.seed raises for an empty one.
                                made_ids = []           # filled inside: own_write reads it on exit
                                with own_write(ctx, author, made_ids):
                                    made = api.seed(ctx, id, a.target_scope, None, author=author)
                                    made_ids.extend(n.id for n in made)
                            except (ValueError, RuntimeError) as e:
                                ui.notify(str(e), type="negative")
                                return
                            ui.notify(f"seeded {len(made)} new target(s)")
                            body.refresh()

                        ui.button(f"Seed all {a.target_scope}s", icon="playlist_add",
                                  on_click=_seed).props("flat dense").mark("arc-seed")

                    # A row of its own, so the description takes the rest of
                    # the first: beside it, phases squeezed it to a few words.
                    with ui.row(wrap=False).classes("items-center gap-2 w-full"):
                        ph = ui.input("phases, comma-separated", value=", ".join(a.phases or ())) \
                            .props("dense outlined").classes("col").mark("arc-phases")

                        def _phases():
                            if demo_says("sets the arc's phases"):
                                return
                            with refusal_shown():
                                # a name keeps its inner spaces; set_phases refuses an empty one
                                names = [x.strip() for x in (ph.value or "").split(",")] \
                                    if (ph.value or "").strip() else []
                                with own_write(ctx, author):
                                    api.set_phases(ctx, id, names)
                                body.refresh()

                        ui.button("Save", on_click=_phases).props("flat dense") \
                            .mark("arc-phases-save")
                header["form"], header["archived"] = form, bool(a.archived)
                form.set_visibility(view["edit"] or header["archived"])

                limit = None if all and not S.read_only() else _page_rows()
                if view["board"]:
                    arcs.board(ctx, a, body.refresh, author=author, seen=rd, limit=limit)
                else:
                    left = arcs.checklist(ctx, a, body.refresh, author=author, seen=rd,
                                          limit=limit)
                    _not_shown(left, "entry", target_href("arc", id))

                # Notes ABOUT the arc (its target or a ref), not its boxes:
                # the page showed only the checklist (the gui spec).
                about = S.heads_for(ctx.store_dir, "arc", id, loaded=rd.loaded)
                if about:
                    heading(2, "entries about this arc").classes("sb-board-title")
                    git_head = gitref.head_sha(ctx.cfg)
                    since = gitref.commits_since(ctx.cfg, about, git_head)
                    for n in about:
                        render_note(ctx, n, body.refresh, author=author, git_head=git_head,
                                    since=since[n.id], seen=rd)

            body()

            def _moved(e) -> None:
                """A card dropped on the board (chrome's _BOARD_JS): {id, phase,
                before} or {id, phase, after}. The card has moved in the browser,
                so a write redraws that card alone, and the count and bar; a
                refusal says why and redraws the board as the store has it, as
                the demo's drop does."""
                if demo_says("moves the box"):
                    body.refresh()
                    return
                d = e.args if isinstance(e.args, dict) else {}
                nid, phase = d.get("id"), d.get("phase")
                a = next((x for x in S.load_arcs(ctx.store_dir) if x.id == id), None)
                fu = next((n for n in S.arc_items(S.load(ctx.store_dir), id) if n.id == nid), None)
                with refusal_shown():
                    if a is None or fu is None:
                        raise ValueError(f"{nid} is no longer a box of this arc")
                    done = S.read_status(fu) == "resolved"
                    if phase == S.DONE:
                        if done:                    # within done: nothing to write
                            return
                        if fu.spec.verdict:
                            # A pre-registration closes with its verdict, as
                            # the checklist's tick: the card goes back to its
                            # column until the dialog saves.
                            body.refresh()
                            edit_dialog(ctx, fu, body.refresh, author=author, resolving=True)
                            return
                        with own_write(ctx, author, [nid]):
                            api.supersede(ctx, nid, author=author, status="resolved")
                    else:
                        to = {"phase": phase if a.phases else None,
                              "before": d.get("before"), "after": d.get("after")}
                        with own_write(ctx, author, [nid]):
                            (api.reopen_and_place if done else api.move_box)(
                                ctx, nid, author=author, **to)
                    arcs.redraw_box(ctx, id, nid, body.refresh, author=author)
                    _progress()
                    return
                body.refresh()

            ui.on("sbMove", _moved)
            ui.on("sbArcWide", _wide)
            ui.run_javascript("sbArcWide()")
