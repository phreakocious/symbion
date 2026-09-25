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
    """The other direction: an empty theme.py would pass the test above."""
    found = HEX.findall((GUI_DIR / "theme.py").read_text(encoding="utf-8"))
    assert len(found) >= 10, f"theme.py defines only {len(found)} colors"


def test_the_audit_fires_on_a_planted_hex():
    """...and the regex itself. A typo here makes both tests above vacuous:
    the first passes by finding nothing, the second is the only thing left
    holding the rule up."""
    assert HEX.findall("color: #0a0a0a;") == ["#0a0a0a"]
    assert HEX.findall("ui.label('x').classes('sb-chip')") == []


def test_every_kind_chip_class_is_defined_in_theme():
    """A kind chip whose CSS class has no rule renders unstyled, and the hex
    audit above cannot see it — it only asks where colors live, not whether
    anything uses them. Measured 2026-09-08: the `audit`->`check` sweep skipped
    theme.py, so the old per-label `_KIND_CHIP` pointed at `sb-chip-check`
    while the stylesheet still defined `.sb-chip-audit`. Chips are now by
    bits (`chip_class`), so the audit covers every bit combination rather
    than every declared label."""
    pytest.importorskip("nicegui")   # chip_class lives in gui.notes, which imports it
    import itertools
    from symbion import kinds as K
    from symbion.gui.notes import chip_class
    css = (GUI_DIR / "theme.py").read_text(encoding="utf-8")
    classes = {chip_class(K.Kind(status=s, parked=p, verdict=v))
               for s, p, v in itertools.product((False, True), repeat=3)}
    missing = [c for c in classes if c and f".{c} " not in css
               and f".{c}\t" not in css and f".{c}{{" not in css]
    assert not missing, f"no CSS rule for: {missing}"


def test_the_chip_audit_fires_on_a_planted_class():
    """The other direction: an empty _KIND_CHIP would pass the test above."""
    css = (GUI_DIR / "theme.py").read_text(encoding="utf-8")
    assert ".sb-chip-sausage " not in css and ".sb-chip-sausage{" not in css


def test_portaled_quasar_menus_and_tooltips_are_themed():
    """Quasar portals QMenu (ui.select options) and QTooltip to <body>, outside
    the styled tree, so nothing under .sb-* reaches them and the options
    rendered low-contrast grey (measured 2026-09-08, the edit dialog's arc
    select). One rule per portal root. Source audit like the rest of this
    file: CSS has no behavioural test without a browser."""
    css = (GUI_DIR / "theme.py").read_text(encoding="utf-8")
    for sel in (".q-menu", ".q-tooltip"):
        assert re.search(rf"{re.escape(sel)}\s*\{{", css), f"no rule for {sel}"
