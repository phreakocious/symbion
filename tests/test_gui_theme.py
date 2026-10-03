"""Every 6-digit hex in gui/ lives in theme.py. Source-text audit, so it runs
without nicegui."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

GUI_DIR = Path(__file__).resolve().parent.parent / "src" / "symbion" / "gui"
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def test_no_hex_outside_theme():
    offenders = {p.name: HEX.findall(p.read_text(encoding="utf-8"))
                 for p in sorted(GUI_DIR.rglob("*.py")) if p.name != "theme.py"}
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"hexes belong in theme.py: {offenders}"


def test_theme_actually_defines_colors():
    """The other direction: an empty theme.py would pass the test above.
    Most of its colours come from term.py, so count what the theme resolves to."""
    from symbion.gui.theme import quasar_colors
    found = {v for v in quasar_colors().values() if HEX.fullmatch(v)}
    assert len(found) >= 10, f"theme.py defines only {len(found)} colors"


def test_the_gui_prints_in_the_terminal_palette():
    """`list` printed Catppuccin Mocha and the GUI Solarized, so the two faces
    of one tool disagreed on every colour (2026-09-29). Each text role and
    each kind is the terminal's own value."""
    from symbion import term
    from symbion.gui.theme import DARK_CSS, quasar_colors
    q = quasar_colors()
    assert [q[r] for r in ("emph", "body", "muted", "dim", "good", "notable", "bad")] == \
        [term.TEXT, term.BODY, term.META, term.FADED, term.GOOD, term.WARN, term.BAD]
    chips = dict(re.findall(r"\.sb-kind-([a-z]+)\s*\{\s*--kind:\s*(#[0-9a-f]{6})", DARK_CSS))
    assert chips == {**term.KIND, "own": term.OWN_KIND}


def test_text_that_sets_no_colour_takes_the_text_colour():
    """Quasar's dark body is #fff, and a note's markdown body set no colour of
    its own: a short body rendered white beside the clipped long ones
    (measured 2026-10-01). The page's own colour is the palette's."""
    from symbion.gui.theme import DARK_CSS
    assert re.search(r"body\.body--dark\s*\{\s*color:\s*var\(--q-emph\)", DARK_CSS)


def test_a_verdict_reads_as_bright_as_a_body():
    """A check with no body showed only its `checked → result` line, as dim
    11px metadata, beside rows whose body was Text (owner, 2026-10-01: "the
    brighter is more correct")."""
    from symbion.gui.theme import DARK_CSS
    assert re.search(r"\.sb-verdict\s*\{[^}]*color:\s*var\(--q-emph\)", DARK_CSS)


def test_quasars_own_whites_are_themed():
    """Quasar draws #fff in a field's own text and a list item, and on a
    filled button and a notice, where Mocha's pastels need dark text; a
    notice with no type is its own grey (measured in the browser,
    2026-10-01). Source audit, as for
    the portals above. `.text-white` is `!important` in NiceGUI's last layer,
    `quasar_importants`, so it beats any unlayered `!important`: the rule
    against it sits in `overrides`, the layer before (an unlayered one lost)."""
    from symbion.gui.theme import DARK_CSS
    assert re.search(r"\.q-field--dark \.q-field__native[\s,{]", DARK_CSS)
    assert re.search(r"\.q-item--dark\s*\{\s*color:\s*var\(--q-emph\);", DARK_CSS)  # /arcs' archived
    # A notice with no type carries `.text-white` too: dark text on the dark
    # panel, until its own rule moved into the layer as well.
    layer = re.search(r"@layer overrides\s*\{(.*?)\}\s*\}", DARK_CSS, re.S)
    assert layer and all(s in layer[1] for s in (
        ".q-btn.text-white", ".q-notification[class*=bg-]", ".q-notification:not([class*=bg-])"))


def test_the_audit_fires_on_a_planted_hex():
    """...and the regex itself. A typo here makes both tests above vacuous:
    the first passes by finding nothing, the second is the only thing left
    holding the rule up."""
    assert HEX.findall("color: #0a0a0a;") == ["#0a0a0a"]
    assert HEX.findall("ui.label('x').classes('sb-chip')") == []


def test_every_kind_class_is_defined_in_theme():
    """A kind chip whose CSS class has no rule renders unstyled, and the hex
    audit above cannot see it — it only asks where colors live, not whether
    anything uses them. Measured 2026-09-08: the `audit`->`check` sweep skipped
    theme.py, so the old per-label `_KIND_CHIP` pointed at `sb-chip-check`
    while the stylesheet still defined `.sb-chip-audit`. Chips are by label
    again, as the terminal colours a row (2026-10-01): each default kind,
    and one that is not, which a store declares."""
    pytest.importorskip("nicegui")   # kind_class lives in gui.notes, which imports it
    from symbion import kinds as K
    from symbion.gui.notes import kind_class
    from symbion.gui.theme import DARK_CSS
    classes = {kind_class(k) for k in [*K.DEFAULT_KINDS, "sausage"]}
    missing = [c for c in classes if not re.search(rf"\.{c}\s*\{{", DARK_CSS)]
    assert not missing, f"no CSS rule for: {missing}"


def test_the_chip_audit_fires_on_a_planted_class():
    """The other direction: an empty _KIND_CHIP would pass the test above."""
    from symbion.gui.theme import DARK_CSS
    assert not re.search(r"\.sb-kind-sausage\s*\{", DARK_CSS)


def test_portaled_quasar_menus_and_tooltips_are_themed():
    """Quasar portals QMenu (ui.select options) and QTooltip to <body>, outside
    the styled tree, so nothing under .sb-* reaches them and the options
    rendered low-contrast grey (measured 2026-09-08, the edit dialog's arc
    select). One rule per portal root. Source audit like the rest of this
    file: CSS has no behavioural test without a browser."""
    css = (GUI_DIR / "theme.py").read_text(encoding="utf-8")
    for sel in (".q-menu", ".q-tooltip"):
        assert re.search(rf"{re.escape(sel)}\s*\{{", css), f"no rule for {sel}"


def test_only_link_chips_light_up_on_hover():
    """A label chip (the check badge) lit up like a link and a click did
    nothing. The hover rule names `a.sb-chip` and nothing wider."""
    import re
    from symbion.gui.theme import DARK_CSS
    css = re.sub(r"/\*.*?\*/", "", DARK_CSS, flags=re.S)
    hover = [sel.strip() for block in re.findall(r"([^{}]+)\{", css)
             for sel in block.split(",") if ".sb-chip:hover" in sel]
    assert hover == ["a.sb-chip:hover"], hover


def test_a_row_marks_its_kind_on_one_corner_not_its_whole_left_edge():
    """A full-height left border in the kind's colour read as every other
    dashboard's (owner, 2026-10-01). The mark is a bend at the top-left
    corner and a run along the top that trails off in dashes."""
    from symbion.gui.theme import DARK_CSS
    css = re.sub(r"/\*.*?\*/", "", DARK_CSS, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    edges = [sel.strip() for sel, body in rules if re.search(r"border-left[^;]*var\(--kind\)", body)]
    assert edges == ['.sb-note[class*="sb-kind-"]::before'], edges
    trail = [body for sel, body in rules if sel.strip() == '.sb-note[class*="sb-kind-"]::after']
    # nine dashes, each a colour stop at either end, fading as they go
    assert trail and trail[0].count("color-mix(in srgb, var(--kind)") == 18


def test_the_browsers_own_widgets_are_dark():
    """The browser draws a datalist's dropdown (the composer's names), the
    scrollbars and autofill itself, outside any CSS but `color-scheme`; with
    none declared they came out light on the dark page (the owner,
    2026-10-01). Declared in the first-paint style, so no light frame."""
    from symbion.gui.theme import root_vars_css
    assert re.search(r":root\{[^}]*color-scheme:dark", root_vars_css())


def _ratio(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _bodies(selector: str) -> list[str]:
    """The bodies of every DARK_CSS rule whose selector list holds `selector`."""
    from symbion.gui.theme import DARK_CSS
    css = re.sub(r"/\*.*?\*/", "", DARK_CSS, flags=re.S)
    return [body for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
            if selector in (" ".join(s.split()) for s in sels.split(","))]


def test_the_contrast_ratio_is_wcags():
    """...and the failing pair the ring test below guards against still fails,
    so that test is not vacuous."""
    from symbion.gui.theme import PALETTE
    assert round(_ratio("#ffffff", "#000000"), 2) == 21
    assert round(_ratio(PALETTE.text_muted, PALETTE.bg_panel), 2) == 4.75
    assert _ratio(PALETTE.border_hi, PALETTE.bg_panel) < 3


def test_the_empty_resolve_ring_is_a_control_a_reader_can_see():
    """A control's border needs 3:1 on its ground (WCAG 2, 1.4.11): the ring
    was `border_hi`, 1.92:1 on the card (measured 2026-10-02)."""
    from symbion.gui.theme import PALETTE, quasar_colors
    (slot,) = [m[1] for b in _bodies(".q-btn.sb-resolve")
               if (m := re.search(r"border:[^;]*var\(--q-([a-z-]+)\)", b))]
    ring = quasar_colors()[slot.replace("-", "_")]
    assert _ratio(ring, PALETTE.bg_panel) >= 3, (slot, ring)


def test_a_focused_link_or_button_shows_a_ring():
    """Quasar's `no-outline` is `outline: 0 !important` on every button, in
    NiceGUI's last layer: keyboard focus showed Quasar's faint tint, and a card
    moved its border to `border_hi`, 1.92:1 (2026-10-02). The ring sits in
    `overrides`, the one layer whose `!important` beats it, and reads 3:1 or
    more on every ground it can land on."""
    from symbion.gui.theme import DARK_CSS, PALETTE, quasar_colors
    layer = re.search(r"@layer overrides\s*\{(.*?)\}\s*\}", DARK_CSS, re.S)
    ring = layer and re.search(r":is\(([^)]*)\):focus-visible\s*\{[^}]*"
                               r"outline:\s*2px solid var\(--q-([a-z-]+)\) !important", layer[1])
    assert ring, "no focus ring in @layer overrides"
    assert {"a", ".q-btn"} <= {s.strip() for s in ring[1].split(",")}
    colour = quasar_colors()[ring[2].replace("-", "_")]
    for ground in (PALETTE.bg_page, PALETTE.bg_panel, PALETTE.bg_raise, PALETTE.border):
        assert _ratio(colour, ground) >= 3, (ring[2], ground)


def test_a_fields_label_icons_and_hint_are_the_palettes():
    """Quasar paints a dark field's label, icons and hint `rgba(255, 255, 255,
    .7)`, the one colour off the palette, and the hex audit cannot see it
    (2026-10-02). The label keeps Quasar's `:not(.q-field--highlighted)`, so
    a focused field's label still takes the field's colour."""
    for sel in (".q-field--dark:not(.q-field--highlighted) .q-field__label",
                ".q-field--dark .q-field__marginal", ".q-field--dark .q-field__bottom"):
        assert any(re.search(r"color:\s*var\(--q-[a-z-]+\)", b) for b in _bodies(sel)), sel


def test_no_surface_casts_quasars_white_shadow():
    """Borders, not shadows: a dialog's card and a menu kept Quasar's
    white-alpha shadow, the only shadows on the page (2026-10-02)."""
    for sel in (".q-card--dark", ".q-menu--dark"):
        assert any(re.search(r"box-shadow:\s*none", b) for b in _bodies(sel)), sel


def test_a_fields_outline_is_a_control_a_reader_can_see():
    """A control's boundary needs 3:1 (WCAG 2, 1.4.11): an outlined field and
    the composer's frame, the boundary of its borderless name field on /arcs,
    were `border`, 1.4:1 on the card (2026-10-02). A field sits on the card
    and in the top bar on the page, so both."""
    from symbion.gui.theme import PALETTE, quasar_colors
    for sel, prop in ((".q-field--dark.q-field--outlined .q-field__control:before", "border-color"),
                      (".sb-composer", "border")):
        (slot,) = [m[1] for b in _bodies(sel)
                   if (m := re.search(rf"{prop}:[^;]*var\(--q-([a-z-]+)\)", b))]
        colour = quasar_colors()[slot.replace("-", "_")]
        for ground in (PALETTE.bg_page, PALETTE.bg_panel):
            assert _ratio(colour, ground) >= 3, (sel, slot, ground)


def test_a_link_inside_prose_is_underlined():
    """In a note's text a link is accent beside text_emph, 1.4:1 apart, so its
    colour alone does not mark it (WCAG 2, 1.4.1; 2026-10-02). `a` takes no
    underline elsewhere, since a row of chips read as a ransom note."""
    from symbion.gui.theme import PALETTE
    assert _ratio(PALETTE.accent, PALETTE.text_emph) < 3
    for sel in (".sb-note .nicegui-markdown a", ".sb-md-card .nicegui-markdown a"):
        assert any(re.search(r"text-decoration:\s*underline", b) for b in _bodies(sel)), sel
