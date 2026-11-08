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
from .. import summary as summ

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
FILTER_KEYS = tuple(_URL_TO_KWARG) + ("q",)


def search_pattern(q: str):
    """The search box's `q` as store.query's `grep`: every word, anywhere, in
    any case, taken literally. A person types `reload serve` meaning both
    words and pastes `$HOME` meaning `$HOME`; `list --grep`'s regex reads the
    first as one phrase and the second as an end-of-line anchor, a silent 0
    both times. Double quotes make a phrase, its words in order across any
    whitespace; an unclosed one runs to the end. `sbFilter` in chrome.py
    reads the box the same way. `\\A` pins the
    lookaheads to one attempt per row instead of one per character."""
    terms = [r"\s+".join(map(re.escape, (p or w).split()))
             for p, w in re.findall(r'"([^"]*)"?|([^\s"]+)', q)]
    terms = [t for t in terms if t]
    if not terms:
        return None
    return re.compile(r"\A" + "".join(f"(?=[\\s\\S]*?{t})" for t in terms), re.I)


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
    parts = [f"search: {' '.join(q.split())}"] if q.strip() else []
    parts += [f"{_KWARG_TO_URL.get(k, k)}={v}" for k, v in sorted(kwargs.items())
              if k != "grep"]
    return " · ".join(parts) or "All entries"


def title(kwargs: dict, q: str = "") -> str:
    """The filtered view's title in words, "Open tasks by claude": the query
    string read as code in a title and a tab (the GUI review, 2026-11-07).
    One entry's page keeps `describe`'s form: it is not a filtered list."""
    if "id" in kwargs:
        return describe(kwargs, q)
    if not q.strip() and not any(k != "grep" for k in kwargs):
        return "All entries"
    kind, typ, name = (kwargs.get(k) for k in ("kind", "target_type", "target_name"))
    words = " ".join(filter(None, [kwargs.get("status"),
                                   summ._count(2, kind).split(" ", 1)[1] if kind else "entries"]))
    words += "".join(f" {lead} {v}" for lead, v in (
        ("by", kwargs.get("author")), ("tagged", kwargs.get("tag") and f"#{kwargs['tag']}"),
        ("on", f"{typ}: {name}" if typ and name else typ or name),
        ("in arc", kwargs.get("arc_id")),
        ("matching", q.strip() and f"“{' '.join(q.split())}”")) if v)
    return words[0].upper() + words[1:]
