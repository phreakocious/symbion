"""URL query params <-> store.query() kwargs.

store.query takes seven scalar keyword filters, which is a query string. So the
browse view is not a page with a search feature -- it IS store.query(**params),
and every chip is an <a> that adds one parameter. Back button, bookmarks and
shareable URLs fall out of that for free.

No nicegui import: these are pure functions and they test without the extra.
"""
from __future__ import annotations

from urllib.parse import urlencode

# URL name -> store.query kwarg. `type`/`name` are shortened in the URL because
# they are the pair a human types; the rest match store.query exactly.
_URL_TO_KWARG = {
    "id": "id",
    "type": "target_type",
    "name": "target_name",
    "kind": "kind",
    "status": "status",
    "arc": "arc_id",
    "tag": "tag",
    "author": "author",
}
_KWARG_TO_URL = {v: k for k, v in _URL_TO_KWARG.items()}
FILTER_KEYS = tuple(_URL_TO_KWARG) + ("structured",)


def from_params(params) -> dict:
    """Parsed query params -> store.query kwargs. Unknown keys and empty
    values are DROPPED, never raised: a stale bookmark must still render."""
    out = {}
    for url_key, kwarg in _URL_TO_KWARG.items():
        v = (params.get(url_key) or "").strip()
        if v:
            out[kwarg] = v
    s = (params.get("structured") or "").strip().lower()
    if s in ("true", "false"):
        out["structured"] = (s == "true")
    return out


def href(**overrides) -> str:
    """A /notes URL for these filters. Keys are URL names; a None value drops
    the filter, which is how a chip clears one."""
    q = {k: v for k, v in overrides.items() if v is not None and v != ""}
    return f"/notes?{urlencode(q)}" if q else "/notes"


def describe(kwargs: dict) -> str:
    """Human-readable summary of active filters, for the page heading."""
    if not kwargs:
        return "all notes"
    parts = [f"{_KWARG_TO_URL.get(k, k)}={v}" for k, v in sorted(kwargs.items())]
    return " · ".join(parts)
