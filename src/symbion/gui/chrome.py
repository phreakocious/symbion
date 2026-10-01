"""Per-page shell: head CSS, the top bar, the author badge and the commit
button. The author badge is not decoration -- `serve` is launched from a shell
carrying CLAUDECODE=1, and showing the resolved identity is what makes a
misattribution visible BEFORE it is permanent in an append-only file."""
from __future__ import annotations

from importlib import metadata

from nicegui import ui

from .. import api, gitref
from .. import store as S
from .. import summary as summ
from .filters import href
from .notes import add_form, kind_class
from .theme import DARK_CSS, FONTS_HTML, LOGO_SVG, NARROW, quasar_colors, root_vars_css

# From anywhere but a field being typed in or an open dialog: `/` focuses the
# search box, `n` and `?` press the buttons they name. What is typed in the
# box hides the cards on the page that lack a word of it (`sbFilter`): every
# word, in any case, in the card's text or its id.
_KEYS_JS = """<script>
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
function sbFilter(text) {
  const words = (text || '').toLowerCase().split(/\\s+/).filter(Boolean);
  for (const card of document.querySelectorAll('.sb-main .sb-note')) {
    const hay = (card.textContent + ' ' + (card.dataset.id || '')).toLowerCase();
    card.classList.toggle('sb-filtered', !words.every(w => hay.includes(w)));
  }
  for (const board of document.querySelectorAll('.sb-board'))
    board.classList.toggle('sb-filtered',
      words.length > 0 && !board.querySelector('.sb-note:not(.sb-filtered)'));
}
</script>"""

# What `?` lists. The kinds are the store's, read at render.
_KEYS = (("/", "filter this page"), ("Enter", "in the filter: search the whole store"),
         ("n", "new note"), ("?", "this list"), ("Esc", "close a dialog"),
         ("⌘/Ctrl Enter", "add the note being written"),
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
    # the body has content; this meta stops it before it paints anything.
    ui.add_head_html('<meta name="darkreader-lock">')
    ui.add_head_html(f"<style>{DARK_CSS}</style>")
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
            ui.button(icon="menu", on_click=drawer.toggle) \
                .props('flat dense round aria-label="menu"').classes("sb-narrow text-body")
            with ui.row(wrap=False).classes("items-center gap-2 sb-crumbs"):
                for i, (label, to) in enumerate(crumbs):
                    if i:
                        ui.label("›").classes("sb-sep")
                    last = ui.link(label, to) if to else ui.label(label)
                last.classes("sb-here").mark("page-title")
            ui.space()
            _new_note_button(ctx, author, *new)
            _commit_button(ctx)
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
        with ui.dialog() as dialog, ui.card().classes("bg-panel sb-new-card"):
            # The name in its own case: the label's capitals rewrote a path.
            with ui.row().classes("items-center gap-2"):
                ui.label("new note on").classes("sb-stat-label")
                ui.label(f"{target_type}: {target_name}" if target_name else "project") \
                    .classes("sb-chip sb-target").mark("new-note-on")
            add_form(ctx, target_type, target_name, lambda: ui.navigate.reload(),
                     author=author, arc_id=arc_id)
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
                                       + ", ".join(ctx.kinds)),):
                ui.label(key).classes("sb-chip")
                ui.label(what)
    return dialog


SOURCE = "https://github.com/phreakocious/symbion"

# The sidebar's places: (label, path, Material icon).
_PLACES = (("Notebook", "/", "menu_book"), ("All notes", "/notes", "notes"),
           ("Tags", "/tags", "tag"), ("Arcs", "/arcs", "linear_scale"))


def _sidebar(ctx, author: str, name: str, heads, here: str) -> None:
    """Brand, places, an open count per kind that has a status, and who is
    writing. The badge is not decoration: see the module docstring."""
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
        kinds = [k for k, spec in ctx.kinds.items() if spec.status and not spec.parked]
        if kinds:
            with ui.element("div").classes("sb-nav"):
                ui.label("open").classes("sb-nav-head")
                for k in kinds:
                    n = len(summ.open_notes(heads, k))
                    with ui.link(target=href(kind=k, status="open")).classes(
                            f"sb-nav-item {kind_class(k)}" + ("" if n else " sb-zero")) \
                            .mark(f"open-{k}"):
                        ui.element("span").classes("sb-dot")
                        ui.label(k)
                        ui.label(str(n)).classes("sb-nav-n")
        with ui.element("div").classes("sb-who"):
            ui.label((author or "?")[:1]).classes("sb-avatar")
            ui.label(f"writing as {author}").classes("sb-badge").mark("author-badge") \
                .tooltip("identity stamped on every write made here")
            ui.button(icon="keyboard", on_click=_once(lambda: _keys_dialog(ctx))) \
                .props('flat dense round size=sm aria-label="keyboard shortcuts"') \
                .classes("sb-keys").mark("keys").tooltip("keyboard shortcuts (?)")
        ui.link(f"symbion {metadata.version('symbion')}", SOURCE, new_tab=True) \
            .classes("sb-source").mark("source").tooltip("source on GitHub")


def _search_box(q: str, keep: dict) -> None:
    """Typing filters the cards on this page, in the browser; Enter searches
    the whole store (the owner, 2026-10-01)."""
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

    box.on("update:value", js_handler="(v) => sbFilter(v)")
    box.on("keydown.enter", _go, js_handler="(e) => emit(e.target.value)")
    box.tooltip("typing filters this page; Enter searches the store: every word, "
                "in any case, in body, target, checked, result or refs; or a note id")


def _commit_button(ctx) -> None:
    """The store is a sibling git repo. A GUI that writes but cannot commit
    only grows the number the SessionStart hook nags about."""
    n, registry = gitref.uncommitted(ctx.store_dir)
    if not n and not registry:
        return

    def _do():
        try:
            ok = api.commit(ctx, "notes: via symbion serve")
        except RuntimeError as e:     # a hook refused: no reload, so it stays read
            ui.notify(str(e), type="negative", multi_line=True,
                      close_button="dismiss", timeout=0)
            return
        ui.notify("committed" if ok else "nothing to commit")
        ui.navigate.reload()

    # On a phone the word goes and the count stays: the icon says commit.
    with ui.button(icon="save", on_click=_do).props("outline dense no-caps color=notable") \
            .classes("px-2 sb-commit").mark("commit-button") \
            .tooltip("git commit the note store"):
        ui.label("commit").classes("sb-btn-word")
        ui.label(str(n) if n else "registry")
