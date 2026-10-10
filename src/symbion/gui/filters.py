"""URL query params <-> store.query() kwargs.

store.query takes scalar keyword filters, which is a query string. So the
browse view is not a page with a search feature -- it IS store.query(**params),
and every chip is an <a> that adds one parameter. Back button, bookmarks and
shareable URLs fall out of that for free.

No nicegui import: these are pure functions and they test without the extra.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

from .. import store

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
FILTER_KEYS = tuple(_URL_TO_KWARG) + ("structured", "q")


def search_pattern(q: str):
    """The search box's `q` as store.query's `grep`: every word, anywhere, in
    any case, taken literally. A person types `reload serve` meaning both
    words and pastes `$HOME` meaning `$HOME`; `list --grep`'s regex reads the
    first as one phrase and the second as an end-of-line anchor, a silent 0
    both times. `\\A` pins the
    lookaheads to one attempt per row instead of one per character."""
    words = q.split()
    if not words:
        return None
    return re.compile(r"\A" + "".join(f"(?=[\\s\\S]*?{re.escape(w)})" for w in words),
                      re.I)


def id_hit(note_id: str, q: str) -> bool:
    """Whether `q` is this note's id or its tail as summary and list print it
    (`…a1b`, `123456-a1b`). The search reads text, not ids, so a pasted id
    read 0 and looked like a row that was gone."""
    tail = store.id_tail(q)
    return bool(tail) and not any(c.isspace() for c in tail) and \
        (note_id == tail or note_id.endswith("-" + tail))


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
    grep = search_pattern(params.get("q") or "")
    if grep is not None:
        out["grep"] = grep
    return out


def href(**overrides) -> str:
    """A /notes URL for these filters. Keys are URL names; a None value drops
    the filter, which is how a chip clears one."""
    q = {k: v for k, v in overrides.items() if v is not None and v != ""}
    return f"/notes?{urlencode(q)}" if q else "/notes"


def describe(kwargs: dict, q: str = "") -> str:
    """Human-readable summary of active filters, for the page heading. The
    search is named by what was typed: its compiled `grep` is not."""
    parts = [f'search "{" ".join(q.split())}"'] if q.strip() else []
    parts += [f"{_KWARG_TO_URL.get(k, k)}={v}" for k, v in sorted(kwargs.items())
              if k != "grep"]
    return " · ".join(parts) or "all notes"
