"""Per-page shell: head CSS, the top bar, the author badge and the commit
button. The author badge is not decoration -- `serve` is launched from a shell
carrying CLAUDECODE=1, and showing the resolved identity is what makes a
misattribution visible BEFORE it is permanent in an append-only file."""
from __future__ import annotations

from importlib import metadata

from nicegui import run, ui

from .. import api, gitref
from .. import store as S
from .. import summary as summ
from . import servers
from .filters import href
from .notes import add_form, ago, kind_class
from .theme import (DARK_CSS, FONTS_HTML, LOGO_SVG, NARROW, kind_css, quasar_colors,
                    root_vars_css)

# From anywhere but a field being typed in or an open dialog: `/` focuses the
# search box, `n` and `?` press the buttons they name. What is typed in the
# box hides the cards on the page that lack a word of it (`sbFilter`): every
# word, in any case, in the card's text or its id. When it hides them all, a
# pause in the typing searches the store, as Enter does: a superseded id, or a
# row on no card of this page, read as gone (the owner, 2026-10-05).
_KEYS_JS = """<script>
let sbExpandTimer;
const sbKeys = {'/': '.sb-search input', 'n': '.sb-new', '?': '.sb-keys'};
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey || !Object.hasOwn(sbKeys, e.key)) return;
  if (e.target.closest && e.target.closest('input, textarea, [contenteditable]')) return;
  if (document.querySelector('.q-dialog')) return;
  const el = document.querySelector(sbKeys[e.key]);
  if (!el) return;
  e.preventDefault();
  if (e.key === '/') { el.focus(); el.select(); } else el.click();
});
function sbFilter(text, expand) {
  const words = (text || '').toLowerCase().split(/\\s+/).filter(Boolean);
  for (const card of document.querySelectorAll('.sb-main .sb-note')) {
    const hay = (card.textContent + ' ' + (card.dataset.id || '')).toLowerCase();
    card.classList.toggle('sb-filtered', !words.every(w => hay.includes(w)));
  }
  for (const board of document.querySelectorAll('.sb-board'))
    board.classList.toggle('sb-filtered',
      words.length > 0 && !board.querySelector('.sb-note:not(.sb-filtered)'));
  clearTimeout(sbExpandTimer);
  if (words.length && !document.querySelector('.sb-main .sb-note:not(.sb-filtered)'))
    sbExpandTimer = setTimeout(() => expand(text), 700);
}
</script>"""

# What `?` lists. The kinds are the store's, read at render.
_KEYS = (("/", "filter this page"), ("Enter", "in the filter: search the whole store"),
         ("n", "new note"), ("?", "this list"), ("Esc", "close a dialog"),
         ("⌘/Ctrl Enter", "add the note being written, or save an edit"),
         ("Shift Enter", "add it and write another"),
         ("#tag", "in a note: adds the tag"))


def shell(ctx, author: str, crumbs, *, q: str = "", keep: dict | None = None,
          here: str = "", new=("project", None, None)) -> None:
    """`crumbs` end in the page's title, which the top bar shows: a title of
    its own under the bar left a band of nothing above it (the owner,
    2026-10-01). `q` is the search being shown, `keep` the filters a new
    search keeps: on /notes a search narrows the list in view, elsewhere it
    starts one. `here` is the sidebar place this page is (`/`, `/notes`,
    `/tags`, `/arcs`), and `new` the (type, name, arc) a new note starts on."""
    ui.dark_mode().enable()
    root = ctx.cfg.project_root
    name = root.name if root else ctx.store_dir.name   # named from outside any repo: no project
    ui.page_title(f"{name} · symbion")                 # the tab names the project
    ui.colors(**quasar_colors())
    ui.add_head_html(root_vars_css())          # no border flash on first paint
    ui.add_head_html(FONTS_HTML)
    # Dark Reader applies its theme at once and detects a dark page only after
    # the body has content; this meta stops its theme. Its fallback style does
    # not wait for the theme: at document_start, before this head is parsed,
    # it sets every border a light brown !important, and only the lock check takes
    # it off, once Dark Reader's background answers. A slow answer showed every
    # card in a bright full border (Dark Reader 4.9.133, 2026-10-01). The
    # script takes it off first.
    ui.add_head_html('<meta name="darkreader-lock"><script>'
                     'document.querySelectorAll(".darkreader--fallback").forEach(e => e.remove())'
                     '</script>')
    ui.add_head_html(f"<style>{DARK_CSS}{kind_css(ctx.kinds)}</style>")
    ui.add_head_html(_KEYS_JS)
    heads = S.heads(S.load(ctx.store_dir))
    # value=None: open beside the page on a wide window, a slide-over behind
    # the menu button on a narrow one (open by default, it covered a phone).
    with ui.left_drawer(value=None, fixed=True, bordered=False) \
            .props(f"width=248 breakpoint={NARROW}").classes("sb-side") as drawer:
        _sidebar(ctx, author, name, heads, here)
    with ui.header(elevated=False).classes("sb-header"):
        with ui.row(wrap=False).classes("items-center gap-3 w-full sb-topbar") \
                .style("min-height:56px"):
            ui.button(icon="menu", on_click=drawer.toggle, color=None) \
                .props('flat dense round aria-label="menu"').classes("sb-narrow text-body") \
                .mark("menu")
            with ui.row(wrap=False).classes("items-center gap-2 sb-crumbs"):
                for i, (label, to) in enumerate(crumbs):
                    if i:
                        ui.label("›").classes("sb-sep")
                    last = ui.link(label, to) if to else ui.label(label)
                last.classes("sb-here").mark("page-title")
            ui.space()
            # Left of New note, so that button stays put as these come and go
            # (the owner, 2026-10-02).
            _git_buttons(ctx)
            _new_note_button(ctx, author, *new)
            _search_box(q, keep or {})
            with ui.element("div").classes("sb-who-top sb-narrow") \
                    .tooltip(f"writing as {author}: identity stamped on every write made here"):
                ui.label((author or "?")[:1]).classes("sb-avatar")
                ui.label(author).classes("sb-badge").mark("author-badge-top")
    # The sidebar runs the full height; the top bar sits beside it. After
    # both: creating a header or drawer rewrites the layout's view.
    ui.context.client.layout.props('view="lHh LpR lFf"')


def _once(build):
    """An opener for the dialog `build` makes on the first call: one per
    page, so a draft closed with Esc is there when it opens again."""
    made = []

    def _open():
        if not made:
            made.append(build())
        made[0].open()
    return _open


def _new_note_button(ctx, author: str, target_type, target_name, arc_id) -> None:
    """The composer, from every page: on the page's object, or the project
    where a page has none. It sat on two pages, as the page's top."""
    def build():
        added = []                  # by Shift Enter: the page shows them on close
        with ui.dialog() as dialog, ui.card().classes("bg-panel sb-new-card"):
            add_form(ctx, target_type, target_name, lambda: ui.navigate.reload(),
                     author=author, arc_id=arc_id, another=lambda: added.append(1))
        dialog.on_value_change(lambda e: added and not e.value and ui.navigate.reload())
        return dialog

    with ui.button(icon="add", on_click=_once(build)) \
            .props('unelevated dense no-caps color=primary aria-label="new note"') \
            .classes("px-2 sb-new").mark("new-note").tooltip("new note (n)"):
        ui.label("New note").classes("sb-btn-word")


def _keys_dialog(ctx):
    with ui.dialog() as dialog, ui.card().classes("bg-panel"):
        ui.label("keyboard").classes("sb-stat-label")
        with ui.element("div").classes("sb-keys-list").mark("keys-list"):
            for key, what in _KEYS + (("!kind", "in a note: sets its kind, one of "
                                       + ", ".join(ctx.kinds)
                                       + "; its first letters do, while one kind "
                                         "alone starts with them"),):
                ui.label(key).classes("sb-chip")
                ui.label(what)
    return dialog


SOURCE = "https://github.com/phreakocious/symbion"

# The sidebar's places: (label, path, Material icon).
_PLACES = (("Notebook", "/", "menu_book"), ("All notes", "/notes", "notes"),
           ("Targets", "/targets", "my_location"), ("Tags", "/tags", "tag"),
           ("Arcs", "/arcs", "linear_scale"))


def _sidebar(ctx, author: str, name: str, heads, here: str) -> None:
    """Brand, places, an open count per kind that has a status, the parked
    ones under their own head (an open idea showed nowhere: the owner,
    2026-10-02), a row count per kind with none, and who is writing. The badge is not decoration: see the
    module docstring."""
    with ui.element("div").classes("sb-side-inner"):
        with ui.link(target="/").classes("sb-brand").mark("brand-link"):
            ui.html(LOGO_SVG, sanitize=False)
            with ui.column().classes("gap-0"):
                ui.label(name).classes("sb-brand-name").mark("brand")
                ui.label(f"symbion · {summ._count(len(heads), 'note')}").classes("sb-brand-sub")
        with ui.element("nav").classes("sb-nav").props('aria-label="places"'):
            for label, path, icon in _PLACES:
                with ui.link(target=path).classes(
                        "sb-nav-item" + (" sb-on" if path == here else "")) \
                        .mark(f"place-{path.strip('/') or 'home'}"):
                    ui.icon(icon)
                    ui.label(label)
        for head, parked in (("open", False), ("parked", True)):
            kinds = [k for k, spec in ctx.kinds.items() if spec.status and spec.parked == parked]
            if not kinds:
                continue
            with ui.element("div").classes("sb-nav"):
                ui.label(head).classes("sb-nav-head")
                for k in kinds:
                    _kind_item(k, len(summ.open_notes(heads, k)),
                               href(kind=k, status="open"), f"open-{k}")
        # The kinds with no status, which showed nowhere here (the owner,
        # 2026-10-02): no open count, so each shows its rows.
        plain = [k for k, spec in ctx.kinds.items() if not spec.status]
        if plain:
            with ui.element("div").classes("sb-nav"):
                ui.label("records").classes("sb-nav-head")
                for k in plain:
                    _kind_item(k, sum(h.kind == k for h in heads), href(kind=k), f"kind-{k}")
        _closed(heads)
        _other_stores(ctx)
        with ui.element("div").classes("sb-who"):
            ui.label((author or "?")[:1]).classes("sb-avatar")
            ui.label(f"writing as {author}").classes("sb-badge").mark("author-badge") \
                .tooltip("identity stamped on every write made here")
            ui.button(icon="keyboard", on_click=_once(lambda: _keys_dialog(ctx))) \
                .props('flat dense round size=sm aria-label="keyboard shortcuts"') \
                .classes("sb-keys").mark("keys").tooltip("keyboard shortcuts (?)")
        ui.link(f"symbion {metadata.version('symbion')}", SOURCE, new_tab=True) \
            .classes("sb-source").mark("source").tooltip("source on GitHub")


def _kind_item(k: str, n: int, target: str, mark: str) -> None:
    with ui.link(target=target).classes(
            f"sb-nav-item {kind_class(k)}" + ("" if n else " sb-zero")).mark(mark):
        ui.element("span").classes("sb-dot")
        ui.label(k)
        ui.label(str(n)).classes("sb-nav-n")


def _closed(heads, n: int = 5) -> None:
    """The newest rows resolved, each a link to itself: a resolve writes a
    new head, so its stamp is when the row closed."""
    rows = [h for h in S.newest_first(heads) if S.read_status(h) == "resolved"][:n]
    if not rows:
        return
    with ui.element("div").classes("sb-nav"):
        ui.link("recently closed", href(status="resolved")).classes("sb-nav-head") \
            .mark("closed-all").tooltip("every resolved row")
        for r in rows:
            text = r.body or (f"{r.target.type}: {r.target.name}"
                              if r.target.name else r.target.type)
            with ui.link(target=href(id=r.id)) \
                    .classes(f"sb-nav-item sb-closed {kind_class(r.kind)}") \
                    .mark("closed-item").tooltip(summ.clip(text, summ.BODY_CHARS)):
                ui.element("span").classes("sb-dot")
                ui.label(summ.clip(text, 120)).classes("sb-closed-text")
                ui.label(ago(r.created_at)).classes("sb-nav-n")


def _other_stores(ctx) -> None:
    """Every other store a `serve` runs on here, a link to its notebook: a
    click switches, a cmd-click opens it beside this one. Named by the
    store's directory, `<repo>-notes` less its suffix: the repo a serve was
    started from need not be the store's (`--dir`), and two read the same."""
    here = str(ctx.store_dir.resolve())
    others = {r["store"]: r for r in servers.running() if r["store"] != here}
    if not others:
        return
    with ui.element("div").classes("sb-nav"):
        ui.label("other stores").classes("sb-nav-head")
        for path, r in sorted(others.items()):
            with ui.link(target=r["url"]).classes("sb-nav-item").mark("store-item") \
                    .tooltip(f"{path}\n{r['url']}"):
                ui.icon("swap_horiz")
                ui.label(servers.name(path))


def _search_box(q: str, keep: dict) -> None:
    """Typing filters the cards on this page, in the browser; Enter searches
    the whole store (the owner, 2026-10-01), and so does a pause once the
    filter hides every card (2026-10-05)."""
    box = ui.input(placeholder="Filter · Enter searches all", value=q) \
        .props('dense outlined clearable aria-label="search"').classes("sb-search") \
        .mark("search")
    with box.add_slot("prepend"):
        ui.icon("search", size="xs")
    with box.add_slot("append"):
        ui.label("/").classes("sb-chip sb-mono").style("line-height:16px;padding:0 5px")

    def _go(e):
        # The field's own text: the key can reach the server before the value.
        text = e.args if isinstance(e.args, str) else box.value
        ui.navigate.to(href(**{**keep, "q": (text or "").strip() or None}))

    def _expand(e):
        # The page that shows this search can hide all its cards: an id hit's
        # card lacks the id's text. Reloading it would change nothing.
        if (e.args if isinstance(e.args, str) else "").split() != q.split():
            _go(e)

    box.on("update:value", _expand, js_handler="(v) => sbFilter(v, emit)")
    box.on("keydown.enter", _go, js_handler="(e) => emit(e.target.value)")
    box.tooltip("typing filters this page, and searches the store when nothing here "
                "matches; Enter searches the store: every word, in any case, in body, "
                "target, checked, result, refs or tags; or a note id")


# Seconds between reads of the store's git state for the two buttons.
_GIT_EVERY = 5


def _git_buttons(ctx) -> None:
    """Commit and push, read again every `_GIT_EVERY` seconds and redrawn
    when a count moves: a commit or push made at a terminal left them on the
    page, and rows written there never showed the commit button (the owner,
    2026-10-01)."""
    def read():
        return (*gitref.uncommitted(ctx.store_dir), gitref.unpushed(ctx.store_dir))

    @ui.refreshable
    def buttons(state):
        _commit_button(ctx, *state[:2])
        _push_button(ctx, state[2])

    shown = [read()]
    buttons(shown[0])

    async def tick():
        state = await run.io_bound(read)      # git, off the event loop
        if state is not None and state != shown[0]:    # None: the app is stopping
            shown[0] = state
            buttons.refresh(state)
    ui.timer(_GIT_EVERY, tick, immediate=False)    # the page has just read it


def _commit_button(ctx, n: int, registry: bool) -> None:
    """The store is a sibling git repo. A GUI that writes but cannot commit
    only grows the number the SessionStart hook nags about."""
    if not n and not registry:
        return

    def _do():
        try:
            ok = api.commit(ctx, "notes: via symbion serve")
        except RuntimeError as e:     # a hook refused: no reload, so it stays read
            ui.notify(str(e), type="negative", multi_line=True,
                      close_button="dismiss", timeout=0)
            return
        ui.notify(gitref.committed(ctx.store_dir) if ok else "nothing to commit")
        ui.navigate.reload()

    # On a phone the word goes and the count stays: the icon says commit.
    with ui.button(icon="save", on_click=_do).props("outline dense no-caps color=notable") \
            .classes("px-2 sb-commit").mark("commit-button") \
            .tooltip("git commit the note store"):
        ui.label("commit").classes("sb-btn-word")
        ui.label(str(n) if n else "registry")


def _push_button(ctx, n: int | None) -> None:
    """Committed is not off this disk. Quieter than commit: nothing here is
    at risk of being lost by a crash, only by the disk."""
    if not n:                       # None: no remote, nowhere to push
        return

    async def _do():
        # A thread: a push waits on the network, and the event loop serves
        # every open page.
        r = await run.io_bound(api.push, ctx, capture=True)
        if r.returncode:
            ui.notify(r.stderr.strip() or f"git push exited {r.returncode}", type="negative",
                      multi_line=True, close_button="dismiss", timeout=0)
            return
        ui.notify("pushed")
        ui.navigate.reload()

    with ui.button(icon="cloud_upload", on_click=_do).props("flat dense no-caps color=muted") \
            .classes("px-2 sb-push").mark("push-button") \
            .tooltip(f"git push the note store: {summ._count(n, 'commit')} not on its remote"):
        ui.label("push").classes("sb-btn-word")
        ui.label(str(n))
