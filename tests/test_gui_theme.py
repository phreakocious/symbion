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


def test_a_picked_kind_colour_is_a_rule_on_that_kinds_class():
    """A store's own kinds all shared one lavender, so several of them read
    alike (2026-10-06). The page adds this after the theme, so a default
    kind's own colour is replaced too."""
    from symbion import kinds as K
    from symbion.gui.theme import kind_css
    assert kind_css({"anomaly": K.Kind(status=True, color="flamingo"), "note": K.Kind(),
                     "bug": K.Kind(status=True, color="red")}) == \
        ".sb-kind-anomaly { --kind: #f2cdcd; }\n.sb-kind-bug { --kind: #f38ba8; }\n"


def test_every_palette_colour_reads_on_every_ground():
    """A kind's `color` is a palette name, never a hex, so no store can pick
    an unreadable one: each passes WCAG AA text contrast (4.5:1)."""
    from symbion import kinds as K
    from symbion.gui.theme import Palette
    p = Palette()
    low = [(n, g) for n, h in K.COLORS.items() for g in (p.bg_panel, p.bg_raise)
           if _ratio(h, g) < 4.5]
    assert not low, low


def test_text_that_sets_no_colour_takes_the_text_colour():
    """Quasar's dark body is #fff, and a note's markdown body set no colour of
    its own: a short body rendered white beside the clipped long ones
    (measured 2026-10-01). The page's own colour is the palette's."""
    from symbion.gui.theme import DARK_CSS
    assert re.search(r"body\.body--dark\s*\{\s*color:\s*var\(--q-emph\)", DARK_CSS)


def test_a_verdict_reads_as_bright_as_a_body():
    """A check with no body showed only its `checked → result` line, as dim
    11px metadata, beside rows whose body was Text (user, 2026-10-01: "the
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
    # an arc's progress track: white at .6 under opacity .4 (2026-10-16)
    assert re.search(r"\.q-linear-progress__track--dark\s*\{\s*background:\s*var\(--q-", DARK_CSS)
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
    classes = {kind_class(k).split()[0] for k in [*K.DEFAULT_KINDS, "sausage"]}
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


def test_a_tooltip_is_capped_over_quasars_inline_width():
    """Quasar's position engine writes max-width inline, as wide as the space
    left, so a closed row's tooltip ran 1216px across a 1280px page
    (2026-10-06). Only an !important cap beats an inline style."""
    from symbion.gui.theme import DARK_CSS
    rule = re.search(r"\.q-tooltip\s*\{([^}]*)\}", DARK_CSS).group(1)
    assert re.search(r"max-width:[^;]*\d+ch[^;]*!important", rule), rule


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
    dashboard's (user, 2026-10-01). The mark is a bend at the top-left
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
    none declared they came out light on the dark page (the user,
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


def _rules() -> list[tuple[list[str], str]]:
    """Each DARK_CSS rule as (its selectors, its body), comments dropped."""
    from symbion.gui.theme import DARK_CSS
    css = re.sub(r"/\*.*?\*/", "", DARK_CSS, flags=re.S)
    return [([" ".join(s.split()) for s in sels.split(",")], body)
            for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _bodies(selector: str) -> list[str]:
    """The bodies of every DARK_CSS rule whose selector list holds `selector`."""
    return [body for sels, body in _rules() if selector in sels]


def _ground(body: str, q: dict) -> str:
    """The ground a rule's words sit on: its own wash over a card, its own
    background, else a card's."""
    from symbion.gui.theme import PALETTE
    if w := re.search(r"background(?:-color)?:\s*color-mix\(in srgb, var\(--q-([a-z-]+)\) (\d+)%",
                      body):
        wash, t = q[w[1]], int(w[2]) / 100
        return "#" + "".join(f"{round(int(wash[i:i + 2], 16) * t + int(PALETTE.bg_panel[i:i + 2], 16) * (1 - t)):02x}"
                             for i in (1, 3, 5))
    if s := re.search(r"background(?:-color)?:\s*var\(--q-([a-z-]+)\)", body):
        return q[s[1]]
    return PALETTE.bg_panel


# Not words on a card: a mark, whose floor is 3:1 (WCAG 2, 1.4.11), or dark
# text on a filled control, whose ground is the control's own colour.
_NOT_WORDS = (".sb-sep", ".text-dim", ".sb-docs > .q-icon", ".q-btn.text-white")


def test_every_rule_that_sets_a_colour_reads_at_4_5_to_1():
    """An amendment's who-and-when was `dim` on the card, 3.6:1, and a
    deleted block's words `muted` on a red wash, 4.3:1 (measured 2026-10-16):
    text needs 4.5:1 (WCAG 2, 1.4.3). Three selectors named by hand missed
    every rule added after them, so each rule that sets a palette colour is
    read on its own ground, else a card's."""
    from symbion.gui.theme import quasar_colors
    q = {k.replace("_", "-"): v for k, v in quasar_colors().items()}
    read, low = 0, []
    for sels, body in _rules():
        m = re.search(r"(?<![-\w])color:\s*var\(--q-([a-z-]+)\)", body)
        if not m or m[1] not in q or any(s in sels for s in _NOT_WORDS):
            continue
        read += 1
        if (r := _ratio(q[m[1]], ground := _ground(body, q))) < 4.5:
            low.append((sels, m[1], ground, round(r, 2)))
    assert read > 100 and not low, (read, low)


def test_a_wash_over_a_whole_card_steps_its_small_words_up():
    """The peek's pick washed its card in the accent, and the card's small
    words, `muted`, read 4.21:1 on the wash (2026-10-16). The words are other
    rules', so the rule that washes a card re-maps `--q-muted` to a step up."""
    from symbion.gui.theme import quasar_colors
    q = {k.replace("_", "-"): v for k, v in quasar_colors().items()}
    card = re.compile(r"(\.[\w-]+ )*\.sb-note(\.[\w-]+|:[\w-]+)*")
    washes = [(sels, body) for sels, body in _rules()
              if any(card.fullmatch(s) for s in sels) and "color-mix" in _background(body)]
    assert washes, "the pick's wash is one"
    for sels, body in washes:
        if _ratio(q["muted"], _ground(body, q)) < 4.5:
            assert re.search(r"--q-muted:\s*var\(--q-(body|emph)\)", body), sels


def _background(body: str) -> str:
    m = re.search(r"background(?:-color)?:\s*([^;]+)", body)
    return m[1] if m else ""


def test_the_contrast_ratio_is_wcags():
    """...and the failing pair the ring test below guards against still fails,
    so that test is not vacuous."""
    from symbion.gui.theme import PALETTE
    assert round(_ratio("#ffffff", "#000000"), 2) == 21
    assert round(_ratio(PALETTE.text_muted, PALETTE.bg_panel), 2) == 4.75
    assert _ratio(PALETTE.border_hi, PALETTE.bg_panel) < 3


def test_code_takes_the_palette_and_reads_on_its_ground():
    """Fenced code took the github-dark NiceGUI serves (the user, 2026-10-16:
    "palette code"). Each token's colour is a Mocha hue or a text step and
    reads 4.5:1 on a block's ground; no github-dark token rule is left
    standing, and a diff's lines take the palette's washes."""
    from pygments.formatters import HtmlFormatter

    from symbion import kinds, term
    from symbion.gui.theme import DARK_CSS, PALETTE
    rules = re.findall(r"body\.body--dark \.codehilite \.(\w+) \{([^}]*)\}", DARK_CSS)
    theirs = re.findall(r"\.body--dark \.codehilite \.(\w+) \{", HtmlFormatter(
        style="github-dark").get_style_defs(".body--dark .codehilite"))
    assert set(theirs) - {cls for cls, _ in rules} == {"hll"}   # a line pick: never drawn
    def six(h):                       # pygments writes #ff0000 as #F00
        return "#" + (h if len(h) == 6 else "".join(c * 2 for c in h)).lower()
    inks = {six(h) for _, body in rules
            for h in re.findall(r"(?<![-\w])color:\s*#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b", body)}
    assert inks and inks <= set(kinds.COLORS.values()) | {term.TEXT, term.BODY}, inks
    assert min(_ratio(c, PALETTE.bg_raise) for c in inks) >= 4.5, \
        sorted((round(_ratio(c, PALETTE.bg_raise), 2), c) for c in inks)[:3]
    assert {cls for cls, body in rules
            if re.search(r"(?<![-\w])background:\s*color-mix", body)} == {"gd", "gi"}


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


def test_every_page_keeps_room_for_the_scrollbar():
    """The column and the top bar moved 7px, half a scrollbar, between a page
    that scrolls and one that does not, wherever a scrollbar takes room
    (measured in headless Chrome at 1440px, 2026-10-16)."""
    from symbion.gui.theme import DARK_CSS
    assert re.search(r"html\s*\{\s*scrollbar-gutter:\s*stable;", DARK_CSS)
