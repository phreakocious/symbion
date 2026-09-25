"""Per-page shell: head CSS, the top bar, the author badge and the commit
button. The author badge is not decoration -- `serve` is launched from a shell
carrying CLAUDECODE=1, and showing the resolved identity is what makes a
misattribution visible BEFORE it is permanent in an append-only file."""
from __future__ import annotations

from nicegui import ui

from .. import api, gitref
from .theme import DARK_CSS, quasar_colors, root_vars_css


def shell(ctx, author: str, crumbs) -> None:
    ui.dark_mode().enable()
    root = ctx.cfg.project_root
    name = root.name if root else ctx.store_dir.name   # named from outside any repo: no project
    ui.page_title(f"{name} · symbion")                 # the tab names the project
    ui.colors(**quasar_colors())
    ui.add_head_html(root_vars_css())          # no border flash on first paint
    ui.add_head_html(f"<style>{DARK_CSS}</style>")
    with ui.header(elevated=False).classes("sb-header column sb-sticky px-4"):
        with ui.row().classes("items-center gap-3 w-full"):
            ui.link(f"{name} · symbion", "/").classes("sb-brand") \
                .style("text-decoration:none")
            ui.link("notes", "/notes").classes("text-body").style("text-decoration:none")
            ui.link("tags", "/tags").classes("text-body").style("text-decoration:none")
            ui.link("arcs", "/arcs").classes("text-body") \
                .style("text-decoration:none")
            ui.space()
            _commit_button(ctx)
            ui.label(f"writing as {author}").classes("sb-badge").mark("author-badge") \
                .tooltip("identity stamped on every write made here")
        with ui.row().classes("items-center gap-2 w-full sb-crumbs"):
            for i, (label, href) in enumerate(crumbs):
                if i:
                    ui.label("›").classes("sb-sep")
                ui.link(label, href) if href else ui.label(label)


def _commit_button(ctx) -> None:
    """The store is a sibling git repo. A GUI that writes but cannot commit
    only grows the number the SessionStart hook nags about."""
    n, registry = gitref.uncommitted(ctx.store_dir)
    if not n and not registry:
        return

    def _do():
        ok = api.commit(ctx, "notes: via symbion serve")
        ui.notify("committed" if ok else "nothing to commit")
        ui.navigate.reload()

    label = f"commit {n}" if n else "commit registry"
    ui.button(label, icon="save", on_click=_do).props("flat dense no-caps") \
        .mark("commit-button").tooltip("git commit the note store")
