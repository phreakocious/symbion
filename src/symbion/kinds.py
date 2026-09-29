"""The kinds table: labels are the project's, the three bits are symbion's.

A kind is a label plus three bits. `status`: the row carries open/resolved
and counts in every open view. `parked`: it carries a status but is hidden
from every open view (requires status). `verdict`: it carries --checked and
--result, is stamped with provenance at write, and has a state derived
against HEAD. status + verdict is a pre-registration. The seven labels below
are the defaults; a [kinds] table in symbion.toml replaces them entirely.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_FILE


@dataclass(frozen=True)
class Kind:
    status: bool = False
    parked: bool = False
    verdict: bool = False
    when: str = ""


DEFAULT_KINDS: dict[str, Kind] = {
    "note": Kind(when="a durable fact that fits nothing else: a gotcha, a footgun, how a thing works"),
    "decision": Kind(when="an ADR: a judgment call not obvious from the diff"),
    "bug": Kind(status=True, when="a known-broken thing to return to; resolve when fixed"),
    "task": Kind(status=True, when="a concrete next action; with --arc-id, a checklist box in that arc"),
    "question": Kind(status=True, when="needs the owner's answer before work can proceed; resolve --body adds the answer below it"),
    "idea": Kind(status=True, parked=True, when="a parked thought; resolve --add-tag adopted or retired, --body says why"),
    "check": Kind(verdict=True, when="a dated verification: --checked what ran, --result what it said"),
}

BITS = ("status", "parked", "verdict")
_KEYS = set(BITS) | {"when"}
_LABEL = re.compile(r"^[a-z][a-z0-9_-]*$")


def parse_kinds(section) -> dict[str, Kind]:
    """A parsed [kinds] table -> {label: Kind}. None (no table) -> the defaults.
    Every refusal names the offending label, so a 30-line table is fixed
    from the message alone."""
    if section is None:
        return dict(DEFAULT_KINDS)
    if not isinstance(section, dict) or not section:
        raise ValueError("[kinds] must be a non-empty table of `label = { ... }` entries")
    out = {}
    for label, v in section.items():
        if not isinstance(label, str) or not _LABEL.match(label):
            raise ValueError(f"[kinds] {label!r}: a label is lowercase letters, digits, _ or -")
        if not isinstance(v, dict):
            raise ValueError(f"[kinds] {label!r}: expected a table like {{ status = true }}")
        bad = sorted(set(v) - _KEYS)
        if bad:
            raise ValueError(f"[kinds] {label!r}: unknown key(s) {bad}; "
                             f"keys are status, parked, verdict, when")
        for b in BITS:
            if b in v and not isinstance(v[b], bool):
                raise ValueError(f"[kinds] {label!r}: {b} must be true or false")
        if "when" in v and not isinstance(v["when"], str):
            raise ValueError(f"[kinds] {label!r}: when must be a string")
        k = Kind(status=v.get("status", False), parked=v.get("parked", False),
                 verdict=v.get("verdict", False), when=v.get("when", ""))
        if k.parked and not k.status:
            raise ValueError(f"[kinds] {label!r}: parked requires status")
        out[label] = k
    return out


def _section(store_dir):
    p = Path(store_dir) / CONFIG_FILE
    if not p.exists():
        return None
    return tomllib.loads(p.read_text(encoding="utf-8")).get("kinds")


def read_kinds(store_dir) -> dict[str, Kind]:
    """The store's table: symbion.toml's [kinds], else the defaults."""
    return parse_kinds(_section(store_dir))


def is_declared(store_dir) -> bool:
    return _section(store_dir) is not None


def render_toml(kinds: dict[str, Kind]) -> str:
    """The table as toml: `init`'s starter file and `schema --toml`. A JSON
    string is a valid toml basic string, so any `when` round-trips."""
    lines = ["[kinds]"]
    w = max(len(label) for label in kinds)
    for label, k in kinds.items():
        parts = [f"{b} = true" for b in BITS if getattr(k, b)]
        if k.when:
            parts.append(f"when = {json.dumps(k.when, ensure_ascii=False)}")
        lines.append(f"{label:<{w}} = {{ {', '.join(parts)} }}")
    return "\n".join(lines) + "\n"
