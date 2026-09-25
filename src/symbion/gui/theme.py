"""Palette and dark theme. Every 6-digit hex in gui/ lives here, audited by
tests/test_gui_theme.py -- a hue tweak is then one edit, not a grep.

Ported from the research notebook symbion was extracted from. Its Plotly
constants are gone: symbion has no charts, and a constant kept "in case" is
a hex the audit must keep excusing.
Class prefix is `sb-`, and `--mono` moved into the :root block so the font is
set by the same first-paint style that kills the light-border flash.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    """Every hex in the app. Solarized-derived foreground over near-black.
    Field names are role-semantic so a hue tweak is a one-line edit here."""
    # backgrounds
    bg_page: str = "#0a0a0a"
    bg_surface: str = "#0e1115"
    bg_panel: str = "#0e1115"
    border: str = "#1f2629"
    # text (Solarized base scale)
    text_emph: str = "#eee8d5"
    text_body: str = "#93a1a1"
    text_muted: str = "#657b83"
    text_dim: str = "#586e75"
    # status -- semantic, not hue-named
    good: str = "#859900"     # green
    notable: str = "#b58900"  # yellow -- "look here"
    bad: str = "#cb4b16"      # orange -- everyday bad / warn
    crit: str = "#dc322f"     # red -- critical
    info: str = "#268bd2"     # blue
    accent: str = "#2aa198"   # cyan -- selection / primary
    rare: str = "#6c71c4"     # violet


PALETTE = Palette()

MONO_STACK = '"DejaVu Sans Mono", Menlo, Consolas, monospace'


def quasar_colors() -> dict[str, str]:
    """Kwargs for ui.colors(...): Quasar slots + brand tokens (--q-<name> vars)."""
    p = PALETTE
    return {
        "primary": p.accent, "secondary": p.info, "accent": p.rare,
        "dark": p.bg_surface, "dark_page": p.bg_page,
        "positive": p.good, "negative": p.bad, "info": p.info, "warning": p.notable,
        "good": p.good, "notable": p.notable, "bad": p.bad, "crit": p.crit,
        "emph": p.text_emph, "body": p.text_body, "muted": p.text_muted, "dim": p.text_dim,
        "panel": p.bg_panel, "border": p.border,
    }


def root_vars_css() -> str:
    """Emit the --q-* brand vars in :root so borders/surfaces resolve to dark on
    the FIRST paint -- kills the light-border flash before ui.colors() runs
    post-connect. Underscores are normalized to hyphens to match the CSS vars
    that ui.colors() generates (e.g. dark_page -> --q-dark-page)."""
    decls = ";".join(f"--q-{k.replace('_', '-')}:{v}" for k, v in quasar_colors().items())
    return (f"<style>:root{{{decls};--mono:{MONO_STACK}}}"
            f"html,body{{background:var(--q-dark-page);font-family:var(--mono)}}</style>")


DARK_CSS = """
/* brand-token utility classes (Quasar only auto-generates them for its own slots) */
.text-emph    { color: var(--q-emph) !important; }
.text-body    { color: var(--q-body) !important; }
.text-muted   { color: var(--q-muted) !important; }
.text-dim     { color: var(--q-dim) !important; }
.text-good    { color: var(--q-good) !important; }
.text-notable { color: var(--q-notable) !important; }
.text-bad     { color: var(--q-bad) !important; }
.text-crit    { color: var(--q-crit) !important; }
.bg-panel     { background: var(--q-panel) !important; }
.q-page { padding: 0 !important; }
/* The top bar is position:sticky (.sb-sticky) -- it sits in normal flow and reserves
   its own height. But Quasar's QLayout ALSO pads q-page-container by the header
   height as if it were a fixed header, double-counting the space and leaving a
   ~header-tall gap before content. Drop the redundant reservation; the sticky
   header already holds its slot. */
.q-page-container { padding-top: 0 !important; }

/* header + breadcrumb */
.sb-header { background: var(--q-panel) !important; border-bottom: 1px solid var(--q-border);
             box-shadow: none !important; min-height: 46px; }
.sb-brand  { color: var(--q-emph); font-weight: 500; letter-spacing: 0.02em; }
.sb-crumbs { color: var(--q-dim); font-size: 12px; }
.sb-crumbs a { color: var(--q-body); text-decoration: none; }
.sb-crumbs a:hover { color: var(--q-primary); }
.sb-sep    { color: var(--q-dim); }
.sb-sticky { position: sticky; top: 0; z-index: 100; }

/* main + cards */
.sb-main   { padding: 18px 22px; }
.sb-title  { font-size: 16px; color: var(--q-emph); font-weight: 500; }
.sb-subtitle { font-size: 12px; color: var(--q-muted); }
.sb-card   { background: var(--q-panel) !important; border: 1px solid var(--q-border);
             border-radius: 6px; padding: 10px 14px; }
.sb-stat-label { color: var(--q-muted); font-size: 11px; text-transform: uppercase;
                 letter-spacing: .04em; }

/* badges */
.sb-badge { font-family: var(--mono); font-size: 11px; padding: 1px 8px; border-radius: 3px;
            border: 1px solid var(--q-border); color: var(--q-muted); }

/* notes layer -- chips/rows (colors via brand vars; hex-audit safe) */
.sb-note { border-left: 2px solid var(--q-border); padding: 2px 0 6px 10px; }
.sb-note-meta { color: var(--q-dim); font-family: var(--mono); font-size: 11px; }
.sb-chip { font-family: var(--mono); font-size: 10px; padding: 0 6px; border-radius: 3px;
           border: 1px solid var(--q-border); color: var(--q-muted); text-decoration: none; }
.sb-chip:hover    { border-color: var(--q-primary); color: var(--q-primary); }
.sb-chip-bug  { color: var(--q-bad); }
.sb-chip-decision { color: var(--q-info); }
.sb-chip-task { color: var(--q-notable); }
.sb-chip-check    { color: var(--q-accent); }
.sb-chip-open     { color: var(--q-notable); }
.sb-chip-resolved { color: var(--q-good); }
.sb-chip-good     { color: var(--q-good); }
.sb-chip-notable  { color: var(--q-notable); }
.sb-chip-bad      { color: var(--q-bad); }

/* Quasar portals menus (ui.select options) and tooltips to <body>, outside the
   styled tree: nothing under .sb-* reaches them, and the options rendered
   low-contrast grey (measured 2026-09-08, the edit dialog's arc select). One
   rule per portal root, brand vars only so the hex audit stays clean. */
.q-menu { background: var(--q-panel) !important; color: var(--q-body) !important;
          border: 1px solid var(--q-border); font-family: var(--mono); }
.q-menu .q-item { color: var(--q-body); }
.q-menu .q-item--active, .q-menu .q-item.q-manual-focusable--focused,
.q-menu .q-item:hover { color: var(--q-primary); }
.q-tooltip { background: var(--q-panel) !important; color: var(--q-emph) !important;
             border: 1px solid var(--q-border); font-family: var(--mono); font-size: 11px; }

/* markdown bodies sit inside a note row; keep them from inheriting page margins */
.sb-note .nicegui-markdown > *:first-child { margin-top: 2px; }
.sb-note .nicegui-markdown > *:last-child  { margin-bottom: 0; }
.sb-note .nicegui-markdown code { font-family: var(--mono); font-size: 12px; }
/* A note is a paragraph, not a document. A real <h1> in a list of eighty rows
   dominates the board, and markdown eats a line-leading `#tag` into one -- the
   harvest strips those now, but a deliberate heading still has to sit down. */
.sb-note .nicegui-markdown h1,
.sb-note .nicegui-markdown h2,
.sb-note .nicegui-markdown h3,
.sb-note .nicegui-markdown h4,
.sb-note .nicegui-markdown h5,
.sb-note .nicegui-markdown h6 { font-size: 13px; font-weight: 600; margin: 4px 0 2px;
                                color: var(--q-emph); line-height: 1.3; }
"""
