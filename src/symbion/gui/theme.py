"""Palette and dark theme. Every 6-digit hex in gui/ lives here, audited by
tests/test_gui_theme.py -- a hue tweak is then one edit, not a grep.

Catppuccin Mocha, as the terminal prints: the text, status and kind colours
are term.py's, so `list` and `serve` agree (2026-10-01; Solarized before).
Only the surfaces are the GUI's own. Class prefix is `sb-`, and `--mono` sits
in the :root block so the font is set by the same first-paint style that
kills the light-border flash.

The layout (2026-10-01): a sidebar of places and open counts, one centred
column, and each row a card. A card's kind shows as one mark on its top-left
corner -- a short bend down the left side, a solid run along the top that
breaks into dashes, shorter and fainter as they go -- in place of a
full-height left border in the kind's colour, which read as every other
dashboard's.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import term as T


@dataclass(frozen=True)
class Palette:
    """Every colour in the app. Field names are role-semantic so a hue tweak
    is a one-line edit, here or in term.py."""
    # backgrounds: Mocha's crust (the page), mantle (cards, sidebar) and base
    # (a field or toolbar raised inside a card)
    bg_page: str = "#11111b"
    bg_surface: str = "#181825"
    bg_panel: str = "#181825"
    bg_raise: str = "#1e1e2e"
    border: str = "#313244"       # surface0
    border_hi: str = "#45475a"    # surface1: a hovered card, a ref's dashes
    # text: the four steps a terminal row prints in. Small text on a card
    # takes `muted` (4.7:1 on mantle); `dim` (3.6:1) is for marks, not words:
    # the empty resolve ring is one (border_hi read 1.9:1, 2026-10-02).
    text_emph: str = T.TEXT
    text_body: str = T.BODY
    text_muted: str = T.META
    text_dim: str = T.FADED
    # status -- semantic, not hue-named
    good: str = T.GOOD
    notable: str = T.WARN     # "look here"
    bad: str = T.BAD
    info: str = "#74c7ec"     # sapphire
    accent: str = T.HUE["blue"]       # links, selection, primary
    rare: str = T.HUE["magenta"]      # mauve


PALETTE = Palette()

# Geist for prose, Geist Mono for what is code-shaped or lines up in columns:
# ids, dates, chips, code. Both ship in the package (OFL, data/fonts/) and
# are served from loopback with the page: from Google Fonts, every page load
# reached a third party (2026-10-01).
FONTS_DIR = Path(__file__).resolve().parents[1] / "data" / "fonts"
FONTS_URL = "/fonts"
MONO_STACK = '"Geist Mono", "DejaVu Sans Mono", Menlo, Consolas, monospace'
SANS_STACK = 'Geist, system-ui, -apple-system, "Segoe UI", "Helvetica Neue", sans-serif'
FONTS_HTML = "<style>" + "".join(
    f'@font-face{{font-family:"{family}";src:url({FONTS_URL}/{file}) format("woff2");'
    'font-weight:100 900;font-display:swap}'
    for family, file in (("Geist", "Geist-Variable.woff2"),
                         ("Geist Mono", "GeistMono-Variable.woff2"))) + "</style>"

# px: at this layout width and below, the sidebar is a slide-over behind the
# menu button (Quasar's drawer `breakpoint`), and the top bar carries what
# the hidden sidebar would show
NARROW = 900

# Two overlapping rings, one per partner, in place of NiceGUI's own icon.
_RINGS = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"{size}>'
    f'<circle cx="12" cy="16" r="8" fill="none" stroke="{PALETTE.accent}" stroke-width="3.5"/>'
    f'<circle cx="20" cy="16" r="8" fill="none" stroke="{PALETTE.rare}" stroke-width="3.5"/>'
    '</svg>')
FAVICON_SVG = _RINGS.format(size="")
LOGO_SVG = _RINGS.format(size=' width="24" height="24" aria-hidden="true"')


def quasar_colors() -> dict[str, str]:
    """Kwargs for ui.colors(...): Quasar slots + brand tokens (--q-<name> vars)."""
    p = PALETTE
    return {
        "primary": p.accent, "secondary": p.info, "accent": p.rare,
        "dark": p.bg_surface, "dark_page": p.bg_page,
        "positive": p.good, "negative": p.bad, "info": p.info, "warning": p.notable,
        "good": p.good, "notable": p.notable, "bad": p.bad,
        "emph": p.text_emph, "body": p.text_body, "muted": p.text_muted, "dim": p.text_dim,
        "panel": p.bg_panel, "raise": p.bg_raise,
        "border": p.border, "border_hi": p.border_hi,
    }


def root_vars_css() -> str:
    """Emit the --q-* brand vars in :root so borders/surfaces resolve to dark on
    the FIRST paint -- kills the light-border flash before ui.colors() runs
    post-connect. Underscores are normalized to hyphens to match the CSS vars
    that ui.colors() generates (e.g. dark_page -> --q-dark-page).
    `color-scheme` darkens what the browser draws itself: a datalist's
    dropdown, scrollbars, autofill."""
    decls = ";".join(f"--q-{k.replace('_', '-')}:{v}" for k, v in quasar_colors().items())
    return (f"<style>:root{{color-scheme:dark;{decls};--mono:{MONO_STACK};--sans:{SANS_STACK}}}"
            f"html,body{{background:var(--q-dark-page);font-family:var(--sans)}}</style>")


# The corner mark's trail: each dash shorter, each gap wider, each step
# fainter. One gradient, so the mark is two pseudo-elements and no markup.
_DASHES = (14, 11, 9, 7, 5, 4, 3, 2, 2)
_GAPS = (5, 5, 6, 6, 7, 8, 9, 10, 12)
_RUN = 76          # the solid run along the top, after the corner's bend


def _trail() -> tuple[str, int]:
    stops, x = [], 0
    for k, (d, g) in enumerate(zip(_DASHES, _GAPS)):
        c = f"color-mix(in srgb, var(--kind) {round(100 - 85 * k / len(_DASHES))}%, transparent)"
        stops += [f"{c} {x}px", f"{c} {x + d}px", f"transparent {x + d}px",
                  f"transparent {x + d + g}px"]
        x += d + g
    return "linear-gradient(90deg, " + ", ".join(stops) + ")", x


_TRAIL, _TRAIL_W = _trail()

DARK_CSS = """
/* brand-token utility classes (Quasar only auto-generates them for its own slots) */
.text-emph    { color: var(--q-emph) !important; }
.text-body    { color: var(--q-body) !important; }
.text-muted   { color: var(--q-muted) !important; }
.text-dim     { color: var(--q-dim) !important; }
.text-good    { color: var(--q-good) !important; }
.text-notable { color: var(--q-notable) !important; }
.text-bad     { color: var(--q-bad) !important; }
.bg-panel     { background: var(--q-panel) !important; }
/* Quasar's dark body is #fff: text that sets no colour (a note's markdown
   body) rendered white beside the rest (measured 2026-10-01) */
body.body--dark { color: var(--q-emph) !important; font-size: 14px; }
.q-page { padding: 0 !important; }
/* Links set no underline of their own; a row of them read as a ransom note. */
a { text-decoration: none; }

/* the sidebar: places, open counts, recently closed, who is writing */
.q-drawer { background: var(--q-panel) !important; border-right: 1px solid var(--q-border); }
/* NiceGUI pads its drawer 16px too: under .sb-side-inner's own, a place's
   icon sat 40px in (measured 2026-10-01, the owner's "a lot of border space").
   The 11px top centres the 35px brand on the 56px top bar's line. */
.sb-side { padding: 0; }
/* The drawer aligns its content flex-start, so the inner box took its
   content's width: a closed row's one line made it 800px (2026-10-01). */
.sb-side-inner { display: flex; flex-direction: column; gap: 24px; padding: 11px 14px 18px;
                 min-height: 100%; width: 100%; box-sizing: border-box; }
.sb-brand  { display: flex; align-items: center; gap: 10px; padding: 0 8px; color: var(--q-emph); }
.sb-brand-name { font-weight: 600; font-size: 15px; line-height: 1.2; color: var(--q-emph);
                 overflow-wrap: anywhere; }
.sb-brand-sub  { font-family: var(--mono); font-size: 11px; color: var(--q-muted); }
.sb-nav    { display: flex; flex-direction: column; gap: 2px; }
.sb-nav-head { padding: 0 10px 6px; font-size: 11px; font-weight: 600; letter-spacing: .07em;
               text-transform: uppercase; color: var(--q-muted); }
.sb-nav-item { display: flex; align-items: center; gap: 10px; height: 34px; padding: 0 10px;
               border-radius: 7px; color: var(--q-body); }
.sb-nav-item:hover { background: var(--q-raise); color: var(--q-emph); }
.sb-nav-item.sb-on { background: var(--q-border); color: var(--q-emph); font-weight: 500; }
.sb-nav-item .q-icon { font-size: 18px; color: var(--q-muted); }
.sb-nav-item.sb-on .q-icon { color: var(--q-emph); }
.sb-nav-n  { margin-left: auto; font-family: var(--mono); font-size: 12px; color: var(--q-muted); }
.sb-nav-item.sb-zero { color: var(--q-muted); }
/* a closed row: its text cut to one line, quieter than the open counts */
.sb-nav-item.sb-closed { height: 28px; font-size: 13px; color: var(--q-muted); }
.sb-nav-item.sb-closed:hover, a.sb-nav-head:hover { color: var(--q-emph); }
.sb-closed-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
                  white-space: nowrap; }
.sb-dot    { width: 8px; height: 8px; border-radius: 50%; background: var(--kind); flex: none; }
.sb-who    { display: flex; align-items: center; gap: 10px; padding: 12px 10px 0;
             margin-top: auto; border-top: 1px solid var(--q-border); }
.sb-avatar { width: 28px; height: 28px; border-radius: 50%; background: var(--q-border);
             color: var(--q-accent); display: grid; place-items: center; flex: none;
             font-family: var(--mono); font-size: 12px; }
.sb-badge  { font-size: 12px; color: var(--q-body); overflow-wrap: anywhere; }
.sb-who .sb-keys { margin-left: auto; }
.sb-source { margin-top: -14px; padding: 0 10px; font-family: var(--mono); font-size: 11px;
              color: var(--q-muted); }
.sb-source:hover { color: var(--q-emph); }
/* who is writing, in the top bar while the sidebar is hidden: the badge
   shows before a write, and a narrow window still writes */
.sb-who-top { display: flex; align-items: center; gap: 8px; flex: none; max-width: 160px; }
.sb-who-top .sb-avatar { width: 24px; height: 24px; font-size: 11px; }
.sb-who-top .sb-badge { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* the top bar: where you are, ending in the page's title, and the search */
/* NiceGUI pads its header 16px a side: with the 56px bar in it, an 89px band
   (measured 2026-10-01, the owner's "large gap") */
.sb-header { background: var(--q-dark-page) !important; border-bottom: 1px solid var(--q-panel);
             box-shadow: none !important; min-height: 56px; padding: 0; }
/* the bar's row lines up with the cards: the main column's width, plus the
   16px NiceGUI pads the page content with */
.sb-topbar { max-width: 952px; margin: 0 auto; padding: 0 48px; box-sizing: border-box; }
.sb-crumbs { color: var(--q-muted); font-size: 13px; min-width: 0; }
.sb-crumbs > * { flex: none; white-space: nowrap; }
.sb-crumbs a { color: var(--q-body); }
.sb-crumbs a:hover { color: var(--q-primary); }
.sb-sep    { color: var(--q-dim); }
/* a long object name or search is cut short, never wraps the bar */
.sb-crumbs > .sb-here { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
                        font-size: 17px; font-weight: 600; letter-spacing: -0.01em;
                        color: var(--q-emph); }
/* main column, cards */
.sb-main   { width: 100%; max-width: 920px; margin: 0 auto; padding: 8px 32px 72px;
             box-sizing: border-box; }
.sb-subtitle { font-size: 13px; color: var(--q-muted); }
.sb-card   { background: var(--q-panel) !important; border: 1px solid var(--q-border);
             border-radius: 12px; padding: 14px 16px; }
.sb-stat-label { color: var(--q-muted); font-size: 11px; font-weight: 600;
                 text-transform: uppercase; letter-spacing: .07em; }
.sb-mono   { font-family: var(--mono); }
/* the composer's dialog, and the list `?` opens */
.sb-new-card { width: min(680px, 94vw); max-width: none !important; }
.sb-new-card .sb-composer { border: 0; padding: 0; }
/* a markdown file read in a dialog: a document, so its headings stand, at
   sizes that fit a dialog (NiceGUI's h1 is 3rem) */
.sb-md-card { width: min(860px, 94vw); max-width: none !important; }
/* the card's width, not its widest code line's: a long line scrolls in its block */
.sb-md-card .nicegui-markdown { color: var(--q-emph); font-size: 14.5px; line-height: 1.6;
                                width: 100%; min-width: 0; overflow-wrap: anywhere; }
/* links in prose: accent is 1.4:1 from the text around it, so an underline
   marks them (WCAG 2, 1.4.1; 2026-10-02). Links elsewhere take none. */
.sb-md-card .nicegui-markdown a { color: var(--q-primary); text-decoration: underline;
                                  text-underline-offset: 2px; }
.sb-md-card .nicegui-markdown h1 { font-size: 22px; line-height: 1.3; margin: 18px 0 8px; }
.sb-md-card .nicegui-markdown h2 { font-size: 18px; line-height: 1.3; margin: 16px 0 6px; }
.sb-md-card .nicegui-markdown :is(h3, h4, h5, h6) { font-size: 15px; line-height: 1.3;
                                                    margin: 12px 0 4px; }
.sb-md-card .nicegui-markdown > *:first-child { margin-top: 0; }
.sb-md-card code { font-family: var(--mono); font-size: 12.5px; padding: 1px 5px;
                   border-radius: 4px; background: var(--q-raise); }
.sb-md-card pre { background: var(--q-raise); padding: 8px 12px; border-radius: 6px;
                  overflow-x: auto; }
.sb-md-card pre code { padding: 0; background: none; }
.sb-md-card :is(th, td) { border: 1px solid var(--q-border); padding: 4px 8px; }
.sb-md-card table { border-collapse: collapse; }
.sb-keys-list { display: grid; grid-template-columns: auto 1fr; gap: 8px 14px;
                align-items: center; color: var(--q-body); font-size: 13px; }
.sb-keys-list .sb-chip { justify-self: start; color: var(--q-emph); }

/* a board: a heading and its cards, no box around them */
.sb-board  { display: flex; flex-direction: column; gap: 10px; width: 100%; }
.sb-board + .sb-board { margin-top: 18px; }
.sb-board-head { display: flex; align-items: baseline; gap: 10px; }
.sb-board-title { font-size: 18px; font-weight: 600; color: var(--q-emph); }
.sb-board-title::first-letter { text-transform: uppercase; }
.sb-count  { font-family: var(--mono); font-size: 13px; color: var(--q-muted); }
a.sb-board-aside { font-size: 12px; color: var(--q-muted); }
a.sb-board-aside:hover { color: var(--q-primary); }
.sb-board-empty .sb-board-title { color: var(--q-muted); font-weight: 500; font-size: 15px; }
/* an arc on the index: its name as the person wrote it (no capital forced) */
.sb-arc    { display: flex; flex-direction: column; gap: 10px; }
a.sb-arc-name { font-size: 16px; font-weight: 600; color: var(--q-emph); }
a.sb-arc-name:hover { color: var(--q-primary); }

/* the composer: a card holding a borderless body and one toolbar. Its frame
   is the body's boundary, so it reads 3:1 like a field's outline, and
   focused it takes a focused field's accent (2026-10-02). */
.sb-composer { background: var(--q-panel); border: 1px solid var(--q-dim);
               border-radius: 12px; padding: 4px 12px 10px; width: 100%; }
.sb-composer:focus-within { border-color: var(--q-primary); }
.sb-composer .sb-compose-body .q-field__native { font-size: 15px; line-height: 1.5;
                                                 min-height: 48px; }
.sb-composer .sb-compose-tags input { font-family: var(--mono); font-size: 12px; }

/* badges */
.sb-chip { display: inline-flex; align-items: center; font-family: var(--mono);
           font-size: 11.5px; line-height: 20px; padding: 0 8px; border-radius: 999px;
           border: 1px solid var(--q-border); color: var(--q-body); }
/* Links only: a label chip lit up on hover and a click did nothing. */
a.sb-chip:hover   { border-color: var(--q-primary); color: var(--q-primary); }
.sb-chip-open     { color: var(--q-notable); }
.sb-chip-resolved { color: var(--q-good); }
.sb-chip-good     { color: var(--q-good); }
.sb-chip-notable  { color: var(--q-notable); border-color: transparent;
                    background: color-mix(in srgb, var(--q-notable) 12%, transparent); }
.sb-chip-bad      { color: var(--q-bad); border-color: transparent;
                    background: color-mix(in srgb, var(--q-bad) 12%, transparent); }
/* where a row points: a quiet pill; a ref is the same, dashed. A long
   path or a full sha is cut short, not run past a narrow card's edge. */
.sb-target { background: var(--q-raise); color: var(--q-body); }
.sb-target, .sb-ref { display: inline-block; max-width: 100%; overflow: hidden;
                      text-overflow: ellipsis; white-space: nowrap; }
.sb-ref    { border-style: dashed; border-color: var(--q-border-hi); }
/* a kind reads as a dot and its name, in its colour: no box */
.sb-kindchip { border: 0; padding: 0 4px 0 0; gap: 6px; }
.sb-kindchip::before { content: ""; width: 7px; height: 7px; border-radius: 50%;
                       background: var(--kind); }
.sb-tag    { font-family: var(--mono); font-size: 12px; color: var(--q-muted); }
a.sb-tag:hover { color: var(--q-primary); }

/* notes layer -- each row a card. Colours via brand vars; hex-audit safe. */
.sb-note { position: relative; display: flex; align-items: flex-start; gap: 14px;
           background: var(--q-panel); border: 1px solid var(--q-border);
           border-radius: 12px; padding: 14px 16px 12px; box-sizing: border-box;
           transition: border-color .15s; }
.sb-note:hover, .sb-note:focus-within { border-color: var(--q-border-hi); }
.sb-note-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
/* the body's first line clears the toolbar over the card's top-right
   corner, and the lines under it run the card's width: a padding cleared
   it on every line, and on a phone the body lost a fifth of its width */
.sb-has-actions .sb-note-main > :first-child::before { content: ""; float: right;
                                                       width: 64px; height: 22px; }
.sb-resolvable .sb-note-main > :first-child::before { width: 100px; }
.sb-note-text { color: var(--q-emph); font-size: 14.5px; line-height: 1.55;
                text-wrap: pretty; }
/* a path or a URL with no space in it breaks rather than widening the page */
.sb-note-text, .sb-verdict, .sb-note .nicegui-markdown { overflow-wrap: anywhere; }
.sb-note-foot { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px;
                font-size: 12px; }
/* who, when and which row: one group at the right that wraps as a unit;
   split, its author stayed on the first line and its age and id began
   the second */
.sb-note-by { display: flex; align-items: center; gap: 10px; margin-left: auto;
              white-space: nowrap; }
.sb-note-meta { color: var(--q-muted); font-family: var(--mono); font-size: 12px; }
/* a check's `checked → result` is its content: as bright as a body, mono at
   the size of a body's inline code (dim metadata before, 2026-10-01) */
.sb-verdict { color: var(--q-emph); font-family: var(--mono); font-size: 12px; }
a.sb-id { color: var(--q-muted); }
a.sb-id:hover { color: var(--q-primary); }
/* copy the id: shown with the card's other buttons */
.q-btn.sb-copy { color: var(--q-muted); margin-left: -6px; opacity: 0; transition: opacity .15s; }
.sb-note:hover .sb-copy, .sb-note:focus-within .sb-copy { opacity: 1; }
.q-btn.sb-copy:hover { color: var(--q-emph); }
/* a clipped body: the link to the whole note */
a.sb-note-open { display: block; }
a.sb-note-open:hover .sb-note-text { color: var(--q-primary); }
/* a card the search box's text filters out, and a board left with none */
.sb-filtered { display: none !important; }

/* resolve: an empty box, a tick on hover; the 7px radius every .q-btn takes
   below. A card's is at its right and the toolbar sits beside it; an arc's
   checklist keeps its boxes on the left, since every row there has one. */
.q-btn.sb-resolve { width: 22px; height: 22px; min-width: 22px; min-height: 22px; padding: 0;
                    margin-top: 1px; border: 1.5px solid var(--q-dim); flex: none; }
.q-btn.sb-resolve .q-icon { font-size: 14px; }
.q-btn.sb-resolve:hover, .q-btn.sb-resolve:focus-visible { border-color: var(--q-good); }
/* a done box on an arc's checklist: ticked, and its row steps back */
.q-btn.sb-resolve.sb-resolved { border-color: var(--q-good);
                                background: color-mix(in srgb, var(--q-good) 14%, transparent); }
.sb-done .sb-note-text { color: var(--q-muted); }
/* star and edit: a toolbar over the top-right corner, shown on hover or
   focus; always shown where there is no hover (a phone) */
.sb-actions { position: absolute; top: 8px; right: 8px; display: flex; gap: 2px;
              padding: 2px; background: var(--q-raise); border: 1px solid var(--q-border);
              border-radius: 8px; opacity: 0; transition: opacity .15s; }
.sb-note:hover .sb-actions, .sb-note:focus-within .sb-actions { opacity: 1; }
.sb-actions .q-btn { color: var(--q-body); }
.sb-actions .q-btn:hover { color: var(--q-emph); }
/* pinned, not a column: a column narrowed an open card's text, and its
   foot ended short of a closed card's */
.sb-resolvable .q-btn.sb-resolve { position: absolute; top: 13px; right: 14px; margin: 0; }
.sb-resolvable .sb-actions { right: 44px; }
.sb-starred .sb-actions { opacity: 1; }
@media (hover: none) { .sb-actions, .q-btn.sb-copy { opacity: 1; } }

/* the corner mark: the bend (::before) and the run with its trail (::after) */
.sb-note[class*="sb-kind-"]::before, .sb-note[class*="sb-kind-"]::after {
    content: ""; position: absolute; pointer-events: none; opacity: .7;
    transition: opacity .15s; }
.sb-note[class*="sb-kind-"]::before {
    top: -1px; left: -1px; width: 28px; height: 28px; box-sizing: border-box;
    border-top: 2px solid var(--kind); border-left: 2px solid var(--kind);
    border-top-left-radius: 12px;
    -webkit-mask-image: linear-gradient(to bottom, black 35%, transparent);
    mask-image: linear-gradient(to bottom, black 35%, transparent); }
.sb-note[class*="sb-kind-"]::after {
    top: -1px; left: 27px; height: 2px; width: calc(@RUN@px + 6px + @TRAILW@px);
    max-width: calc(100% - 40px);
    background: linear-gradient(var(--kind), var(--kind)) 0 0 / @RUN@px 2px no-repeat,
                @TRAIL@ calc(@RUN@px + 6px) 0 / @TRAILW@px 2px no-repeat; }
.sb-note:hover::before, .sb-note:hover::after,
.sb-note:focus-within::before, .sb-note:focus-within::after { opacity: 1; }

/* Quasar portals menus (ui.select options) and tooltips to <body>, outside the
   styled tree: nothing under .sb-* reaches them, and the options rendered
   low-contrast grey (measured 2026-09-08, the edit dialog's arc select). One
   rule per portal root, brand vars only so the hex audit stays clean. */
.q-menu { background: var(--q-panel) !important; color: var(--q-body) !important;
          border: 1px solid var(--q-border); font-family: var(--sans); }
.q-menu .q-item { color: var(--q-body); }
.q-menu .q-item--active, .q-menu .q-item.q-manual-focusable--focused,
.q-menu .q-item:hover { color: var(--q-primary); }
/* Borders, not shadows: Quasar gives a dark menu and a dialog's card a white
   glow, the only shadows on the page (2026-10-02). A dialog's card takes a
   card's border and corners. */
.q-menu--dark { box-shadow: none; }
.q-card--dark { box-shadow: none; border: 1px solid var(--q-border); border-radius: 12px; }
/* Quasar's own #fff (measured 2026-10-01). On a filled button and a notice,
   Mocha's pastels take the page colour as text; a notice with no type is
   Quasar's grey, so it takes the panel; a field's own text is the page's.
   `.text-white` is !important in NiceGUI's last layer, which beats every
   unlayered !important: only an earlier layer's !important wins. */
@layer overrides {
  /* the resolve ring's tick shows only on hover; a button's text-<colour>
     class is !important in NiceGUI's last layer too */
  .q-btn.sb-resolve { color: transparent !important; }
  .q-btn.sb-resolve:hover, .q-btn.sb-resolve:focus-visible { color: var(--q-good) !important; }
  .q-btn.sb-resolve.sb-resolved { color: var(--q-good) !important; }
  /* an unlayered muted rule read text-primary's blue (2026-10-01) */
  .q-btn.sb-keys { color: var(--q-muted) !important; }
  .q-btn.sb-keys:hover { color: var(--q-emph) !important; }
  /* Quasar's `no-outline` is `outline: 0 !important` on every button, so
     keyboard focus showed only Quasar's faint tint (2026-10-02). accent is
     5.9:1 or more on every ground, a selected sidebar row's included. */
  :is(a, .q-btn):focus-visible { outline: 2px solid var(--q-primary) !important;
                                 outline-offset: 2px; }
  .q-btn.text-white, .q-notification[class*=bg-] { color: var(--q-dark-page) !important; }
  .q-notification:not([class*=bg-]) { background: var(--q-panel) !important;
                                      color: var(--q-emph) !important;
                                      border: 1px solid var(--q-border); }
}
.q-field--dark .q-field__native, .q-field--dark .q-field__input { color: var(--q-emph) !important; }
/* Quasar's rgba(255, 255, 255, .7), the one colour off the palette and out of
   the hex audit's sight (2026-10-02). A highlighted label keeps the field's
   colour, as in Quasar. */
.q-field--dark:not(.q-field--highlighted) .q-field__label,
.q-field--dark .q-field__marginal, .q-field--dark .q-field__bottom { color: var(--q-body); }
.q-field--outlined .q-field__control { border-radius: 8px; }
/* an outline is a control's boundary: 3:1 on the card and the page (border
   read 1.4:1, 2026-10-02) */
.q-field--dark.q-field--outlined .q-field__control:before { border-color: var(--q-dim); }
/* a filled button is sentence case and medium, not Quasar's shouted caps */
.q-btn { text-transform: none; font-weight: 500; letter-spacing: 0; border-radius: 7px; }
/* not !important: Quasar's is a plain layered rule, and `.q-menu .q-item` must still win */
.q-item--dark { color: var(--q-emph); }
.q-tooltip { background: var(--q-panel) !important; color: var(--q-emph) !important;
             border: 1px solid var(--q-border); font-family: var(--mono); font-size: 11px;
             white-space: pre-line; }

/* the header's search box, and the words a search found in a result */
/* the title takes the bar first and the search what is left: shrinking
   alike, "Arcs" got 30px at 901 with the sidebar open (2026-10-01) */
.sb-search { flex: 10 1 0; min-width: 120px; max-width: 340px; }
.sb-search .q-field__control { background: var(--q-panel); }
.sb-snippet mark { background: transparent; color: var(--q-notable);
                   box-shadow: inset 0 -1px var(--q-notable); }

/* NiceGUI's "connection lost" popup (shown while `serve` restarts): black,
   sans-serif and an emoji; the panel, the mono stack and a notable line here */
.nicegui-error-popup { background: var(--q-panel) !important; color: var(--q-body);
                       border-color: var(--q-border) !important; font-family: var(--mono);
                       font-size: 12px; padding: 1em 1.5em !important; }
.nicegui-error-popup > span:first-child { color: var(--q-notable); }
.nicegui-error-popup > span:first-child::before { content: none !important; }

/* markdown bodies sit inside a note row; keep them from inheriting page margins */
.sb-note .nicegui-markdown { color: var(--q-emph); font-size: 14.5px; line-height: 1.55; }
.sb-note .nicegui-markdown > *:first-child { margin-top: 0; }
.sb-note .nicegui-markdown > *:last-child  { margin-bottom: 0; }
.sb-note code { font-family: var(--mono); font-size: 12.5px; padding: 1px 5px;
                border-radius: 4px; background: var(--q-raise); }
.sb-note pre code { padding: 0; background: none; }
/* body links (a cited note id is one) took the browser's default blue */
.sb-note .nicegui-markdown a { color: var(--q-primary); text-decoration: underline;
                               text-underline-offset: 2px; }
/* A note is a paragraph, not a document. A real <h1> in a list of eighty rows
   dominates the board, and markdown eats a line-leading `#tag` into one -- the
   harvest strips those now, but a deliberate heading still has to sit down. */
.sb-note .nicegui-markdown h1,
.sb-note .nicegui-markdown h2,
.sb-note .nicegui-markdown h3,
.sb-note .nicegui-markdown h4,
.sb-note .nicegui-markdown h5,
.sb-note .nicegui-markdown h6 { font-size: 14px; font-weight: 600; margin: 4px 0 2px;
                                color: var(--q-emph); line-height: 1.3; }

@media (min-width: @WIDE@px) { .sb-narrow { display: none; } }
/* with the sidebar hidden the bar also names the writer, and the menu button
   is the way back: the buttons give up their words and the crumbs their
   parents, which left the title 49px at 601 (2026-10-01) */
@media (max-width: @NARROW@px) {
  .sb-btn-word, .sb-crumbs > :not(.sb-here) { display: none; }
  .sb-topbar { column-gap: 8px; }
}
.sb-commit .q-btn__content, .sb-push .q-btn__content, .sb-new .q-btn__content {
  flex-wrap: nowrap; white-space: nowrap; gap: 6px; }
@media (max-width: 600px) {
  /* a phone's top bar: the title alone says where you are, and there is
     no key to press `/` with */
  .sb-search .sb-chip, .sb-who-top .sb-avatar { display: none; }
  .sb-main { padding: 4px 16px 56px; }
  /* two lines: where you are and who writes, then the buttons and the
     search. On one, the title got 30px at 390 and the search 0 (2026-10-01).
     The ::after is a full-width item: it ends the first line. */
  .sb-topbar { flex-wrap: wrap !important; row-gap: 0; padding: 8px 16px; }
  .sb-topbar > .q-space { display: none; }
  .sb-topbar > .sb-crumbs { order: 1; flex: 1 1 0; }
  .sb-topbar > .sb-who-top { order: 2; }
  .sb-topbar::after { content: ""; order: 3; flex-basis: 100%; margin-top: 8px; }
  .sb-topbar > .sb-new { order: 4; }
  .sb-topbar > .sb-commit, .sb-topbar > .sb-push { order: 5; }
  .sb-topbar > .sb-search { order: 6; flex: 1; min-width: 0; max-width: none; }
}
""".replace("@WIDE@", str(NARROW + 1)).replace("@NARROW@", str(NARROW)).replace("@TRAILW@", str(_TRAIL_W)) \
   .replace("@TRAIL@", _TRAIL).replace("@RUN@", str(_RUN))
# A kind's `list` colour, for its chip and its row's corner; a kind the store
# declares shares one. A variable, so a row's class does not tint its text.
DARK_CSS += "".join(f".sb-kind-{k} {{ --kind: {c}; }}\n"
                    for k, c in {**T.KIND, "own": T.OWN_KIND}.items())
DARK_CSS += '.sb-chip[class*="sb-kind-"] { color: var(--kind); }\n'
