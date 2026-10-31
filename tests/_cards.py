# tests/_cards.py
"""The parts of the cards on a page, read from each card's prop.

A card is one element (notes.Card), drawn by card.js from its `card` prop,
so `user.find(marker="note-star")` finds nothing: the star is not a server
element. `parts(user, "note-star")` reads the same things from the props
(the part's text, href, classes, colour) and presses them as the browser
does, with the card's `act` event. Each part keeps the marker its server
element carried, so a test reads as it did.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

def cards(user: User) -> list:
    """The page's cards (in `user.scope`), in the order the page made them."""
    try:
        found = user.find(marker="note-row").elements
    except AssertionError:
        return []
    return sorted(found, key=lambda e: e.id)


class Part:
    """One part of one card: `text`, `props` (href, color, icon), `classes`,
    `content` (a part's HTML), and `card`, the Card it is on."""

    def __init__(self, user, card, d: dict, *, act=None, field=None):
        self.user, self.card, self.d, self._act, self._field = user, card, d, act, field
        self.text = d.get("t") or ""
        self.content = d.get("html") or ""
        self.classes = (d.get("c") or "").split() + {
            "verdict": ["sb-verdict"], "md": ["nicegui-markdown"]}.get(d.get("k"), [])
        self.props = {k: v for k, v in (("href", d.get("h")), ("color", d.get("color")),
                                        ("icon", d.get("icon")), ("tip", d.get("tip")))
                      if v is not None}

    @property
    def id(self):
        return self.card.id

    def click(self, *, shift: bool = False) -> None:
        if self._act is None:
            raise AssertionError(f"{self.d.get('m')} takes no click")
        _emit(self.user, self.card, *self._act, *([shift] if self._act[0] == "view" else []))

    def hover(self) -> None:
        """The first hover over a chip whose tip the server fills."""
        _emit(self.user, self.card, "tip", self._field)

    def trigger(self, event: str, *_) -> Part:
        {"mouseenter": self.hover, "click": self.click}[event]()
        return self


def _emit(user, card, *args) -> None:
    """What card.js sends: one `act` event, its arguments a list."""
    UserInteraction(user, {card}, None).trigger("act", list(args))


def _markers(d: dict) -> list:
    m = d.get("m")
    return [] if m is None else [m] if isinstance(m, str) else list(m)


def _parts_of(user, card) -> list:
    c = card._props["card"]
    out = []
    for p in c["main"]:
        out.append((p, Part(user, card, p)))
        if p.get("k") == "answer":
            for o in p["options"]:
                out.append((o, Part(user, card, o, act=("answer", o["t"]))))
            out.append(({"m": "answer-field"}, AnswerField(user, card)))
    for i, ch in enumerate(c["foot"]):
        out.append((ch, Part(user, card, ch, field=i, act=("view", i) if ch.get("open") else None)))
        if v := ch.get("view"):
            out.append((v, Part(user, card, v, act=("view", i))))
    if c["star"] is not None:
        star = {"m": "note-star", "icon": "star" if c["star"] else "star_outline",
                "color": "warning" if c["star"] else None}
        out.append((star, Part(user, card, star, act=("star",))))
        out.append(({"m": "note-edit"}, Part(user, card, {"m": "note-edit"}, act=("edit",))))
    if c["resolve"]:
        out.append(({"m": "note-resolve"},
                    Part(user, card, {"m": "note-resolve"}, act=("resolve",))))
    for m, d, act in (("note-age", c["age"], None), ("note-id", c["sid"], None),
                      ("note-copy-id", {}, ("copy",))):
        if d is not None:          # an open checklist box has no age
            out.append(({"m": m}, Part(user, card, {"m": m, **d}, act=act)))
    return out


class AnswerField:
    """A question's answer field: type, then Enter, as the browser sends them."""

    def __init__(self, user, card):
        self.user, self.card = user, card
        self.value = card._props["card"].get("typed", "")

    @property
    def id(self):
        return self.card.id

    def type(self, text: str) -> AnswerField:
        self.value += text
        _emit(self.user, self.card, "typed", self.value)
        return self

    def trigger(self, event: str, *_) -> AnswerField:
        assert event == "keydown.enter", event
        _emit(self.user, self.card, "answer", self.value)
        return self


def bodies(user: User, card=None) -> list:
    """The HTML of each markdown piece of the cards' bodies, in page order."""
    return [p["html"] for c in ([card] if card is not None else cards(user))
            for p in c._props["card"]["main"] if p["k"] == "md"]


def template() -> str:
    """card.js beside these tests: what a test of a button's name or place
    reads. A path, not an import: the `user` fixture drops page modules."""
    return (Path(__file__).resolve().parents[1] / "src/symbion/gui/card.js").read_text()


def button(cls: str) -> str:
    """The template's one q-btn with class `cls`, as written."""
    (tag,) = re.findall(r'<q-btn\b[^>]*\bclass="%s"[^>]*/>' % re.escape(cls), template())
    return tag


def drawn(e) -> set:
    """The classes card.js draws inside card `e` for its prop, as `.class`
    selectors read them; `input` for the answer field's. Empty off a card.
    Each is checked against the template, so a rename there fails here."""
    c = getattr(e, "_props", {}).get("card")
    if not isinstance(c, dict):
        return set()
    out = {"sb-note-main", "sb-note-foot", "sb-note-by", "sb-copy"}
    if c["star"] is not None:
        out |= {"sb-actions", "sb-star", "sb-edit"}
    if c["resolve"]:
        out.add("sb-resolve")
    if any(p["k"] == "answer" for p in c["main"]):
        out |= {"sb-answer", "sb-answer-field", "input"}
    text = template()
    assert all(f'"{k}"' in text or f"{k} " in text for k in out - {"input"}), out
    return out


def parts(user: User, marker: str, *, content: str | None = None, card=None) -> list:
    """Every part with `marker` on the page's cards (or on `card`), in page
    order; with `content`, only those whose text holds it."""
    found = []
    for c in ([card] if card is not None else cards(user)):
        for d, part in _parts_of(user, c):
            if marker in _markers(d) and (content is None or content in getattr(part, "text", "")):
                found.append(part)
    return found


def part(user: User, marker: str, **kw):
    """The one part with `marker`: a test that means one says so."""
    found = parts(user, marker, **kw)
    assert len(found) == 1, f"{len(found)} parts marked {marker}"
    return found[0]


async def see(user: User, marker: str, *, retries: int = 3, **kw) -> None:
    """`should_see(marker=…)` for a card part: a write's redraw lands late."""
    for _ in range(retries):
        if parts(user, marker, **kw):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"no part marked {marker}")


async def not_see(user: User, marker: str, *, retries: int = 3, **kw) -> None:
    for _ in range(retries):
        if not parts(user, marker, **kw):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"a part marked {marker} is still there")
