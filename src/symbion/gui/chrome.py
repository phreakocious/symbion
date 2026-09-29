"""Per-page shell: head CSS, the top bar, the author badge and the commit
button. The author badge is not decoration -- `serve` is launched from a shell
carrying CLAUDECODE=1, and showing the resolved identity is what makes a
misattribution visible BEFORE it is permanent in an append-only file."""
from __future__ import annotations

from nicegui import ui

from .. import api, gitref
from .filters import href
from .theme import DARK_CSS, quasar_colors, root_vars_css

# `/` focuses the search box from anywhere but a field being typed in.
_SLASH_JS = """<script>
document.addEventListener('keydown', e => {
  if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target.closest && e.target.closest('input, textarea, [contenteditable]')) return;
  const box = document.querySelector('.sb-search input');
  if (box) { e.preventDefault(); box.focus(); box.select(); }
});
</script>"""


def shell(ctx, author: str, crumbs, *, q: str = "", keep: dict | None = None) -> None:
    """`q` is the search being shown, `keep` the filters a new search keeps:
    on /notes a search narrows the list in view, elsewhere it starts one."""
    ui.dark_mode().enable()
    root = ctx.cfg.project_root
    name = root.name if root else ctx.store_dir.name   # named from outside any repo: no project
    ui.page_title(f"{name} · symbion")                 # the tab names the project
    ui.colors(**quasar_colors())
    ui.add_head_html(root_vars_css())          # no border flash on first paint
    # Dark Reader applies its theme at once and detects a dark page only after
    # the body has content; this meta stops it before it paints anything.
    ui.add_head_html('<meta name="darkreader-lock">')
    ui.add_head_html(f"<style>{DARK_CSS}</style>")
    ui.add_head_html(_SLASH_JS)
    with ui.header(elevated=False).classes("sb-header column sb-sticky px-4"):
        with ui.row().classes("items-center gap-3 w-full"):
            ui.link(f"{name} · symbion", "/").classes("sb-brand") \
                .style("text-decoration:none")
            ui.link("notes", "/notes").classes("text-body").style("text-decoration:none")
            ui.link("tags", "/tags").classes("text-body").style("text-decoration:none")
            ui.link("arcs", "/arcs").classes("text-body") \
                .style("text-decoration:none")
            _search_box(q, keep or {})
            ui.space()
            _commit_button(ctx)
            ui.label(f"writing as {author}").classes("sb-badge").mark("author-badge") \
                .tooltip("identity stamped on every write made here")
        with ui.row().classes("items-center gap-2 w-full sb-crumbs"):
            for i, (label, href) in enumerate(crumbs):
                if i:
                    ui.label("›").classes("sb-sep")
                ui.link(label, href) if href else ui.label(label)


def _search_box(q: str, keep: dict) -> None:
    box = ui.input(placeholder="search   /", value=q) \
        .props("dense outlined clearable").classes("sb-search").mark("search")
    with box.add_slot("prepend"):
        ui.icon("search", size="xs")

    def _go():
        ui.navigate.to(href(**{**keep, "q": (box.value or "").strip() or None}))

    box.on("keydown.enter", _go)
    box.tooltip("every word, in any case, in body, target, checked, result or "
                "refs; or a note id")


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

    label = f"commit {n}" if n else "commit registry"
    ui.button(label, icon="save", on_click=_do).props("flat dense no-caps") \
        .mark("commit-button").tooltip("git commit the note store")
