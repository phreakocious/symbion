"""Note and Arc schema: dataclasses, serialization, kind/status invariants.

Lifted from a working reference: the research notebook symbion was
extracted from. Field names on disk are a contract with every store already
written: renaming one takes a migration and a dated note in the specs, as
the 2026-09-08 vocabulary change did for `activity_id` and `global`.

This module holds the schema, the locked file I/O and the arc registry.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:                     # Windows
    fcntl = None
    import errno
    import msvcrt
import json
from collections import Counter
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tomllib
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, fields as dc_fields, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep

from . import catalog  # for nfc() only -- catalog does not import store, so
                        # this does not invert the layering.

from . import kinds as K
from . import merge
from .config import CONFIG_FILE

# ---- constants ----
STATUSES = {"open", "resolved"}
# `item` is the no-catalog target type: freeform names, never canonicalized,
# always `uncheckable` in reconcile. Without it, a project with no [catalogs]
# has no legal --scope and cannot make a plain checklist at all.
BUILTIN_TARGET_TYPES = {"commit", "project", "arc", "item"}


# ---- dataclasses ----
@dataclass(frozen=True)
class Target:
    type: str                  # one of BUILTIN_TARGET_TYPES, or a catalog type
    name: str | None           # None for project; the arc id for arc


@dataclass(frozen=True)
class Note:
    id: str
    kind: str                  # a label in the store's [kinds] table
    target: Target
    created_at: str            # ISO 8601
    author: str
    body: str
    status: str | None = None  # open | resolved (bug/task)
    checked: str | None = None # check: what was checked
    result: str | None = None  # check: verdict
    arc_id: str | None = None
    supersedes: str | None = None
    due: str | None = None     # YYYY-MM-DD or an ISO datetime; status kinds only
    tags: tuple = ()
    refs: tuple = ()           # secondary (Target) objects this note also implicates
    provenance: dict | None = None  # domain stamp: what this note was measured against
    measurements: dict | None = None  # flat: finite float/int/str values only
    options: tuple | None = None      # question: its answers, the default first (check_fields)
    target_blob: str | None = None    # the target file's blob id when written (gitref.target_blob)
    side: str | None = None           # shared store: the writing clone's git user.name
    extra: dict | None = None         # a newer symbion's fields, written back as read (_ROW_KEYS)
    spec: K.Kind = K.Kind()           # the kind's bits, set at read time; NEVER serialized
    # Its chain's first created_at, set at read time on a revision; NEVER
    # serialized. A revision is no re-reading of the target (see target_blob).
    first_written: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Arc:
    id: str                    # URL-safe slug, stable across rename
    name: str
    description: str
    target_scope: str          # checked against arc_from_dict/create_arc's
                                # optional legal_scopes set; unchecked when omitted
    created_at: str
    author: str
    archived: bool = False
    archived_at: str | None = None
    side: str | None = None            # a shared store: the side that created the arc
    phases: tuple | None = None        # board columns, left to right; None: no phases
    # One {id, phase, by?, at?} per placed box; list order is the order within a phase.
    # ponytail: a shared store merges it as one field, so the upstream's whole order wins; merge per box if two writers reorder one arc
    placement: tuple | None = None
    changed_by: str | None = None      # the last edit's writer, for commit's `Arcs:` trailer
    changed_at: str | None = None
    extra: dict | None = None          # a newer symbion's fields, written back as read


# The keys a row's line holds that this symbion writes. Any other is a newer
# symbion's field, kept through an edit, or the row loses it at an older
# install's edit (the user, 2026-10-19). A key this symbion took out
# is not carried: every row written before 2026-10-09 holds `"evidence": []`.
_ROW_KEYS = frozenset(f.name for f in dc_fields(Note)) - {"spec", "first_written", "extra"}
_RETIRED_KEYS = frozenset({"evidence"})
_ARC_KEYS = frozenset(f.name for f in dc_fields(Arc)) - {"extra"}


# ---- status and verdict invariants ----
def default_status_for(spec: K.Kind) -> str | None:
    """What `add` stores when --status is omitted. The reference defaulted every
    kind to None, so `add --kind bug` stored status: null and then never
    matched query(status="open") — a bug invisible to every open view."""
    return "open" if spec.status else None


def read_status(note) -> str | None:
    """The stored status, verbatim.

    This used to return "open" for a status-kind row carrying status: null --
    read-side tolerance for rows written before the invariant existed. That
    clause was measured dead on 2026-09-11: no store symbion wrote relied on
    it. The one exception was a store built by a hand transform, where heads
    with a null status made the guess wrong in the expensive direction: it
    inflated the session-start headline ~4x into a backlog nobody could act
    on.

    The reference's store, which this tolerance was written for, is NOT
    evidence either way and was checked: it is not a symbion store, and none
    of the rows symbion can read from it is on a status kind. Nothing symbion
    reads anywhere depended on the clause.

    A null status on a status kind is now a defect in the FILE, not a state
    to guess at, and `summary` reports the count every run while any remain
    -- see summary.no_status. add_many defaults it (default_status_for), so
    only a write that bypasses symbion can mint one.

    A status on a kind WITHOUT the bit is the opposite case and reads as
    None: the kinds table is the semantics, the file is the history. A store
    that gives a result kind a status bit it never needed mints its finished
    results open; dropping the bit has to empty the status views at once,
    and restoring it has to bring the rows back, with no sweep in between. `read_dict` carries the same reading into --json."""
    return note.status if note.spec.status else None


DUE_FORMAT = "YYYY-MM-DD, or an ISO 8601 datetime like 2026-10-01T22:30Z"


def canon_due(value: str) -> str:
    """A `due` value as stored: a date stays a date; a datetime gets an offset,
    the display zone's when it has none, so a reader in another zone does not
    move it. Raises ValueError naming the format on anything else."""
    s = value.strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"due {value!r} is not a date: use {DUE_FORMAT}") from None
    return typed(dt).isoformat()


SINCE_FORMAT = "a span back from now (30m, 2h, 3d, 1w), a date or an ISO datetime"
_SPAN = re.compile(r"(\d+)([mhdw])")


def since_cutoff(value: str, now: datetime | None = None) -> datetime:
    """`list --since`: the instant a window opens, as `git log --since`
    reads it. A span counts back from `now`, a date opens at its start in the
    display zone, a datetime without an offset is in the display zone."""
    now = now or now_shown()
    s = value.strip()
    m = _SPAN.fullmatch(s)
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m[2]]
        return now - timedelta(**{unit: int(m[1])})
    try:
        return typed(datetime.combine(date.fromisoformat(s), time.min))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"--since {value!r}: use {SINCE_FORMAT}") from None
    return typed(dt)


def written_at(n, stamp: str | None = None) -> datetime:
    """A row's created_at, or `stamp`, as an instant: what every order sorts on. A string
    sort put `10-01T05:00+09:00` after `09-30T22:00+00:00`, though it is two
    hours earlier, so a store written from two zones listed out of order. Rows from before
    offsets were stamped are naive: local wall clock, which is what they
    were written in. A hand-edited stamp that is not ISO sorts oldest
    rather than stopping every read."""
    try:
        dt = datetime.fromisoformat(stamp or n.created_at)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.astimezone()


def utc_stamp(stamp: str) -> str:
    """A stamp in UTC, as rows are written now, for a reader that sorts it as
    a string: an import keeps a local offset. A stamp that is not ISO stays
    as written."""
    try:
        datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return stamp
    return written_at(None, stamp).astimezone(timezone.utc).isoformat()


def newest_first(rows) -> list:
    """Rows by when they were written, newest first; of rows written in one
    second, the later in `rows` first. Not the id: an edit keeps the row's
    id, so the id no longer says when a version was written. Every caller
    passes rows in file order, which is the order they were written in."""
    return sorted(reversed(list(rows)), key=written_at, reverse=True)


def due_state(due: str, now: datetime | None = None) -> tuple[bool, int, timedelta | None] | None:
    """(past due, calendar days from today to the due date, time left), at
    `now`'s offset, the display zone's by default. A date is due through the
    end of its day, and has no time left; a datetime is past due from that
    instant, so it can be past due with 0 days left, and its time left is
    then below zero. None for a value the writer would refuse -- a
    hand-edited row still loads."""
    now = now or now_shown()
    try:
        d = date.fromisoformat(due)
        return now.date() > d, (d - now.date()).days, None
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(due)
    except ValueError:
        return None
    dt = (dt if dt.tzinfo else dt.astimezone()).astimezone(now.tzinfo)
    return now >= dt, (dt.date() - now.date()).days, dt - now


def is_overdue(note, now: datetime | None = None) -> bool:
    """Open and past due. False for an unreadable due, which no write mints."""
    state = due_state(note.due, now) if note.due else None
    return read_status(note) == "open" and state is not None and state[0]


def without_self_ref(target: dict, refs) -> list:
    """`refs` less a ref to the row's own target: it adds nothing, and
    `context --target` reported the row twice. Refusing it cost a retyped
    write each time (2026-11-12), so it is dropped. Compared on RESOLVED
    names: `file:cli` and `file:src/cli.py` are one object once the catalog
    has run."""
    own = (target["type"], target.get("name"))
    return [r for r in refs or () if (r["type"], r.get("name")) != own]


NULL_WORDS = ("None", "null")


def has_result(value) -> bool:
    """Is a verdict present? Not absent, not blank, not a null's spelling
    (which rule 5 of `check_fields` refuses now, and older rows still hold).
    The resolve gate and `api.verdict_state`'s `pending` ask the same."""
    return bool((value or "").strip()) and value not in NULL_WORDS


def stamp_sha(prov) -> str | None:
    """A provenance stamp's commit, or None. A configured provenance command's
    object is stored as printed, so its `sha` can be any JSON value, and
    `{"sha": 123}` crashed every reader that sliced it (review, 2026-10-02).
    The one door for reading it."""
    sha = prov.get("sha") if isinstance(prov, dict) else None
    return sha if isinstance(sha, str) and sha else None


def control_char(s) -> str | None:
    """The first C0 or DEL character in `s`, for a name or a tag."""
    return next((c for c in s if c < " " or c == "\x7f"), None) if isinstance(s, str) else None


def check_fields(kind: str, spec: K.Kind, d: dict) -> None:
    """The write-side invariants, determined by the kind's bits and shared by
    `add_many`, `_supersede_unlocked` and `api.fields_from_row` -- a
    correction (or `resolve`, which is `supersede` with a fixed status) must
    not reach a state `add` itself would have refused. The rules:

    1. `status` only on a kind with the status bit.
    2. `checked`/`result` only on a kind with the verdict bit.
    3. A status+verdict row (a pre-registration) cannot be `resolved` with an
       empty `result`: the resolving row IS the verdict. `d` is the whole
       resulting row, so an inherited result satisfies it.
    4. `due` only on a kind with the status bit -- only an open row can be
       past due -- and only in a form `canon_due` reads.
    5. No `checked`/`result` that is a null's spelling (`None`, `null`): the
       word passed rule 3 as a verdict (measured 2026-09-26).
    6. `status` is open or resolved, and `tags` is not one string: a script
       stored `wontfix`, a row in no view, and "abc" as three tags (2026-10-09).
    7. `options` only on a question. By the kind's NAME, not a bit: a
       supersede inherits the tip's fields and `--kind` moves a question to
       a task (they share bits), so the rule judges the row as written and
       refuses the move, as the kind-change rule refuses a shed field. A row
       that carries them under a renamed kind (the reference's sed) is a
       legacy row: `unholdable` drops them on its next write.
       ponytail: `summary.asks` names the kind the same way; a bit in
       [kinds] lifts both when a store renames it.

    The reader never calls this: a row already on disk loads as written."""
    for key in ("checked", "result"):
        if d.get(key) in NULL_WORDS:
            raise ValueError(f"--{key} {d[key]!r} stores the word, not an absent {key}: "
                             f"omit the flag, or say what it found")
    if d.get("status") is not None and not spec.status:
        raise ValueError(f"status is not valid for kind {kind!r}: it has no status bit, so "
                         f"there is nothing to open or resolve. A plain entry leaves the "
                         f"default views with `supersede <id> --add-tag retired`; "
                         f"`symbion schema` lists each kind's bits")
    if d.get("status") not in (None, *STATUSES):
        raise ValueError(f"status {d['status']!r} is neither open nor resolved, so no view "
                         f"would show the entry")
    if isinstance(d.get("tags"), str):
        raise ValueError(f"tags must be a list of strings, not the string {d['tags']!r}")
    if bad := next((t for t in d.get("tags") or () if control_char(t)), None):
        raise ValueError(f"tag {bad!r} holds a control character ({control_char(bad)!r}): "
                         f"a newline split `list`'s one line per entry")
    if not spec.verdict and (d.get("checked") is not None or d.get("result") is not None):
        raise ValueError(f"--checked/--result are not valid for kind {kind!r}: "
                         f"it has no verdict bit; put the evidence in --body "
                         f"(`symbion schema` lists the verdict kinds)")
    if (spec.status and spec.verdict and d.get("status") == "resolved"
            and not has_result(d.get("result"))):
        raise ValueError(f"kind {kind!r} cannot be resolved without --result: it is a "
                         f"pre-registration, and the resolving entry carries the verdict: "
                         f"`resolve <id> --result \"what the data said\"`")
    if d.get("due") is not None:
        if not spec.status:
            raise ValueError(f"--due is not valid for kind {kind!r}: it has no status bit, "
                             f"so it is never open and can never be past due "
                             f"(`symbion schema` lists the status kinds)")
        canon_due(d["due"])
    if d.get("options") is not None and kind != "question":
        raise ValueError(f"options on kind {kind!r}: only a question takes answers. A "
                         f"question changing kind drops them with `--option \"\"`")


# ---- serialization ----
# The vocabulary change of 2026-09-08. An error accepts nothing, so this is
# not a compatibility surface; it is the one place an un-migrated store can
# learn what happened.
_VOCAB_HINT = ("the vocabulary changed on 2026-09-08 (followup->task, anomaly->bug, "
               "audit->check, activity->arc, global->project) and this store has not "
               "been migrated")
# ponytail: refused unconditionally on load, where the catalogs are unknown;
# a store that declares a catalog type named `global` or `activity` would need
# load() to pass its target types.
_RETIRED_TYPES = ("global", "activity")


def check_types(objs, target_types) -> None:
    """Each object a write resolved is of a type the store declares. The
    rule that resolves a name runs in the api, and a name it missed (a
    `file:` in a store with [repos]) stored a row no read could reach (the
    repos design). None checks nothing: a reader, or an inherited name."""
    if target_types is None:
        return
    for o in objs:
        t = o.type if isinstance(o, Target) else o["type"]
        if t not in target_types:
            raise ValueError(f"unknown target type: {t!r}")


_TEXT_KEYS = ("id", "kind", "created_at", "author", "body", "status", "checked", "result",
              "arc_id", "supersedes", "due", "target_blob", "side")


def _check_types(d: dict) -> None:
    """A row another tool wrote with a number for a name or a body raised in
    a reader far from the load: the session-start gotcha list (audit,
    2026-11-08). The load refuses it here, and `load_malformed` names its line."""
    def refuse(where, want, v):
        raise ValueError(f"{where} must be {want}, not {type(v).__name__} {v!r}")
    for k in _TEXT_KEYS:
        if (v := d.get(k)) is not None and not isinstance(v, str):
            refuse(k, "a string", v)
    for k in ("tags", "options", "refs"):
        if (v := d.get(k)) is not None and not isinstance(v, (list, tuple)):
            refuse(k, "a list", v)
    for k in ("tags", "options"):
        for x in d.get(k) or ():
            if not isinstance(x, str):
                refuse(f"each of {k}", "a string", x)
    for where, t in (("target", d.get("target")), *(("ref", r) for r in d.get("refs") or ())):
        if isinstance(t, dict):
            if not isinstance(t.get("type"), str):
                refuse(f"a {where}'s type", "a string", t.get("type"))
            if t.get("name") is not None and not isinstance(t["name"], str):
                refuse(f"a {where}'s name", "a string", t["name"])


def note_from_dict(d: dict, target_types=None, kinds=None) -> Note:
    """`kinds` is the store's table (label -> Kind); None means the defaults.
    An undeclared label is refused with the declared set, and the three
    retired names get the migration hint on top."""
    kinds = K.DEFAULT_KINDS if kinds is None else kinds
    _check_types(d)
    t = d["target"]
    target = t if isinstance(t, Target) else Target(type=t["type"], name=t.get("name"))
    if d["kind"] not in kinds:
        hint = ""
        if d["kind"] in ("followup", "anomaly", "audit"):
            hint = (f": {_VOCAB_HINT}; rename them in notes.jsonl, or declare it "
                    f"under [kinds] in symbion.toml")
        raise ValueError(f"unknown kind: {d['kind']!r}; this store's kinds are "
                         f"{', '.join(kinds)} (declared under [kinds] in symbion.toml){hint}")
    retired = [x["type"] for x in (t, *(d.get("refs") or ()))
               if isinstance(x, dict) and x.get("type") in _RETIRED_TYPES]
    if retired or target.type in _RETIRED_TYPES:
        raise ValueError(f"retired target type {(retired or [target.type])[0]!r}: "
                         f"{_VOCAB_HINT}; rename it in notes.jsonl")
    refs = tuple(r if isinstance(r, Target) else Target(type=r["type"], name=r.get("name"))
                 for r in (d.get("refs") or ()))
    check_types((target, *refs), target_types)
    # By name, whatever order they were written in, so every reader lists
    # them alike and two rows' numbers line up (the user, 2026-10-09).
    m = d.get("measurements")
    return Note(
        id=d["id"], kind=d["kind"], target=target,
        created_at=d["created_at"], author=d.get("author", "unknown"),
        body=d.get("body") or "", status=d.get("status"),
        checked=d.get("checked"), result=d.get("result"),
        arc_id=d.get("arc_id"), supersedes=d.get("supersedes"), due=d.get("due"),
        tags=tuple(d.get("tags") or ()),
        refs=refs,
        provenance=d.get("provenance"),
        measurements=dict(sorted(m.items(), key=lambda kv: kv[0].casefold())) if m else m,
        # In the order given: the first is the default (the user, 2026-10-16).
        options=tuple(o) if (o := d.get("options")) else None,
        target_blob=d.get("target_blob"),
        side=d.get("side"),
        extra={k: v for k, v in d.items() if k not in _ROW_KEYS | _RETIRED_KEYS} or None,
        spec=kinds[d["kind"]],
    )


def read_dict(note: Note) -> dict:
    """The row as a reader sees it: `note_to_dict` with the status its kind
    can hold (read_status). note_to_dict stays verbatim -- it is also the
    writer, and the file keeps what was written."""
    return note_to_dict(note) | {"status": read_status(note)}


def note_to_dict(note: Note) -> dict:
    return {
        "id": note.id, "kind": note.kind,
        "target": {"type": note.target.type, "name": note.target.name},
        "created_at": note.created_at, "author": note.author,
        "body": note.body, "status": note.status,
        "checked": note.checked, "result": note.result,
        "arc_id": note.arc_id, "supersedes": note.supersedes, "due": note.due,
        "tags": list(note.tags),
        "refs": [{"type": r.type, "name": r.name} for r in note.refs],
        "provenance": note.provenance,
        "measurements": note.measurements,
        "options": list(note.options) if note.options is not None else None,
        "target_blob": note.target_blob,
        **({"side": note.side} if note.side is not None else {}),
        **(note.extra or {}),
    }


def _dumps(obj) -> str:
    # ensure_ascii=False keeps diacritics readable in the JSONL; names are
    # NFC-normalized on the way in, so the bytes are stable.
    return json.dumps(obj, ensure_ascii=False)


def jsonl_lines(text: str) -> list[str]:
    """The lines of a JSONL text. Not str.splitlines(): it also splits at
    U+2028, U+2029 and U+0085, which _dumps leaves raw inside a string, so a
    row holding one read back as two unreadable halves (2026-10-09)."""
    return text.removesuffix("\n").split("\n")


# ==========================================================================
# I/O layer: locking, notes file, supersede, heads, query, commit.
#
# Lifted from the reference, with one structural change from it: the lock
# moves to the OUTER boundary. Every public mutating entry point (`add`,
# `supersede`, `commit`) holds ONE `_lock` spanning load -> validate -> mint
# ids -> write. The reference's `supersede` loads outside the lock, so two
# concurrent calls can read the same head and both append, forking the
# supersede chain.
#
# `_lock` must NEVER nest: flock is per open-file-description, so a second
# LOCK_EX from the same process deadlocks. Internal helpers below are
# unlocked (`_..._unlocked`) and assume the caller already holds the lock;
# `load`/`load_malformed` are the public lock-free reads.
# ==========================================================================

NOTES_FILE = "notes.jsonl"
ARCS_FILE = "arcs.jsonl"
LOCK_FILE = ".lock"


# ---- ids & timestamps ----
# Stored in UTC; shown in UTC unless TZ is set (ruled 2026-09-30, reversing
# 2026-09-05's local wall clock): a local date beside a UTC one in the same
# view read as a day's disagreement. Rows stamped before keep their offset or,
# older still, none; `written_at` orders all three as instants.
class BadClock(ValueError):
    """`SYMBION_NOW` holds no ISO 8601 time with an offset."""


def _utcnow() -> datetime:
    """The one clock. `SYMBION_NOW` stops it: the public demo serves a
    snapshot whose ages must read as on its day (the user, 2026-10-22). A
    time with no offset refuses, as it would read as the host's own zone;
    an empty value is unset."""
    if v := os.environ.get("SYMBION_NOW"):
        try:
            t = datetime.fromisoformat(v)
        except ValueError:
            t = None
        if t is None or t.tzinfo is None:
            raise BadClock(f"SYMBION_NOW={v}: not an ISO 8601 time with an offset, "
                           "such as 2026-10-22T19:47:54Z")
        return t.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def zone():
    """The zone symbion prints in and reads an offset-less input in: UTC,
    or the local zone when TZ is set, which is how a user asks for one.
    None means local, as `astimezone` takes it."""
    return None if os.environ.get("TZ") else timezone.utc


def now_shown() -> datetime:
    return _utcnow().astimezone(zone())


def typed(dt: datetime) -> datetime:
    """An input datetime, offset-less read in the display zone."""
    if dt.tzinfo:
        return dt
    return dt.replace(tzinfo=zone()) if zone() else dt.astimezone()


def shown(stamp: str) -> str:
    """A stored stamp in the display zone, for a reader: `2026-09-30
    23:58:29Z`. A bare date has no zone, and it and a stamp that will not
    parse print as stored."""
    try:
        date.fromisoformat(stamp)
        return stamp
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return str(stamp)
    dt = (dt if dt.tzinfo else dt.astimezone()).replace(microsecond=0)
    s = dt.astimezone(zone()).isoformat(sep=" ")
    return s.removesuffix("+00:00") + "Z" if s.endswith("+00:00") else s


def _now_iso() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _now_fine() -> str:
    """A stamp commit's `Arcs:` trailer reads as a change (an arc edit, a box
    move): at seconds, a change in the second of the commit before it read
    as none, and counted `unknown`."""
    return _utcnow().isoformat(timespec="microseconds")


_last_minted = None


def new_id(_clock=None, _rand=None) -> str:
    """Microsecond-resolution, lexically-sortable id: YYYYMMDD-HHMMSS-ffffff-xxx,
    in UTC. _clock/_rand are injectable for deterministic tests.

    A later mint sorts later, so a batch's ids read in the order it wrote
    them. Windows before Python 3.13 reads the clock in
    ~15.6 ms ticks, which gave a whole batch one microsecond: a reading at or
    up to a second behind the last mint steps one microsecond past it. A
    clock set back further is read as it is."""
    # ponytail: per process; two processes minting in one tick are concurrent
    # writers, and the random tail orders them
    global _last_minted
    clock = _clock or _utcnow
    rand = _rand or (lambda: secrets.randbelow(4096))
    now = clock()
    if _last_minted is not None and timedelta(0) <= _last_minted - now < timedelta(seconds=1):
        now = _last_minted + timedelta(microseconds=1)
    _last_minted = now
    return f"{now.strftime('%Y%m%d-%H%M%S-%f')}-{rand():03x}"


def _kept_id(created_at: str) -> str:
    """An id for the version an edit keeps, on that version's own second, so
    an id read as a date names the version it is on. `created_at` holds no
    microseconds: random ones stand in, since a sweep keeps many versions
    written in one second. It takes no part in new_id's order."""
    try:
        at = datetime.fromisoformat(created_at).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return new_id()
    return f"{at:%Y%m%d-%H%M%S}-{secrets.randbelow(10**6):06d}-{secrets.randbelow(4096):03x}"


def id_tail(raw: str) -> str:
    """An id or tail as people cite one, bare: `…a1b`, `...a1b` and `-a1b`
    are `a1b`. symbion itself prints "rows end in -a1b", and a search for
    `-a1b` found nothing (the user, 2026-10-09)."""
    return raw.strip().removeprefix("…").removeprefix("...").lstrip("-")


# ---- store paths & bootstrap ----
def notes_path(store) -> Path:
    return Path(store) / NOTES_FILE


def exists(store) -> bool:
    """Whether a store has ever been bootstrapped (`symbion init` ran for
    it, or an older symbion's first write created it). Tells an absent
    store (`no store at <path>`; `summary --json` answers `store: null`)
    from an empty one, which renders its zero counts. The hook's silence in a project without a store is
    session_start.sh's own check, made before it runs `summary`."""
    return notes_path(store).exists()


def is_store(root) -> bool:
    """Whether `root` is itself a store: the PAIR ensure_store writes, not
    `notes.jsonl` alone, which is a common enough filename that a project
    holding one must not be mistaken for its own store and written into."""
    return notes_path(root).exists() and arcs_path(root).exists()


def arcs_path(store) -> Path:
    return Path(store) / ARCS_FILE


class ReadOnly(Exception):
    """A write while `SYMBION_READ_ONLY` is set."""

    def __init__(self):
        super().__init__("read-only: nothing here writes")


def read_only() -> bool:
    """`serve --read-only` sets SYMBION_READ_ONLY for the public demo, and
    every write then refuses: at `_lock`, and in `api.save_doc` and
    `api.push`, the two writes that do not take it. Any non-empty value."""
    return bool(os.environ.get("SYMBION_READ_ONLY"))


class Untrusted(ValueError):
    """A write while a pull's change to symbion.toml waits on `symbion trust`."""

    def __init__(self):
        super().__init__(catalog.UNTRUSTED)


def distrust(store, old_head: str) -> None:
    """Shut the gate a pull's change to symbion.toml opens, at the HEAD
    before it; one already shut keeps its earlier HEAD, so `trust` shows
    every change since."""
    if (p := catalog.untrusted_path(store)) is not None and not p.exists():
        _replace_atomically(p, old_head + "\n")


class Busy(Exception):
    """Another command held the store's lock past a caller's deadline."""


def _try_lock(fd) -> bool:
    """One try at the store's lock that never waits: whether it was taken."""
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except BlockingIOError:
        return False
    except OSError as e:                     # Windows says a held byte with EACCES
        if fcntl or e.errno not in (errno.EACCES, errno.EDEADLOCK):
            raise
        return False
    return True


@contextmanager
def _lock(store, undoable: bool = True, until: float | None = None, gated: bool = True):
    """Exclusive lock over the whole store, held for one mutation. A `gated`
    hold refuses while a pulled symbion.toml waits on `symbion trust`
    (catalog.untrusted); a pull is not gated.

    NEVER NEST — flock is per open-file-description, so a second LOCK_EX from
    this same process would deadlock. Acquire only at a top-level mutating entry
    point, and after require_store so the directory exists.

    An `undoable` hold copies the files a write changes before it runs, and
    keeps the copy as the one undo level if the write changed them (`undo`).
    With `until`, a time.monotonic() deadline, a lock still held at it
    raises Busy instead of waiting on."""
    if read_only():
        raise ReadOnly()
    p = Path(store) / LOCK_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if until is not None:
            while not _try_lock(fd):
                if monotonic() >= until:
                    raise Busy("another symbion command holds the store")
                sleep(0.05)
        elif fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            # Byte 0, locked past EOF is allowed. LK_LOCK gives up after 10
            # tries a second apart; flock waits, so this waits too.
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError as e:
                    if e.errno != errno.EDEADLOCK:
                        raise
        snap = None
        try:
            # Held: a pull this write waited on may have shut the gate.
            if gated and catalog.untrusted(store):
                raise Untrusted()
            snap = _snapshot(store) if undoable else None
            yield
        finally:
            if snap:
                _seal(store, *snap)
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _replace_atomically(path: Path, body: str | bytes) -> None:
    """Write via temp + fsync + rename, so a crash leaves the old file whole.
    Bytes are written as given: a pull puts a file back byte for byte."""
    tmp = path.with_name(f".tmp.{os.getpid()}.{path.name}")
    try:
        # LF on every platform: Windows text mode writes CRLF, and a store
        # is shared by clones on all of them.
        with open(tmp, "wb") as f:
            f.write(body.encode("utf-8") if isinstance(body, str) else body)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(path, tmp)      # a 0600 settings file stays 0600
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def require_store(store) -> None:
    """Every write's first step: only `symbion init` creates a store. A write
    that created one forked a renamed repo's, a mistyped --dir's or a stale
    SYMBION_DIR's store, printing an ordinary id (2026-09-11). A store that
    exists still gets any piece it lacks, but not under read-only: that ran
    before the lock refused, and made `docs/` (a reviewer, 2026-10-22)."""
    if read_only():
        raise ReadOnly()
    if not exists(store):
        raise ValueError(f"no store at {store}; run `symbion init`")
    ensure_store(store)


class NoSide(ValueError):
    """A write in a shared store from a clone with no git user.name."""


def shared(store) -> bool:
    """The store's symbion.toml sets `shared = true` (config.load refuses any
    other value, so a read here asks only for the one)."""
    p = Path(store) / CONFIG_FILE
    return p.exists() and tomllib.loads(p.read_text(encoding="utf-8")).get("shared") is True


def shared_side(store) -> str | None:
    """None unless the store is shared; else this clone's side, its git
    user.name, in NFC as every name the store keeps is: a clone typed on a
    macOS and one on a Linux then compare equal. Never cached: a write asks
    before its first line."""
    if not shared(store):
        return None
    r = subprocess.run(["git", "-C", str(store), "config", "user.name"],
                       capture_output=True, text=True, encoding="utf-8")
    if not (name := r.stdout.strip()):
        raise NoSide(f"this store is shared, and its clone has no git user.name: "
                     f"git -C {shlex.quote(str(store))} config user.name NAME")
    return catalog.nfc(name)


def read_side(store) -> str | None:
    """shared_side for a read: a clone with no user.name reads as no side,
    since only a write refuses it by name."""
    try:
        return shared_side(store)
    except NoSide:
        return None


UNDO_DIR = "symbion-undo"


def _stamps(store) -> list:
    """What a write changes, as the OS sees it: size, time and inode of each
    file. Two equal stamps mean nothing wrote between them."""
    out = []
    for f in (notes_path(store), arcs_path(store)):
        try:
            st = f.stat()
            out.append([st.st_size, st.st_mtime_ns, st.st_ino])
        except FileNotFoundError:
            out.append(None)
    return out


def _snapshot(store):
    """Copy the files a write may change into STATE_DIR, where git never
    sees them. A copy, not `git add`: each add left a blob of the whole
    file that nothing references, over a quarter of its size, and git
    prunes those after two weeks (measured 2026-10-13; a copy took 5-8 ms).
    A failed copy costs the undo, never the write."""
    try:
        sess = _state_dir(store, make=True) / SESSIONS_FILE
        new = sess.with_name(UNDO_DIR + ".new")
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir()
        for f in (notes_path(store), arcs_path(store), sess):
            if f.exists():
                shutil.copyfile(f, new / f.name)
    except OSError:
        return None
    return new, _stamps(store)


def _seal(store, new: Path, before: list) -> None:
    """Keep the copy as the undo level if the write changed a file. A write
    that changed nothing (a refused add) leaves the last one's level alone."""
    try:
        after = _stamps(store)
        if after == before:
            shutil.rmtree(new, ignore_errors=True)
            return
        (new / "last.json").write_text(json.dumps(
            {"session": session_key() or "", "after": after}),
            encoding="utf-8")
        cur = new.with_name(UNDO_DIR)
        shutil.rmtree(cur, ignore_errors=True)
        os.replace(new, cur)
    except OSError:
        pass


def _forget_undo(store) -> None:
    sess = _sessions_path(store)
    if sess is not None:
        shutil.rmtree(sess.with_name(UNDO_DIR), ignore_errors=True)


class UndoRefused(ValueError):
    pass


def _undo_level(store):
    """(sessions file, the undo level's dir, its write's record), refused
    when there is no level or the files changed since its write."""
    sess = _sessions_path(store)
    level = sess.with_name(UNDO_DIR) if sess else None
    try:
        last = json.loads((level / "last.json").read_text(encoding="utf-8"))
    except (TypeError, OSError, ValueError):
        raise UndoRefused("nothing to undo: no write since the last commit or undo")
    if last.get("after") != _stamps(store):
        raise UndoRefused("the store's files changed after its last write, outside "
                          "symbion (a git command, a hand edit), so nothing was undone")
    return sess, level, last


def undo_state(store) -> str | None:
    """Whose write the one undo level holds, the files as it left them:
    "mine", one this session made (none, for a person), which it can take
    back; "other", another session's; None, no level (a commit, a pull or an
    undo ended it). The GUI's undo button reads it on every page and every
    few seconds, so it reads no rows: `undo` checks the author on the click."""
    try:
        last = _undo_level(store)[2]
    except UndoRefused:
        return None
    return "mine" if last.get("session", "") == (session_key() or "") else "other"


def undo(store, note_id: str | None, mine, apply: bool = True) -> dict:
    """Put the store back as it was before its last write, one level. The
    misclick case (a resolve, a star, an edit) and an in-place edit, whose
    earlier text is on no disk, are why (the user, 2026-10-01). A batch
    (`add --from-json`, `arc seed`, `rename`) is one write. Refused, naming
    why, unless `note_id` is a row or arc that write touched (None: the
    caller was shown the write, as the GUI's dialog shows it), `mine(last,
    rows)` accepts the write (`last` is its record, `rows` the rows it
    wrote), and nothing changed the files since. A commit ends the level:
    taking back committed work is a reverting row. `apply=False` checks all
    of it and writes nothing.

    Returns {"removed": ids the write added, "changed": ids it changed,
    "arcs": arc ids it added or changed, "rows": the rows it wrote,
    "before": {id: the version the undo puts back} for each changed id}."""
    require_store(store)
    with _lock(store, undoable=False):
        sess, level, last = _undo_level(store)

        def lines(path):
            return [ln for ln in jsonl_lines(path.read_text(encoding="utf-8"))
                    if ln] if path.exists() else []

        def diff(name, live):
            """The rows on lines only before the write, and only after it."""
            old, now = lines(level / name), lines(live)
            only = lambda a, b: _parse_jsonl("\n".join(set(a) - set(b)), dict)[0]
            return only(old, now), only(now, old)

        gone, new = diff(NOTES_FILE, notes_path(store))
        # A version an edit kept is its row's old line under a new id: part
        # of the edit, not a row the write added, and not the editor's.
        kept = {_sans_id(r) for r in gone}
        wrote = [r for r in new if _sans_id(r) not in kept]
        before = {r.get("id") for r in gone}
        removed = sorted({r.get("id") for r in wrote} - before - {None})
        changed = sorted(before - {None})
        arcs_gone, arcs_new = diff(ARCS_FILE, arcs_path(store))
        arcs = sorted({r.get("id") for r in arcs_gone + arcs_new} - {None})
        touched = {r.get("id") for r in new} | before | set(arcs)
        out = {"removed": removed, "changed": changed, "arcs": arcs, "rows": wrote,
               "before": {r["id"]: r for r in gone if r.get("id")}}
        if note_id is not None and note_id not in touched:
            raise UndoRefused(f"the last write did not touch {note_id}: undo takes back "
                              f"only the last write, which was {undo_words(out)}, and "
                              f"never one before it. Correct {note_id} with supersede")
        if not mine(last, wrote):
            raise UndoRefused(f"the last write is not yours: it was {undo_words(out)}")
        if not apply:
            return out
        for name, live in ((NOTES_FILE, notes_path(store)), (ARCS_FILE, arcs_path(store)),
                           (SESSIONS_FILE, sess)):
            copy = level / name
            if copy.exists():
                _replace_atomically(live, copy.read_text(encoding="utf-8"))
            elif live.exists():
                live.unlink()
        shutil.rmtree(level, ignore_errors=True)
        return out


def undo_words(u: dict) -> str:
    """`the add of 20261013-…-abc by claude`: what the last write was, so a
    refusal says what the caller would have taken back."""
    by = sorted({r.get("author") or "unknown" for r in u["rows"]})
    bits = ([f"the add of {', '.join(u['removed'])}"] if u["removed"] else []) + \
           ([f"the edit of {', '.join(u['changed'])}"] if u["changed"] else []) + \
           ([f"the change to arc {', '.join(u['arcs'])}"] if u["arcs"] else [])
    return (" and ".join(bits) or "a write") + (f" by {', '.join(by)}" if by else "")


def ensure_store(store) -> bool:
    """Create the store dir + both empty files + docs/ + `git init` if absent.
    Idempotent. Returns True iff it had to bootstrap anything. `init` is the
    one caller that may create; writes go through require_store. docs/ holds
    the files `doc:NAME` names (the user, 2026-10-15: part of every store);
    git keeps no empty dir, so a clone has it from its first doc."""
    store = Path(store)
    created = not store.exists()
    store.mkdir(parents=True, exist_ok=True)
    for f in (notes_path(store), arcs_path(store)):
        if not f.exists():
            f.touch()
            created = True
    if not (store / "docs").exists():
        (store / "docs").mkdir()
        created = True
    if not (store / ".git").exists():
        r = subprocess.run(["git", "init", "-q", str(store)], capture_output=True, text=True,
                           encoding="utf-8")
        if r.returncode:
            # git can fail after making `.git`, and a left `.git` makes every
            # later call skip this init: remove the one this call made.
            shutil.rmtree(store / ".git", ignore_errors=True)
            said = (r.stderr + r.stdout).strip() or f"exit {r.returncode}, no output"
            raise RuntimeError(f"git init failed in {store}; git said:\n{said}")
        created = True
    ignore = store / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCK_FILE}\n.tmp.*\n", encoding="utf-8", newline="\n")
        created = True
    return created


# ---- notes I/O ----
# file -> (text, kinds, notes, malformed, {line: row}), its last parse. A serve
# page parsed the store two or three times and `summary` twice, at 40 ms a parse
# on a large one (the user, 2026-10-09). The file is read every time and its parse
# kept only for an equal text, so no write, git checkout or hand edit can leave it
# stale. A text that changed parses only its new lines: a write's own read-back
# parsed the whole store again (audit 2026-11-02).
# ponytail: one entry per store path a process reads, each two copies of the file.
_PARSED: dict = {}


def _parse_jsonl(text: str, parse, known=None, fresh=None) -> tuple[list, list]:
    """(parse(row) for each line, [(lineno, raw, error)]). The error travels
    with the row so a reader that skips it can still say WHY: an un-migrated
    or hand-edited store must not read as an empty one. A second line with
    an id already read is skipped too: two copies of a store that edit one
    row, merged keeping both sides, give two heads one id (2026-10-13).
    `known`, line -> row from a last parse, skips each line's parse that it
    holds; `fresh` is filled with this text's."""
    good, bad, seen = [], [], {}
    for i, raw in enumerate(jsonl_lines(text), 1):
        if not raw.strip():
            continue
        row = known.get(raw) if known else None
        if row is None:
            try:
                row = parse(json.loads(raw))
            except Exception as e:              # malformed hand-edit can't take down a read
                bad.append((i, raw, f"missing key {e}" if isinstance(e, KeyError) else str(e)))
                continue
        if fresh is not None:
            fresh[raw] = row
        rid = getattr(row, "id", None)          # a Note or an Arc; undo parses dicts
        if rid is not None and rid in seen:
            bad.append((i, raw, f"duplicate id {rid}: line {seen[rid]} has it too, so "
                                f"this line is skipped. A merge kept both sides of two edits: "
                                f"change the last 3 characters of one line's id to other hex "
                                f"digits, and both entries read"))
            continue
        seen[rid] = i
        good.append(row)
    return good, bad


def _read_utf8(file: Path) -> str:
    """A store file's text. A byte that is not UTF-8 (a hand edit saved as
    Latin-1) stopped every command naming no file: name the line."""
    try:
        return file.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        line = e.object[:e.start].count(b"\n") + 1
        raise ValueError(f"{file} line {line} is not UTF-8 (byte {e.object[e.start]:#04x}); "
                         f"every command reads this file: save that line as UTF-8") from None


def _read_notes(file: Path, kinds):
    """(notes, [(lineno, raw, error)]). Each call gets its own lists:
    callers append."""
    file = Path(file)
    if not file.exists():
        return [], []
    text = _read_utf8(file)
    hit = _PARSED.get(file)
    if hit and hit[0] == text and hit[1] == kinds:
        return list(hit[2]), list(hit[3])
    fresh: dict = {}
    good, bad = _parse_jsonl(text, lambda d: note_from_dict(d, kinds=kinds),
                             hit[4] if hit and hit[1] == kinds else None, fresh)
    # A revision is appended after the row it revises, so one pass in file
    # order finds each chain's start; a predecessor not yet read leaves the
    # revision's own time.
    first = {}
    for i, n in enumerate(good):
        if n.supersedes in first:
            good[i] = n = replace(n, first_written=first[n.supersedes])
        first[n.id] = n.first_written or n.created_at
    _PARSED[file] = (text, kinds, good, bad, fresh)
    return list(good), list(bad)


def load(store):
    """All notes, in file order (oldest first); `load_malformed` lists the
    lines it skips. It takes no lock: a writer loads under its own. The kinds
    table is read from the store's own symbion.toml on every load: it is one
    small file, and reading it here is what lets every consumer take bits off
    the row instead of carrying a table around."""
    return _read_notes(notes_path(store), K.read_kinds(store))[0]


def load_malformed(store):
    """[(lineno, raw, error)] for lines that failed to read — for a surfaced warning."""
    return _read_notes(notes_path(store), K.read_kinds(store))[1]


def _append_line_unlocked(file, line: str) -> None:
    # Bytes, so LF on every platform. A last line with no newline (ENOSPC, a
    # killed write, an editor that drops it) is ended first: the row was
    # written onto it, and neither read back (2026-10-09).
    with open(file, "ab+") as f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write((line + "\n").encode("utf-8"))


def _append_note_unlocked(store, note: Note) -> None:
    # The session first: a record of a row that never landed is dropped by
    # the next commit, but a row whose record failed was written as no
    # session's, and its session's `commit` left it (2026-11-02).
    _record_session(store, note.id)
    _append_line_unlocked(notes_path(store), _dumps(note_to_dict(note)))


def add_many(store, rows, target_types=None, canonicalize=None, refuse_twins=False) -> list:
    """Append several notes as one write. Mints id/created_at/author/body/
    status defaults per row. Every row is built and validated before any is
    appended, so a bad row 30 of 45 leaves nothing behind to supersede.

    `target_types`, forwarded to `note_from_dict`, validates only when a set
    is supplied -- so a caller outside the CLI (whose argparse `choices=`
    would otherwise be the only gate) still gets real validation. Same
    reasoning for the `--status` rejection below: enforced here, in the
    store, not in argparse, so a direct API caller gets it too.

    `canonicalize(rows) -> rows`, when given, runs INSIDE the lock over every
    row before any is appended: the name rule sees the store as it is at
    write time, and a raise there (an ambiguous name) leaves nothing behind.
    The api owns that callback; the store only guarantees where it runs.
    `refuse_twins` raises `Twin`, under the same lock, for a row identical to
    an uncommitted head: the api's policy for a writer, as the callback is."""
    kinds = K.read_kinds(store)
    for fields in rows:
        spec = kinds.get(fields.get("kind"))
        if spec is None:
            raise ValueError(f"unknown kind {fields.get('kind')!r}; this store's kinds are "
                             f"{', '.join(kinds)} (declared under [kinds] in symbion.toml)")
        check_fields(fields["kind"], spec, fields)
    require_store(store)
    side = shared_side(store)                # before the first line: a refusal writes nothing
    with _lock(store):                       # outer: covers canonicalize + mint + write
        # ponytail: lock held across canonicalize = rows x command_timeout worst
        # case; a per-row release would let a neighbour mint between rows, so
        # the ceiling stays until it hurts
        if canonicalize is not None:
            rows = canonicalize(rows)
        existing = load(store)
        used = {n.id for n in existing}
        notes = []
        for fields in rows:
            fields = dict(fields)
            if fields.get("refs"):
                fields["refs"] = without_self_ref(fields["target"], fields["refs"])
            fields.setdefault("id", _mint_unique(used))
            fields.setdefault("created_at", _now_iso())
            fields.setdefault("author", "unknown")
            fields.setdefault("body", "")
            if side is not None:
                fields["side"] = side
            if fields.get("due") is not None:
                fields["due"] = canon_due(fields["due"])
            if "status" not in fields or fields["status"] is None:
                fields["status"] = default_status_for(kinds[fields["kind"]])
            notes.append(note_from_dict(fields, target_types=target_types, kinds=kinds))
        if refuse_twins:
            stored = {_as_written(n): n.id for n in heads(existing)}
            twins = [(i, stored[k]) for i, n in enumerate(notes)
                     if (k := _as_written(n)) in stored]
            if twins:                       # git only when a twin is possible
                pending = {r.get("id") for r in pending_rows(store)}
                if twins := [(i, nid) for i, nid in twins if nid in pending]:
                    raise Twin(twins)
        for note in notes:
            _append_note_unlocked(store, note)
    return notes


class Twin(ValueError):
    """An add whose every field equals an uncommitted head's. A command
    retried after its add ran stored the entry twice (2026-10-15), and
    `undo` reaches only the last write. A committed twin is a new run (the
    same check at a later date). `pairs` is (row index, stored id)."""
    def __init__(self, pairs):
        self.pairs = pairs
        super().__init__(f"identical to stored entry {pairs[0][1]}; nothing was written")


class NoBody(ValueError):
    """A write that would leave an entry with no body: `add`'s and an edit's
    refusal, in the CLI's words. The GUI catches it and says it in its own."""


def says_nothing(row) -> bool:
    """A row with no body that is neither a run's record nor an arc's box.
    A stray bodiless add fell out of undo's reach at the next write
    (2026-10-31), and `--body "$X"` with X unset emptied an entry at exit 0
    (2026-11-11). Every bodiless head in the field was a run or a box."""
    return not (row.get("body") or "").strip() and not any(
        row.get(k) for k in ("checked", "result", "measurements", "arc_id"))


def _as_written(note) -> str:
    """A row's fields as its writer gave them: no id, no times. An external
    stamp's time is when it was written, not what was run."""
    d = note_to_dict(note)
    for k in ("id", "created_at", "supersedes"):
        del d[k]
    if d["provenance"]:
        d["provenance"] = {k: v for k, v in d["provenance"].items() if k != "at"}
    return json.dumps(d, sort_keys=True)


def add(store, target_types=None, **fields) -> Note:
    """Append one note: the one-row case of `add_many`."""
    return add_many(store, [fields], target_types=target_types)[0]


def appended(body: str | None, more: str) -> str:
    """`body` verbatim, a blank line, then `more`. The prior text stays a
    byte-identical prefix -- what an amended pre-registration needs, and what
    rebuild-and-replace left to the author (list --json | jq, a temp file,
    --body-file) -- so only the newlines the paragraph break lacks are added,
    and none is stripped."""
    if not body:
        return more
    have = len(body) - len(body.rstrip("\n"))
    return body + "\n" * max(0, 2 - have) + more


def unholdable(note: Note) -> list[str]:
    """The fields `note` carries that its kind's bits cannot hold: a legacy
    row's (a migration, or a kind whose bits changed). The next supersede drops
    them from the new row."""
    return [k for k, bit in (("checked", note.spec.verdict), ("result", note.spec.verdict),
                             ("status", note.spec.status), ("due", note.spec.status),
                             ("options", note.kind == "question"))
            if not bit and getattr(note, k) is not None]


def chain_tip(notes, note_id: str) -> Note:
    """The live head of note_id's supersede chain. Pure: no I/O, no write.
    KeyError(note_id) when no row has that id. A cyclic chain (a hand edit,
    or two unvalidated add() calls) raises ValueError: the walk would
    otherwise spin forever, under the store-wide lock in a supersede."""
    return chain_tips(notes)(note_id)


def chain_tips(notes):
    """`chain_tip` over one map, for many ids: in a loop, `chain_tip` rebuilt
    its map per call, and `list` over a large store spent half its time there
    (audit 2026-11-02). Only the ids asked for are walked, so a cycle elsewhere
    raises nothing."""
    by_supersedes = {n.supersedes: n for n in notes if n.supersedes}
    first: dict = {}
    for n in notes:
        first.setdefault(n.id, n)

    def tip(note_id: str) -> Note:
        cur = first.get(note_id)
        if cur is None:
            raise KeyError(note_id)
        seen = {cur.id}
        while cur.id in by_supersedes:
            cur = by_supersedes[cur.id]
            if cur.id in seen:
                raise ValueError(f"cycle in supersede chain at {cur.id}")
            seen.add(cur.id)
        return cur
    return tip


def _supersede_unlocked(store, notes, old_id: str, author: str | None = None,
                        kinds=None, append_body: str | None = None, add_refs=None,
                        rm_refs=None, prepend_body: str | None = None, replace_body=None,
                        add_measurements=None, *, side, pending=None, **fields) -> Note:
    """The guts of `supersede`, assuming the caller already holds `_lock` and
    passes in a `notes` list loaded under that same lock. Extracted so a
    multi-row aggregate (`rename_target`, `apply_reconciliation`) can hold
    ONE outer lock spanning its whole sweep instead of nesting a fresh lock
    per row -- flock is per open-file-description, so a second LOCK_EX from
    this same process would deadlock, and reloading the whole notes file once
    per affected row is O(n^2) on a sweep of n.

    Inherits the superseded row's fields (notably `target` — never
    re-resolved, so a departed target stays editable), overridden by
    **fields, with a fresh id/created_at and supersedes=old_id. A `target`
    in **fields moves the entry; the caller resolves it. The old row
    stays on disk (the timeline is the history).

    `author` is NEVER inherited from the superseded row: a correction is
    authored by whoever wrote the correction, not by the person being
    corrected. A caller that supplies nothing gets "unknown" -- an
    obviously-absent attribution beats a plausible false one.

    `kinds` lets a sweep read the table once and pass it; a single-row caller
    may omit it. `side` is `shared_side`'s, read once by the caller before its
    lock: a sweep that asked per row ran one `git config` per row. A sweep's
    `pending` list takes the edit in place of the write, and the sweep
    writes them all with one `_rewrite_notes_unlocked`.

    A caller that will look up further ids in the same transaction must
    append the returned Note to its `notes` list itself -- this function
    only swaps the old version in that list for its kept copy.

    `append_body` adds to the TIP's body, read here under the caller's lock,
    so a revision that landed after the caller read the row is kept.
    `prepend_body` goes above it, the same way, as a new lead; a
    pre-registration refuses it, since its registered text stays on top.
    `replace_body`, (old, new) pairs, swaps each old span of it, in order,
    and refuses one that does not occur exactly once: changing one phrase
    took a read, a script and a whole new body (2026-11-04)."""
    sources = [s for s in (append_body, prepend_body, replace_body) if s is not None]
    if len(sources) > 1 or (sources and "body" in fields):
        raise ValueError("body, append_body, prepend_body and replace_body are sources "
                         "for one field")
    for added in (append_body, prepend_body):
        if added is None:
            continue
        if not added.strip():
            raise ValueError("nothing to add: the text is empty")
    # Fast-forward past any supersede that already landed while this call
    # waited for the lock. Without this, two racing supersede(old_id) calls
    # both build on the literal old_id and fork the chain; walking to the
    # live tip makes the second call chain onto the first's result instead,
    # so the lock actually serializes the mutation, not just the write.
    old = chain_tip(notes, old_id)
    kinds = K.read_kinds(store) if kinds is None else kinds
    base = note_to_dict(old)
    base.update(fields)
    if append_body is not None:
        base["body"] = appended(old.body, append_body)
    if prepend_body is not None:
        base["body"] = appended(prepend_body, old.body) if old.body else prepend_body
    if replace_body is not None:
        base["body"] = _replaced(old, replace_body)
    # A row bodiless already, from before the rule, may stay so.
    if (old.body or "").strip() and says_nothing(base):
        raise NoBody(f"no body: this edit would leave {old.id} without one; only a run's "
                     f"record (--checked, --result, --measure) or an arc's box (--arc-id) "
                     f"may go without one")
    if add_refs or rm_refs:
        # Onto the TIP's refs, as append_body is onto its body; the
        # inherited ones are never re-resolved.
        have = list(base.get("refs") or ())
        if absent := [r for r in rm_refs or () if r not in have]:
            label = lambda r: f"{r['type']}:{r.get('name') or ''}"     # noqa: E731
            raise ValueError(f"--rm-ref {', '.join(map(label, absent))}: not one of its refs "
                             f"({', '.join(map(label, have)) or 'it has none'}); "
                             f"nothing was written")
        have = [r for r in have if r not in (rm_refs or ())]
        base["refs"] = have + [r for r in add_refs or () if r not in have]
    if "refs" in fields or add_refs or "target" in fields:
        base["refs"] = without_self_ref(base["target"], base["refs"])
    if add_measurements:
        # Onto the TIP's too: a name given replaces that name only.
        base["measurements"] = {**(base.get("measurements") or {}), **add_measurements}
    if fields.get("due") is not None:
        base["due"] = canon_due(fields["due"])
    spec = kinds.get(base["kind"])
    if spec is None:
        raise ValueError(f"unknown kind {base['kind']!r}; this store's kinds are "
                         f"{', '.join(kinds)} (declared under [kinds] in symbion.toml)")
    # Parked may change, so a task parks as an idea in place. A task moved to
    # a pre-registration had no registered run and no stamp (2026-10-14), so
    # the verdict bit stays, and on a verdict kind the status bit too. Off
    # verdict kinds the status bit may change (the user, 2026-11-05): gaining it
    # opens the entry, and losing it drops status and due, which the kept
    # version holds and the CLI names.
    if base["kind"] != old.kind:
        if spec.verdict != old.spec.verdict or \
                (spec.verdict and spec.status != old.spec.status):
            raise ValueError(f"{old.kind} to {base['kind']}: a kind change keeps the verdict "
                             f"bit, and on a verdict kind the status bit, and these differ "
                             f"(`symbion schema` lists each kind's bits); a different thing "
                             f"is a new entry")
        if spec.status and not old.spec.status and base.get("status") is None:
            base["status"] = "open"
        if old.spec.status and not spec.status:
            for key in ("status", "due"):
                if key not in fields:
                    base[key] = None
    if spec.status and spec.verdict:
        if prepend_body is not None:
            raise ValueError(f"{old.id} is a pre-registration: its registered text stays on "
                             f"top, so nothing goes above it (--append adds after it)")
        # A body that keeps the registered text as its prefix is an append
        # (the GUI's dialog sends one whole); any other one replaced it at
        # exit 0, under a line still reading `registered at` (2026-10-13).
        body = base.get("body") or ""
        if not body.startswith(old.body or "") and \
                not body.startswith(_committed_floor(store, notes, old)):
            # An uncommitted one read as editable, since all of it was added
            # since the last commit, and undo went unnamed (dogfood, 2026-10-31).
            raise ValueError(f"{old.id} is a pre-registration: its registered text and each "
                             f"committed amendment stay as written, so a body may change only "
                             f"text added since the last `symbion commit` (--append adds "
                             f"after it)" + _undo_anew(store, notes, old))
        if base.get("checked") != old.checked:
            raise ValueError(f"{old.id} is a pre-registration: --checked names its registered "
                             f"run and stays as written; a different run is a new entry"
                             + _undo_anew(store, notes, old))
    # A legacy row (a hand-written migration, or a kind whose bits changed)
    # can carry a field its kind cannot hold. The reader loads it as written;
    # the WRITER owns the invariants, so an INHERITED illegal field is dropped
    # from the new row rather than refused with a message naming flags the
    # caller never passed (measured 2026-09-22: a `checked` on a `note` blocked
    # every later supersede). The same field passed in `fields` still refuses,
    # and so does a KIND CHANGE that would shed any field but status and due:
    # that is not a legacy row, it is the caller moving a row somewhere its
    # fields do not fit.
    dropped = [k for k in unholdable(old) if k not in fields] if base["kind"] == old.kind else []
    for key in dropped:
        base[key] = None
    for key in ("checked", "result"):          # rule 5, on a legacy row
        if key not in fields and base.get(key) in NULL_WORDS:
            base[key] = None
    check_fields(base["kind"], spec, base)
    # The head keeps the id, and the version it replaces moves to a new one
    # (the user, 2026-10-11): a row went through three ids in one session,
    # and the cites, commit messages and chat that named it went stale.
    # Across many stores, over half the cites named a version since
    # replaced, and a sample of them meant the row, not the version. The
    # kept version keeps its line, time and author, so the chain readers
    # walk is whole, and its new id carries its own time.
    kept = replace(old, id=_kept_id(old.created_at))
    base["supersedes"] = kept.id
    base["created_at"] = _now_iso()
    # Never inherited: a correction is authored by whoever wrote the
    # correction, not by the person being corrected. The row gets a fresh
    # timestamp; a stale author beside it is incoherent.
    base["author"] = author or "unknown"
    if side is not None:                    # the edit is the editor's
        base["side"] = side
    note = note_from_dict(base, kinds=kinds)
    if pending is None:
        _rewrite_notes_unlocked(store, [(note, kept.id)])
    else:
        pending.append((note, kept.id))
    notes[notes.index(old)] = kept      # a sweep reads this list for its next row
    return note


def _replaced(old: Note, pairs) -> str:
    """`old`'s body with each (was, now) swapped in turn. `was` must occur
    once: find twice, so an overlapping second match counts too."""
    body, tail = old.body or "", old.id[-10:]
    for was, now in pairs:
        first = body.find(was) if was else -1
        if first >= 0 and body.find(was, first + 1) < 0:
            body = body[:first] + now + body[first + len(was):]
            continue
        shown = was if len(was) <= 60 else was[:57] + "..."
        held = [f for f in ("checked", "result") if was and was in (getattr(old, f) or "")]
        if first >= 0:
            why = (f"occurs more than once in {tail}'s body, and a replace swaps text that "
                   f"occurs exactly once: give more of it")
        elif held:      # agents retried blind when the text sat in one (dogfood, 2026-11-10)
            why = (f"is in {tail}'s {held[0]}, not its body: `--{held[0]} TEXT` rewrites "
                   f"that field whole")
        elif was:
            why = f"is not in {tail}'s body: `symbion show {tail}` prints it as it is now"
        else:
            why = "is empty: give the text to swap"
        raise ValueError(f"replace: {shown!r} {why}; nothing was written")
    return body


def supersede(store, old_id: str, author: str | None = None, canonicalize=None,
              append_body: str | None = None, add_refs=None, rm_refs=None,
              prepend_body: str | None = None, add_measurements=None, target_types=None,
              replace_body=None, **fields) -> Note:
    """Append a correction superseding old_id -- the single-row public entry
    point. One outer `_lock` spans load -> fast-forward -> mint -> write; see
    `_supersede_unlocked` for what happens inside it.

    The load is INSIDE the lock. The reference loads outside it, so two
    concurrent calls read the same head and both append, forking the chain.

    If old_id's chain is ALREADY forked on disk (two notes both supersede the
    same id — e.g. a hand-edited notes.jsonl, or racing supersede calls from
    before this fast-forward existed), the walk picks whichever fork arm the
    last-in-file-order note started and extends only that one; the other
    arm's head is left untouched and still shows up in heads(). Healing
    pre-existing forks is out of scope here — this function only guarantees
    it never CREATES a new one.

    `canonicalize(fields) -> fields` runs inside the lock before the row is
    built: a new target or new refs resolve against the store as it is now;
    an inherited target or ref is never in `fields`. `target_types`,
    when given, checks the names this write brings: a new target or ref,
    never an inherited one. `add_refs` and `rm_refs` resolve the same way,
    so another spelling of a ref the tip holds matches it."""
    require_store(store)
    side = shared_side(store)
    with _lock(store):
        notes = load(store)
        if canonicalize is not None:
            fields = canonicalize(fields)
            if add_refs:
                add_refs = canonicalize({"refs": add_refs})["refs"]
            if rm_refs:
                rm_refs = canonicalize({"refs": rm_refs})["refs"]
        check_types([o for o in (fields.get("target"), *(fields.get("refs") or ()),
                                 *(add_refs or ())) if o], target_types)
        return _supersede_unlocked(store, notes, old_id, author=author, side=side,
                                   append_body=append_body, add_refs=add_refs, rm_refs=rm_refs,
                                   prepend_body=prepend_body, replace_body=replace_body,
                                   add_measurements=add_measurements, **fields)


def _committed_floor(store, notes, tip: Note) -> str:
    """What an edit of pre-registration `tip` keeps as its body's prefix:
    the body git last committed under its id (the head keeps the id through
    every edit), else the registered text, its chain's first body. An
    amendment git has not seen is a draft: a placeholder left in one took a
    second append to patch (2026-10-15). The tip's whole body when git
    cannot say."""
    committed = _committed_lines(store)
    if committed is None:
        return tip.body or ""
    for raw in committed:
        if tip.id in raw and _line_id(raw) == tip.id:
            return json.loads(raw).get("body") or ""
    by_id, root = {n.id: n for n in notes}, tip
    for _ in notes:                     # a cycle ends the walk
        if root.supersedes not in by_id:
            break
        root = by_id[root.supersedes]
    return root.body or ""


def _never_committed(store, notes, tip: Note) -> bool:
    """No version of `tip`'s chain is in git; False when git cannot say."""
    committed = _committed_lines(store)
    if committed is None:
        return False
    ids, by_id, n = set(), {x.id: x for x in notes}, tip
    while n is not None and n.id not in ids:      # a cycle ends the walk
        ids.add(n.id)
        n = by_id.get(n.supersedes)
    return not any(_line_id(raw) in ids for raw in committed)


def _undo_anew(store, notes, old: Note) -> str:
    """A refused edit's way back for a pre-registration none of which is
    committed. Any later write or commit ends the undo, not only another
    writer's (dogfood, 2026-11-09)."""
    if not _never_committed(store, notes, old):
        return ""
    return (f". None of it is committed: `symbion undo {old.id[-10:]}` takes back its "
            f"last write if no other write or commit came since, then add it anew")


def _committed_lines(store) -> set | None:
    """The lines of notes.jsonl in the store's last commit: none before the
    first one. None when git cannot say."""
    git = ["git", "-C", str(store)]
    if subprocess.run([*git, "rev-parse", "-q", "--verify", "HEAD"],
                      capture_output=True).returncode:
        return set()
    r = subprocess.run([*git, "show", f"HEAD:{NOTES_FILE}"], capture_output=True, text=True,
                       encoding="utf-8")
    return set(jsonl_lines(r.stdout)) if r.returncode == 0 else None


def _line_id(raw: str):
    try:
        return json.loads(raw).get("id")
    except (ValueError, AttributeError):
        return None


# ---- which session wrote a pending row ----
# Every Claude session writes as `claude`, so an author cannot tell one
# session's pending rows from another's, and `commit` took a live session's
# rows under another's message four times (2026-10-06 to 10-10). The runtime's
# session key can. The record lives in STATE_DIR, so no commit takes it, and
# a commit forgets the rows it took.
SESSIONS_FILE = "symbion-sessions"
# The store's local state: the sessions record and the undo level. In the
# working tree, not the git dir: agy's sandbox keeps every workspace's `.git/`
# read-only, and a write there raised after notes.jsonl was written (2026-11-01).
# Its own `.gitignore` of `*` keeps it out of `commit`'s `git add -A` with no
# change to a store's tracked .gitignore. Before 0.13.1 both lived in the git
# dir, under the same names; the first write moves them (_state_dir).
STATE_DIR = ".symbion-state"


def _state_dir(store, make: bool = False) -> Path:
    """The store's STATE_DIR. `make`, for a write: create it, with what an
    older symbion kept in the git dir moved in. It is built aside, under the
    store's `.tmp.*` ignore, and renamed whole, so a half-made one is never
    seen. A legacy file that cannot be removed (a read-only `.git/`) is
    left: once the dir exists, nothing reads it."""
    d = Path(store).absolute() / STATE_DIR
    if not make or d.is_dir():
        return d
    legacy = _legacy_sessions_path(store)
    old_undo = legacy.with_name(UNDO_DIR) if legacy is not None else None
    tmp = d.with_name(f".tmp.{os.getpid()}.{STATE_DIR}")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir()
        (tmp / ".gitignore").write_text("*\n", encoding="utf-8", newline="\n")
        if legacy is not None and legacy.exists():
            shutil.copyfile(legacy, tmp / SESSIONS_FILE)
        if old_undo is not None and old_undo.is_dir():
            with suppress(OSError):      # an undo level is worth less than the write
                shutil.copytree(old_undo, tmp / UNDO_DIR)
        os.replace(tmp, d)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        if not d.is_dir():                          # another process made it first: fine
            raise
        return d
    if legacy is not None:
        with suppress(OSError):
            legacy.unlink()
    if old_undo is not None:
        shutil.rmtree(old_undo, ignore_errors=True)
    return d


def session_key() -> str | None:
    """The agent session this process runs in, when its runtime names one.
    Codex, Hermes, Antigravity and Pi before Claude Code: one launched from
    Claude Code inherits its key, and the innermost agent is the one writing (as
    in api.author_default). A Claude Code launched from Pi inherits PI_SESSION_ID,
    so it counts only while AI_AGENT names Pi. Under Hermes, the plugin's
    SYMBION_HERMES_SESSION names the root session, so a delegate_task child's
    writes are its parent's; with neither Hermes variable there is no key, never
    an outer agent's."""
    if os.environ.get("CODEX_THREAD_ID"):
        return os.environ["CODEX_THREAD_ID"]
    if os.environ.get("HERMES_AGENT"):
        return os.environ.get("SYMBION_HERMES_SESSION") or os.environ.get("HERMES_SESSION_ID") or None
    pi = os.environ.get("PI_SESSION_ID") if os.environ.get("AI_AGENT") == "pi" else None
    return (os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
            or pi or os.environ.get("CLAUDE_CODE_SESSION_ID") or None)


_SESSIONS_PATHS: dict = {}


def _legacy_sessions_path(store) -> Path | None:
    """Where symbion before 0.13.1 kept the sessions record: the git dir.
    Asked once per store: per row, it was most of a batch write's time under
    the lock. A miss is not kept: the store may be a repo by the next."""
    store = Path(store).absolute()
    if (p := _SESSIONS_PATHS.get(store)) is None:
        r = subprocess.run(["git", "-C", str(store), "rev-parse", "--git-path", SESSIONS_FILE],
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0:
            p = _SESSIONS_PATHS[store] = Path(store, r.stdout.strip())
    return p


def _sessions_path(store) -> Path | None:
    """The sessions record to read: the legacy one until STATE_DIR exists."""
    d = _state_dir(store)
    return d / SESSIONS_FILE if d.is_dir() else _legacy_sessions_path(store)


def _record_session(store, note_id: str, kept_id: str | None = None,
                    prior: str | None = None, took: str | None = None) -> list[str]:
    """`note_id` is this session's now. An edit's `kept_id` stays with the
    session that wrote the version it holds (`prior`, or `took` when a commit
    took that version), and an edit outside any session takes the row from
    the session that had it. Returns the lines written, so a sweep keeps the
    record without reading it again."""
    key = session_key()
    kept = [f"{kept_id}\t{prior}"] if prior else [f"{kept_id}\t{took}\tc"] if took else []
    lines = (kept if kept_id else []) + \
        ([f"{note_id}\t{key or ''}"] if key or prior or took else [])
    path = lines and _state_dir(store, make=True) / SESSIONS_FILE
    for line in lines if path else []:
        _append_line_unlocked(path, line)
    return lines


def sessions(store, committed: bool = False) -> dict:
    """{row id: session key} for the pending rows an agent session wrote;
    with `committed`, also for the rows a commit took in the last week."""
    path = _sessions_path(store)
    try:
        text = path.read_text(encoding="utf-8") if path else ""
    except FileNotFoundError:
        return {}
    known = dict(ln.split("\t", 1) for ln in text.splitlines() if "\t" in ln)
    # A third field marks a row a commit took (_forget_committed).
    return {i: k.split("\t")[0] for i, k in known.items() if k and (committed or "\t" not in k)}


def own_rows(store, author: str):
    """keep() for `commit`: the caller's own pending rows. In an agent
    session, the rows it wrote, as whoever (a row in the user's words is written as the
    person); with no session, the author's rows that no session wrote."""
    known, key = sessions(store), session_key()
    if key:
        return lambda r: known.get(r.get("id")) == key
    return lambda r: r.get("author") == author and r.get("id") not in known


def _forget_committed(store) -> None:
    """Keep the session of each row still pending, and for a week that of
    each row a commit took, marked: the news of a write reads it, so a row a
    session wrote in the user's words is not someone else's after another
    process commits it (2026-11-11). A version an edit kept keeps its mark."""
    known, pending = sessions(store, committed=True), sessions(store)
    committed = _committed_lines(store) if known else None
    if committed is None:
        return
    live = {_line_id(ln) for ln in jsonl_lines(notes_path(store).read_text(encoding="utf-8"))
            if ln and ln not in committed}
    week = (_utcnow() - timedelta(days=7)).strftime("%Y%m%d-%H%M%S")
    _replace_atomically(_state_dir(store, make=True) / SESSIONS_FILE, "".join(
        f"{i}\t{k}\n" if i in live and i in pending else f"{i}\t{k}\tc\n"
        for i, k in known.items()
        if (i in live and i in pending) or (i[:8].isdigit() and i[:15] >= week)))


def _rewrite_notes_unlocked(store, edits) -> None:
    """For each (note, kept_id), in order: move the line holding note.id to
    `kept_id`, as written, and append the note. Whole-file and atomically: a
    reader without the lock sees the old file or the new one, never half,
    and no crash leaves an id on neither line. A sweep passes all its rows
    at once: a write a row read, parsed and wrote the file each time, and
    two 1,500-row renames took 149 s under the lock (audit 2026-11-02)."""
    path = notes_path(store)
    raws = jsonl_lines(path.read_text(encoding="utf-8"))
    where: dict = {}
    for i, raw in enumerate(raws):
        where.setdefault(_line_id(raw), []).append(i)
    for note, _ in edits:
        at = [str(i + 1) for i in where.get(note.id, ())]
        if len(at) > 1:             # a merge kept both sides (_parse_jsonl)
            raise ValueError(f"{note.id} is on lines {', '.join(at[:-1])} and {at[-1]} of "
                             f"{NOTES_FILE}, and this write would change each. Change the last "
                             f"3 characters of one line's id to other hex digits, then write "
                             f"again")
    # The records first, as in _append_note_unlocked.
    # ponytail: if the rewrite below then fails, each note.id's record names
    # this session while its line is still the prior version; restore the
    # records if a failed rewrite is ever seen.
    known, took = sessions(store), sessions(store, committed=True)
    for note, kept_id in edits:
        lines = _record_session(store, note.id, kept_id, known.get(note.id) or None,
                                took.get(note.id))
        known.update(ln.split("\t", 1) for ln in lines)
        for i in where.get(note.id, ()):
            raws[i] = _dumps({**json.loads(raws[i]), "id": kept_id})
        where[note.id] = [len(raws)]
        raws.append(_dumps(note_to_dict(note)))
    _replace_atomically(path, "\n".join(raws) + "\n")


def heads(notes):
    """Collapse supersede chains: a note is a head iff nothing supersedes it."""
    superseded = {n.supersedes for n in notes if n.supersedes}
    return [n for n in notes if n.id not in superseded]


def heads_for(store, target_type, target_name=None, loaded=None):
    """Current heads attached to one object, newest-first — the read-side hook
    for surfacing prior conclusions where the next investigation starts.
    heads() runs on the FULL row set before filtering, so a superseding row
    never hides from the chain collapse. `loaded` is the row set when the
    caller has already read it."""
    allheads = heads(load(store) if loaded is None else loaded)
    rows = query(allheads, target_type=target_type, target_name=target_name)
    if target_name is not None:                    # also surface notes that REF this object
        seen = {n.id for n in rows}
        rows = rows + [n for n in allheads if n.id not in seen
                       and any(r.type == target_type and r.name == target_name for r in n.refs)]
    return newest_first(rows)


def stored_names(notes, target_type) -> set:
    """Every name `target_type` carries in these rows, as a target or a ref."""
    return ({n.target.name for n in notes if n.target.type == target_type}
            | {r.name for n in notes for r in n.refs if r.type == target_type})


def query(notes, *, id=None, target_type=None, target_name=None, kind=None,
          status=None, arc_id=None, tag=None, author=None, grep=None,
          overdue=None, since=None, due_by=None):
    """Filter raw notes by any combination of fields. Callers compose heads()
    when they want current state (see data-flow in the spec).

    `grep` is a compiled pattern searched over body, target name, checked,
    result, each ref as `type:name`, each tag as `#tag`, each measurement
    as `name=value` and each option -- the text a reader would otherwise pipe a paged list to
    grep for, losing the page's header and reading the silence as absence.
    Refs are in because SKILL.md tells writers to put a second object there
    instead of in prose. Tags are in because a writer tags a subject the text
    never names. Measurements are in because a wrong value read 0 entries
    and was found only by accident (2026-10-14).

    `id` is an exact match on the row, not a walk of its supersede chain: it
    filters whatever set it is handed, so pass raw notes to reach a superseded
    row and heads() to reach only live ones."""
    out = notes
    if id is not None:
        out = [n for n in out if n.id == id]
    if target_type is not None:
        # A tuple is several types: `list --type file` in a store with [repos].
        types = (target_type,) if isinstance(target_type, str) else target_type
        out = [n for n in out if n.target.type in types]
    if target_name is not None:
        out = [n for n in out if n.target.name == target_name]
    if kind is not None:
        # A tuple is several kinds: `list --kind task,bug`.
        kinds = (kind,) if isinstance(kind, str) else kind
        out = [n for n in out if n.kind in kinds]
    if status is not None:
        out = [n for n in out if read_status(n) == status]
    if arc_id is not None:
        out = [n for n in out if n.arc_id == arc_id]
    if tag is not None:
        out = [n for n in out if tag in n.tags]
    if author is not None:
        out = [n for n in out if n.author == author]
    if grep is not None:
        out = [n for n in out if grep.search("\n".join(
            [f for f in (n.body, n.target.name, n.checked, n.result) if f]
            + [f"{r.type}:{r.name or ''}" for r in n.refs] + [f"#{t}" for t in n.tags]
            + [f"{k}={v}" for k, v in (n.measurements or {}).items()]
            + list(n.options or ())))]
    if overdue:
        out = [n for n in out if is_overdue(n)]
    if since is not None:
        out = [n for n in out if written_at(n) >= since]
    if due_by is not None:          # a date: due on or before it, past due included
        now = now_shown()
        left = (due_by - now.date()).days
        out = [n for n in out if (s := due_state(n.due, now) if n.due else None)
               and s[1] <= left]
    return out


def tag_counts(notes):
    """{tag: count} over heads — surfaces freeform-tag drift into near-synonyms
    (e.g. refuted vs retracted) so the vocabulary can be kept tight."""
    return dict(Counter(t for n in heads(notes) for t in n.tags))


# ---- name canonicalization ----
nfc = catalog.nfc  # was defined identically here and in catalog.py; catalog
                    # is the lower layer (store must not become a dependency
                    # of it), so store re-exports catalog's rather than
                    # keeping two copies to drift out of sync.


def _rename_unlocked(store, notes, kinds, target_type, old, new, author,
                     new_type=None, *, side, pending) -> tuple[int, int]:
    """Move every HEAD off (target_type, old): its target, and each ref naming
    it, onto (new_type, new), one supersede per row; `new_type` defaults to
    `target_type`. The other refs keep their place.
    Returns (re-targeted, re-pointed) row counts; a row carrying both counts in
    each. The caller holds the lock and passes `notes` loaded under it; the new
    rows are appended to that list, and their edits to `pending`, which the
    caller writes with `_rewrite_notes_unlocked`.

    Refs are half the name: `context --target` reads them, so a sweep over
    targets alone left every `--ref` on a name nothing else carried."""
    was, now = Target(target_type, old), Target(new_type or target_type, new)
    moved = repointed = 0
    for note in heads(notes):
        if note.target != was and was not in note.refs:
            continue        # the rename does not touch it, legacy self-edge or not
        target = now if note.target == was else note.target
        # dict.fromkeys: a row that already REF'd `new` keeps one entry. A ref
        # equal to the row's target is dropped: a legacy self-edge, or one
        # this rename would make (target old, ref new); either adds nothing
        # (without_self_ref).
        refs = tuple(r for r in dict.fromkeys(now if r == was else r for r in note.refs)
                     if r != target)
        fields = {}
        if target != note.target:
            fields["target"] = {"type": now.type, "name": new}
        if refs != note.refs:
            fields["refs"] = [{"type": r.type, "name": r.name} for r in refs]
        if fields:
            notes.append(_supersede_unlocked(store, notes, note.id, author=author,
                                             kinds=kinds, side=side, pending=pending, **fields))
            moved += "target" in fields
            repointed += was in note.refs
    return moved, repointed


def rename_target(store, target_type, old, new, author=None,
                  canonicalize=None, new_type=None, target_types=None) -> tuple[int, int]:
    """Re-target every HEAD note for (target_type, old) onto (new_type or
    target_type, new), and re-point every ref to it, via supersede so the old rows remain as
    history. Projects rename catalog objects; notes key on the name string, so a rename orphans them without
    this. Returns (re-targeted, re-pointed) -- (0, 0) means the old name
    matched nothing, which the CLI treats as a user error, not a no-op
    success. A name only refs carry is a real object with nothing filed on it.

    `author` is the person who ran the rename, not whoever wrote the note
    being re-targeted -- forwarded to each internal supersede, never
    inherited from it.

    ONE outer `_lock` spans the whole sweep (load -> scan heads -> supersede
    every match), via `_supersede_unlocked` rather than the public
    `supersede` -- calling `supersede` here would acquire a second `LOCK_EX`
    from this same process and deadlock, since flock is per
    open-file-description, not per call. Without one lock spanning the whole
    sweep, two concurrent renames of the same name each take their own
    unlocked snapshot of the heads and each reports the full count, double-
    counting work that only happened once; with it, the second call's scan
    happens strictly after the first's lock is released and sees nothing
    left to rename. This also drops the reload-per-row that made the old
    per-note-locked version O(n^2) on a sweep of n.

    `canonicalize(new) -> new` runs inside the lock, as in `add_many`:
    without it `new` is stored as typed, and a short sha stays short.
    `old` never goes through a catalog: it names a departed object, which a
    live catalog match would redirect. A short commit `old` resolves against
    the stored shas (`_stored_commit`). `target_types`, when given, checks
    the type the rows move to."""
    check_types([{"type": new_type or target_type}], target_types)
    require_store(store)
    side = shared_side(store)
    with _lock(store):
        if canonicalize is not None:
            new = canonicalize(new)
        notes, pending = load(store), []
        if target_type == "commit":
            old = _stored_commit(notes, old)
        counts = _rename_unlocked(store, notes, K.read_kinds(store),
                                  target_type, old, new, author, new_type, side=side,
                                  pending=pending)
        if pending:
            _rewrite_notes_unlocked(store, pending)
        return counts


def _stored_commit(notes, old):
    """`old` as the one stored commit it begins: rows key on the full sha,
    and a short OLD re-targeted nothing after a history rewrite (2026-10-15).
    Stored names only, never git: the rewrite took the old commit away. A
    name stored as typed wins, as `rename SHORT SHORT` needs."""
    names = {o.name for n in heads(notes) for o in (n.target, *n.refs)
             if o.type == "commit" and o.name}
    if old in names or not re.fullmatch(r"[0-9a-fA-F]{4,39}", old):
        return old
    hits = sorted(x for x in names if x.lower().startswith(old.lower()))
    if len(hits) > 1:
        raise ValueError(f"commit:{old} begins {len(hits)} stored commits: "
                         f"{', '.join(hits[:5])}; give more of the sha")
    return hits[0] if hits else old


def retype(store, from_type, to_type, author=None, target_types=None) -> tuple[int, int]:
    """Move every HEAD's target and refs of `from_type` onto `to_type`, each
    name as stored, under one lock: how a store's rows leave a type it
    dropped (the repos design, "Converting a store"). Counts as
    `rename_target` does. No name is resolved: a per-name rename resolves
    its new name through the catalog, and moved a departed path onto a live
    one it is a unique substring of."""
    check_types([{"type": to_type}], target_types)
    require_store(store)
    side = shared_side(store)
    with _lock(store):
        notes, kinds, pending = load(store), K.read_kinds(store), []
        names = sorted({o.name for n in heads(notes) for o in (n.target, *n.refs)
                        if o.type == from_type and o.name is not None})
        moved = repointed = 0
        # ponytail: one sweep per name, so a row holding two names of the type
        # gets two versions; one supersede per row if a converted store's
        # history reads noisy. Each sweep sees the last: _rename_unlocked
        # appends what it writes to `notes`.
        for name in names:
            m, r = _rename_unlocked(store, notes, kinds, from_type, name, name, author, to_type,
                                    side=side, pending=pending)
            moved, repointed = moved + m, repointed + r
        if pending:
            _rewrite_notes_unlocked(store, pending)
        return moved, repointed


def _diff_rows(diff: str) -> dict[str, list[dict]]:
    """A notes.jsonl diff's rows by side, `+` and `-`. A line that does not
    parse is `{}`: a hand edit must not block a commit."""
    rows = {"+": [], "-": []}
    for line in jsonl_lines(diff):
        if line[:2] in ("+{", "-{"):
            try:
                row = json.loads(line[1:])
            except ValueError:
                row = None
            rows[line[0]].append(row if isinstance(row, dict) else {})
    return rows


def added_rows(diff: str) -> list[dict]:
    """The rows a notes.jsonl diff adds, an in-place rewrite included. A
    version an edit kept is not one: it is a removed line under a new id."""
    rows = _diff_rows(diff)
    kept = {_sans_id(r) for r in rows["-"]}
    return [r for r in rows["+"] if not r or _sans_id(r) not in kept]


def removed_rows(diff: str) -> list[dict]:
    """The lines a notes.jsonl diff removes that no added line keeps but for
    its id, as an edit keeps its old version: a committed version lost, by
    a hand edit or a repair. added_rows reads the other side only, so a dry
    run read `nothing to commit` over one (2026-11-02)."""
    rows = _diff_rows(diff)
    kept = {_sans_id(r) for r in rows["+"]}
    return [r for r in rows["-"] if not r or _sans_id(r) not in kept]


def _sans_id(row: dict) -> str:
    """A row's text but its id: a version an edit kept reads as its old line."""
    return json.dumps({k: v for k, v in row.items() if k != "id"}, sort_keys=True)


def entries(rows) -> list[dict]:
    """`rows` less each version another row in it supersedes: an entry
    edited twice before a commit, its first edit kept because another entry
    cites it, read as two entries with one lead (2026-10-14)."""
    later = {r.get("supersedes") for r in rows} - {None}
    return [r for r in rows if r.get("id") not in later]


def rows_by_author(rows) -> str:
    """`claude 2, codex 4`: the commit's `Rows:` trailer, by entry. An entry
    counts once for each author with a version of it in `rows`: a commit
    that takes another writer's base must name them (2026-10-02)."""
    newer = {r.get("supersedes"): r.get("id") for r in rows if r.get("supersedes")}

    def entry(i):
        seen = set()
        while i in newer and i not in seen:
            seen.add(i)
            i = newer[i]
        return i
    by = Counter(a for a, _ in {(r.get("author") or "?", entry(r.get("id")) or id(r))
                                for r in rows})
    return ", ".join(f"{a} {n}" for a, n in sorted(by.items()))


def arcs_by_writer(store, staged: bool = False) -> str:
    """`ada 1, unknown 1`: the commit's `Arcs:` trailer, each change to
    arcs.jsonl since HEAD by its writer, in the index (`staged`, what a
    commit took) or the work tree (what it would take). A new arc counts for
    its author, a moved box for its mover (`by`), and any other edit for its
    writer (`changed_by`); a change no stamp names, such as a hand edit,
    counts as `unknown`. A commit took the user's GUI move under a message
    that named only its rows (2026-11-06)."""
    def arcs(text):
        out = {}
        for ln in jsonl_lines(text):
            with suppress(ValueError):
                if isinstance(d := json.loads(ln), dict):
                    out[d.get("id")] = d
        return out

    def shown(rev):
        r = subprocess.run(["git", "-C", str(store), "show", rev], capture_output=True,
                           text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else ""

    def placed(a):
        return [p for p in a.get("placement") or () if isinstance(p, dict)]

    def rest(a):
        return {k: v for k, v in a.items() if k != "placement"}
    path = arcs_path(store)
    was = arcs(shown(f"HEAD:{ARCS_FILE}"))
    now = arcs(shown(f":{ARCS_FILE}") if staged else _read_utf8(path) if path.exists() else "")
    by = Counter(unknown=len(was.keys() - now.keys()))
    for i, a in now.items():
        if (w := was.get(i)) is None:
            by[a.get("author") or "unknown"] += 1
            continue
        seen = {(p.get("id"), p.get("by"), p.get("at")) for p in placed(w)}
        moves = [p["by"] for p in placed(a)
                 if p.get("by") and (p.get("id"), p["by"], p.get("at")) not in seen]
        by.update(moves)
        if rest(a) != rest(w) or (not moves and placed(a) != placed(w)):
            stamped = a.get("changed_at") not in (None, w.get("changed_at"))
            by[(a.get("changed_by") if stamped else None) or "unknown"] += 1
    return ", ".join(f"{a} {n}" for a, n in sorted(by.items()) if n)


def pending_rows(store) -> list[dict]:
    """What `commit` would take from notes.jsonl: the rows added or
    rewritten since HEAD, or every row before the first commit."""
    return added_rows(_notes_diff(store))


def lost_rows(store) -> list[dict]:
    """HEAD's lines gone from notes.jsonl that no pending row keeps
    (removed_rows): only `commit --all` takes their removal."""
    return removed_rows(_notes_diff(store))


def _notes_diff(store) -> str:
    """notes.jsonl's diff from HEAD, or every line added before the first
    commit. Plumbing: a porcelain `git diff` rewrites the index under
    index.lock when a file's stat data is stale, and a `symbion commit`'s
    `git add` beside it refused while the GUI polled this (2026-10-16)."""
    git = ["git", "-C", str(store)]
    if subprocess.run([*git, "rev-parse", "-q", "--verify", "HEAD"],
                      capture_output=True).returncode:
        f = Path(store) / NOTES_FILE
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        return "".join(f"+{ln}\n" for ln in jsonl_lines(text))
    return subprocess.run([*git, "diff-index", "-p", "-U0", "HEAD", "--", NOTES_FILE],
                          capture_output=True, text=True, encoding="utf-8").stdout


def commit(store, message: str, cfg, keep=None, files: bool = False,
           wrap: str | None = None, paths=()) -> bool:
    """git add -A + commit the store. Hermetic identity so it works in CI/tmp.
    The lock is here because `add -A` would otherwise stage whatever another
    writer has half-written. Returns True on a commit, False when nothing
    changed, and raises RuntimeError with git's words when git refuses: a
    hook's refusal (a secret scanner's catch) once read as 'nothing to
    commit', its output dropped (2026-09-28).

    `keep(row) -> bool`, when given, takes only the pending rows it accepts,
    and the registry; every other row stays pending (_stage_kept), and so
    does every other file unless `files`. `paths`, store-relative, are
    the docs a GUI commit's rows name: those no commit has taken and git
    does not ignore are staged too. `wrap` names who ends a session
    with this commit: a `Wrap:` line in the one trailer block (a second
    `-m` would put `Rows:` outside it), and summary reads the newest. A
    wrap commits even with nothing staged. In a shared store the commit is
    the clone's own (its git identity, so `git log` shows the side) and a wrap
    adds a `Side:` line after `Wrap:`."""
    store = Path(store)
    require_store(store)
    side = shared_side(store)          # a clone with no user.name is refused by name
    git = ["git", "-C", str(store)]
    with _lock(store, undoable=False):
        r = (subprocess.run([*git, "add", "-A"], capture_output=True, text=True, encoding="utf-8")
             if keep is None else _stage_kept(store, keep))
        if r.returncode == 0 and keep is not None and files:
            r = subprocess.run([*git, "add", "-A", "--", ".", f":(exclude){NOTES_FILE}"],
                               capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0 and keep is not None and not files:
            # A change someone staged by hand is a file this commit leaves: with
            # the index as found it went in, after the dry run had named it as
            # left (2026-11-09). An unmerged path stays, and git refuses it.
            # ponytail: the change stays on disk, unstaged; a version staged
            # and then edited again is no longer in the index.
            r = subprocess.run([*git, "diff", "--cached", "--name-only", "-z", "--no-renames",
                                "--diff-filter=u"], capture_output=True, text=True, encoding="utf-8")
            others = [p for p in r.stdout.split("\0") if p and p not in (NOTES_FILE, ARCS_FILE)]
            if r.returncode == 0 and others:
                r = subprocess.run([*git, "--literal-pathspecs", "reset", "-q", "--", *others],
                                   capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0 and paths:
            # Only a doc no commit has taken: an edit to a tracked one is its
            # writer's, and an ignored one makes `git add` refuse. Literal:
            # a name is typed text, and `[ab].md` is a glob.
            r = subprocess.run([*git, "--literal-pathspecs", "ls-files", "-z", "--others",
                                "--exclude-standard", "--", *paths],
                               capture_output=True, text=True, encoding="utf-8")
            new = [p for p in r.stdout.split("\0") if p]
            if r.returncode == 0 and new:
                r = subprocess.run([*git, "--literal-pathspecs", "add", "--", *new],
                                   capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0:
            r = subprocess.run([*git, "diff", "--cached", "--quiet"],
                               capture_output=True, text=True, encoding="utf-8")
            # A wrap commits with nothing staged: its message is the handoff,
            # and a session that changed only the repo had none (2026-10-13).
            empty = r.returncode == 0             # nothing staged
            if empty and not wrap:
                _forget_undo(store)      # what git holds already: undo would take it back
                return False
            if r.returncode in (0, 1):
                # A commit once named another agent's rows as its own work
                # (2026-10-02), and `--all` still takes them. The trailer
                # says whose they are.
                diff = subprocess.run([*git, "diff", "--cached", "-U0", "--", NOTES_FILE],
                                      capture_output=True, text=True, encoding="utf-8").stdout
                rows = rows_by_author(added_rows(diff))
                arcs = arcs_by_writer(store, staged=True)
                trailers = ([f"Rows: {rows}"] if rows else []) + \
                    ([f"Arcs: {arcs}"] if arcs else []) + ([f"Wrap: {wrap}"] if wrap else [])
                if wrap and side:
                    trailers.append(f"Side: {side}")
                # In UTC by the one clock, as the rows are: git's own read the
                # local zone, and ignored a stopped clock. A date the caller
                # set in git's own variables still wins.
                now = _now_iso()
                r = subprocess.run(
                    [*git, *([] if side else ["-c", f"user.name={cfg.git_name}",
                                              "-c", f"user.email={cfg.git_email}"]),
                     "commit", "-q",
                     *(["--allow-empty"] if empty else []), "-m", message,
                     *(["-m", "\n".join(trailers)] if trailers else [])],
                    capture_output=True, text=True, encoding="utf-8",
                    env={"GIT_AUTHOR_DATE": now, "GIT_COMMITTER_DATE": now, **os.environ})
                if r.returncode == 0:
                    _forget_committed(store)
                    _forget_undo(store)          # after a commit there is nothing to undo
    if r.returncode:
        said = (r.stderr + r.stdout).strip() or f"exit {r.returncode}, no output"
        raise RuntimeError(f"git refused the commit; the entries are on disk, "
                           f"not committed. git said:\n{said}")
    return True


def with_bases(rows, keep):
    """`keep`, plus the pending base of each revision it takes, as `commit`
    takes them (_stage_kept): a commit must not hold a revision of a row it
    lacks. A note and the dry run that read `keep` alone named another
    session's base as left, and the commit took it (2026-10-13)."""
    ids = {r.get("id") for r in rows} - {None}
    take = set()
    for r in reversed(rows):                # a base is written before its revision
        if r.get("id") and (keep(r) or r["id"] in take):
            take.add(r["id"])
            if r.get("supersedes") in ids:
                take.add(r["supersedes"])
    return lambda r: r.get("id") in take


class DiscardChanged(ValueError):
    pass


def discard_ids(plan: dict) -> frozenset:
    """What `expect` holds: the rows and arcs a discard plan takes away, and
    each version it gives an id back, whole, with that id. A write that changed
    which version takes the id back went through (audit, 2026-11-08)."""
    return frozenset({r.get("id") for r in plan["removed"]} | {a.get("id") for a in plan["arcs"]}
                     | {json.dumps(r, sort_keys=True) for r in plan["returned"]})


def discard(store, mine, author: str, apply: bool = True, expect=None) -> dict:
    """Throw away what a commit taking `mine` would take, and the arcs
    `author` made since the last commit: the user's way to drop a test arc
    and its tasks without git (2026-11-08). A new row goes; a committed line
    an edit moved aside under a new id comes back as committed, and so does
    another writer's pending version under the entry's id. A row of
    `mine` that another writer's pending revision builds on stays, so does
    an arc a row that stays is filed in, and so does any other change to a
    committed arc, which records no writer. One write, so `undo` takes it
    back; `apply=False` writes nothing. With `expect`, the ids a dry run
    named (discard_ids), it refuses when the store changed since: the GUI's
    dialog lists one plan, and its button must not throw away another.

    Returns {"removed": rows dropped, "restored": committed rows put back,
    "returned": another writer's pending versions a dropped edit sat on,
    each under the entry's id it takes back, "arcs": arcs removed, "kept":
    rows of `mine` kept, "kept_arcs": ids of `author`'s new arcs kept,
    "arcs_changed": ids of committed arcs with changes since HEAD}, rows and
    arcs as dicts."""
    # ponytail: a committed row that cites a discarded one is left citing an
    # unknown id; refuse, or name it, if that is ever seen.
    require_store(store)
    with _lock(store, undoable=apply):
        def committed(name):
            r = subprocess.run(["git", "-C", str(store), "show", f"HEAD:{name}"],
                               capture_output=True, text=True, encoding="utf-8")
            return [ln for ln in jsonl_lines(r.stdout) if ln] if r.returncode == 0 else []

        def loads(ln):
            with suppress(ValueError):
                if isinstance(d := json.loads(ln), dict):
                    return d
            return {}
        pending = pending_rows(store)
        own, theirs = with_bases(pending, mine), with_bases(pending, lambda r: not mine(r))
        drop = {r["id"] for r in pending if own(r) and not theirs(r)}
        # Your edit on another writer's pending version hands that version the
        # entry's id back, as one on a committed line puts the line back: it
        # stayed under its kept id, a second head (audit, 2026-11-08).
        later = {r["supersedes"]: r["id"] for r in pending
                 if r.get("id") in drop and r.get("supersedes")}
        back = {}
        for b in {r.get("id") for r in pending} - drop - {None}:
            i = b
            while i in later:
                i = later[i]
            if i != b:
                back[b] = i
        path = notes_path(store)
        raws = jsonl_lines(path.read_text(encoding="utf-8")) if path.exists() else []
        head = committed(NOTES_FILE)
        held = set(head)        # a list test per line was quadratic (audit, 2026-11-08)
        put_back = {_sans_id(loads(h)): h for h in head
                    if _line_id(h) in drop and _line_id(h) not in back.values()}
        out = {"removed": [], "restored": [], "returned": [], "arcs": [], "kept": [],
               "kept_arcs": [], "arcs_changed": []}
        lines, restored = [], {}
        for raw in raws:
            row = loads(raw) if raw else {}
            if row.get("id") in drop:
                out["removed"].append(row)
            elif row.get("id") in back:
                lines.append(_dumps({**row, "id": back[row["id"]]}))
                out["returned"].append({**row, "id": back[row["id"]]})
            elif raw and raw not in held and (h := put_back.pop(_sans_id(row), None)):
                lines.append(h)
                out["restored"].append(loads(h))
                restored[row.get("id")] = _line_id(h)
            else:
                lines.append(raw)
        out["kept"] = [r for r in pending if own(r) and theirs(r) and r["id"] not in back]
        def arcs_of(r):
            """Each arc a row is filed in, targets or refs: a row left naming a
            removed arc is one `add` refuses to write (audit, 2026-11-08)."""
            objs = [r.get("target"), *(r.get("refs") or ())]
            return {r.get("arc_id"), *(o.get("name") for o in objs
                                       if isinstance(o, dict) and o.get("type") == "arc")}
        filed = set().union(*(arcs_of(loads(ln)) for ln in lines if ln))
        was = {loads(ln).get("id"): ln for ln in committed(ARCS_FILE)}
        apath = arcs_path(store)
        araws = jsonl_lines(_read_utf8(apath)) if apath.exists() else []
        gone = set()
        for raw in filter(None, araws):
            a = loads(raw)
            if a.get("id") in was:
                if raw != was[a["id"]]:
                    out["arcs_changed"].append(a["id"])
            elif a.get("author") == author:
                if a.get("id") in filed:
                    out["kept_arcs"].append(a["id"])
                else:
                    out["arcs"].append(a)
                    gone.add(a.get("id"))
        if expect is not None and discard_ids(out) != expect:
            raise DiscardChanged("the store changed since the list was drawn, so nothing was "
                                 "discarded: open Discard again to see what it takes now")
        if apply:
            if lines != raws:
                _replace_atomically(path, "\n".join(lines) + "\n" if lines else "")
            if gone:
                left = [r for r in araws if r and loads(r).get("id") not in gone]
                _replace_atomically(apath, "".join(r + "\n" for r in left))
            known = sessions(store, committed=True)    # _forget_committed marks it again
            for b, i in {**back, **restored}.items():    # the id goes back to the version's session
                _append_line_unlocked(_state_dir(store, make=True) / SESSIONS_FILE,
                                      f"{i}\t{known.get(b, '')}")
            _forget_committed(store)
        return out


def _stage_kept(store: Path, keep) -> subprocess.CompletedProcess:
    """Stage HEAD's rows plus the pending rows `keep` takes, in file order,
    and arcs.jsonl. A kept revision brings its pending base: a commit must
    not hold a revision of a row it lacks.

    The file keeps every row and takes the commit's order, the rows left
    out after it, so they still read as lines added since HEAD. A commit
    wrote its rows after HEAD's but left them among the others on disk,
    and git read the difference as moved lines that no later commit stages
    (2026-10-12)."""
    git = ["git", "-C", str(store)]
    head = subprocess.run([*git, "show", f"HEAD:{NOTES_FILE}"], capture_output=True,
                          text=True, encoding="utf-8")
    old = [ln for ln in jsonl_lines(head.stdout) if ln] if head.returncode == 0 else []
    path = notes_path(store)
    seen = set(old)
    disk = [ln for ln in (jsonl_lines(path.read_text(encoding="utf-8")) if path.exists() else [])
            if ln]
    # A line that does not parse reads as {}, as in added_rows, and stays out.
    pending = [(ln, (added_rows(f"+{ln}") or [{}])[0]) for ln in disk if ln not in seen]
    # A version an edit kept is HEAD's line under a new id: it goes where
    # that line was, with the edit, and is never taken on its own.
    olds = {_sans_id(r): r.get("id") for r in ((added_rows(f"+{ln}") or [{}])[0] for ln in old)
            if r}
    moved = {olds[_sans_id(r)]: ln for ln, r in pending if r and _sans_id(r) in olds}
    kept_ids = {_line_id(ln) for ln in moved.values()}
    taken = with_bases([r for _, r in pending], lambda r: keep(r) and r.get("id") not in kept_ids)
    take = {r["id"] for _, r in pending if r.get("id") and taken(r)}
    old_ids = [_line_id(ln) for ln in old]
    # HEAD's line gave way to the kept version on disk; by the kept version's
    # id, as a commit can take it, a revision's base, without the head that
    # holds the row's id now. A pull leaves the upstream's kept line on disk.
    on_disk = set(disk)
    head_rows = [moved[i] if ln not in on_disk and i in moved and _line_id(moved[i]) in take
                 else ln for ln, i in zip(old, old_ids)]
    rows = head_rows + [ln for ln, r in pending if r.get("id") in take and ln not in head_rows]
    text = "".join(ln + "\n" for ln in rows)
    # A version an edit kept and this commit leaves sorts where its HEAD line is.
    pos = {ln: k for k, ln in enumerate(rows)}
    pos.update((moved[i], k) for k, i in enumerate(old_ids) if i in moved)
    order = sorted(range(len(disk)), key=lambda j: (0, pos[disk[j]]) if disk[j] in pos else (1, j))
    if order != sorted(order):
        _replace_atomically(path, "".join(disk[j] + "\n" for j in order))
    # Bytes: text mode on Windows wrote each \n as \r\n, so every kept line
    # differed from HEAD's and the commit read as a rewrite of every row.
    blob = subprocess.run([*git, "hash-object", "-w", "--stdin"], input=text.encode("utf-8"),
                          capture_output=True)
    blob.stdout, blob.stderr = (b.decode("utf-8", "replace") for b in (blob.stdout, blob.stderr))
    if blob.returncode:
        return blob
    r = subprocess.run([*git, "update-index", "--add", "--cacheinfo",
                        f"100644,{blob.stdout.strip()},{NOTES_FILE}"],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode == 0 and (store / ARCS_FILE).exists():
        r = subprocess.run([*git, "add", "--", ARCS_FILE], capture_output=True, text=True,
                           encoding="utf-8")
    return r


def _put(store: Path, texts: dict) -> None:
    """Each file `texts` names written as its text; None removes the file."""
    for name, body in texts.items():
        if body is None:
            (store / name).unlink(missing_ok=True)
        else:
            _replace_atomically(store / name, body)


# ---- pull: the upstream merged around the pending rows ----
PULLED = (NOTES_FILE, ARCS_FILE)
PULL_ASIDE = "symbion-pull"


def _git_said(store, *args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(store), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, **kw)


def _git_paths(store, cmd, *args) -> set:
    return set(filter(None, _git_said(store, cmd, "-z", *args).stdout.split("\0")))


def _blob(store, rev: str, name: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(store), "show", f"{rev}:{name}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _on_disk(store) -> dict:
    return {f: (store / f).read_bytes() if (store / f).exists() else None for f in PULLED}


def _body_lines(body) -> list[str]:            # CRLF reads as LF, as read_text reads it
    return jsonl_lines(body.decode("utf-8").replace("\r\n", "\n")) if body else []


def _pull_aside(store) -> Path | None:
    """The git dir's copy of what a pull set aside, while it runs."""
    r = _git_said(store, "rev-parse", "--git-path", PULL_ASIDE)
    return Path(store, r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _pending_back(old: dict, now: dict, merged: dict, renames: dict, dropped: list) -> tuple:
    """The files with the pending changes, `now` against `old` (lines by
    file), put onto `merged`, and the splits, the merge's `renames` first."""
    notes, splits = merge.reapply(old[NOTES_FILE], now[NOTES_FILE], merged[NOTES_FILE],
                                  renames, dropped)
    arcs, said = merge.merge_arcs(old[ARCS_FILE], now[ARCS_FILE], merged[ARCS_FILE])
    dropped += said
    return ({NOTES_FILE: "".join(ln + "\n" for ln in notes),
             ARCS_FILE: "".join(ln + "\n" for ln in arcs)}, [*renames.items(), *splits])


def _mid_merge(store) -> str:
    here = f"git -C {shlex.quote(str(store))}"
    return (f"the store is in the middle of a merge: finish it ({here} commit) or abort it "
            f"({here} merge --abort), then pull")


def _come_back(whose: str) -> str:
    return f"the pending entries {whose} set aside come back at the next pull"


def _drop_aside(aside: Path) -> None:
    """Remove the copy by one rename, then delete it: a kill part way
    through a delete of the copy itself left a part of it, read back as
    rows the pending changes removed (a reviewer, 2026-10-26). A tombstone
    a kill leaves is cleared when the next copy is made."""
    if aside.exists():
        os.replace(aside, gone := aside.with_name(PULL_ASIDE + ".gone"))
        shutil.rmtree(gone, ignore_errors=True)


def _read_aside(store, aside: Path) -> tuple:
    """(old HEAD, saved files' bytes, splits) from a copy, refused unless
    it is whole: its HEAD, its list of the files it holds, each of them, and
    the commit HEAD names. A part of a copy applied as a whole one reads a
    lost file as rows removed."""
    try:
        sha = (aside / "HEAD").read_text(encoding="utf-8").strip()
        holds = (aside / "holds").read_text(encoding="utf-8").split()
        saved = {f: (aside / f).read_bytes() if f in holds else None for f in PULLED}
        splits = aside / "splits.json"
        renames = json.loads(splits.read_text(encoding="utf-8")) if splits.exists() else {}
        whole = isinstance(renames, dict) and _git_said(
            store, "rev-parse", "-q", "--verify", f"{sha}^{{commit}}").returncode == 0
    except (OSError, ValueError):
        whole = False
    if not whole:
        raise RuntimeError(f"the copy a cut-off pull left in {aside} is not whole, so nothing "
                           f"was put back from it: compare its files with notes.jsonl and "
                           f"arcs.jsonl, then remove it")
    return sha, saved, renames


def _keep_sessions(store, old: dict, now: dict, splits: list, texts: dict) -> None:
    """A pending head under a new id stays its session's, for its `commit`.
    A committed row the other clone edited keeps its id for that clone's
    version, and the version this clone had takes the id the edit gave it:
    the mark goes with it. Left on the id, it read the other clone's answer
    as the asking session's own, and no write named it (audit, 2026-11-12)."""
    def mark(line):
        _append_line_unlocked(_state_dir(store, make=True) / SESSIONS_FILE, line)
    known = sessions(store)
    held = {_line_id(ln) for ln in set(now[NOTES_FILE]) - set(old[NOTES_FILE])}
    for was, new in splits:
        if was in held and known.get(was):
            mark(f"{new}\t{known[was]}")
    took = {i: k for i, k in sessions(store, committed=True).items() if i not in known}
    had = {i: json.loads(ln) for ln in old[NOTES_FILE] if took and (i := _line_id(ln)) in took}
    merged = jsonl_lines(texts[NOTES_FILE]) if had else []
    moved = [i for ln in merged if (i := _line_id(ln)) in had and json.loads(ln) != had[i]]
    fresh = [json.loads(ln) for ln in set(merged) - set(old[NOTES_FILE])
             if moved and _line_id(ln)]
    for i in moved:
        mark(f"{i}\t")
        if to := next((f["id"] for f in fresh if {**f, "id": i} == had[i]), None):
            mark(f"{to}\t{took[i]}\tc")


def _recover_unlocked(store) -> tuple:
    """Put back what a pull that was killed set aside: its copy outlived
    it. The pending changes go onto the files as they are now, which may
    hold the merge and later rows, read through the splits the pull saved,
    so rows it had already put back are not put back twice. A merge in
    progress is left to whoever finishes it, a person perhaps: the rows wait
    in the copy. Returns (one line or None, splits, dropped, the HEAD the
    copy saved or None): a cut-off pull's merge is said from that HEAD."""
    aside = _pull_aside(store)
    if aside is None or not aside.exists():
        return None, [], [], None
    if _git_said(store, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        raise RuntimeError(f"{_mid_merge(store)}: {_come_back('a cut-off pull')}")
    sha, saved, renames = _read_aside(store, aside)
    old = {f: _body_lines(_blob(store, sha, f)) for f in PULLED}
    now = {f: _body_lines(saved[f]) for f in PULLED}
    dropped = []
    texts, splits = _pending_back(old, now, {f: _body_lines(b) for f, b in _on_disk(store).items()},
                                  renames, dropped)
    _put(store, texts)
    _keep_sessions(store, old, now, splits, texts)
    _drop_aside(aside)
    _forget_undo(store)
    return ("a pull was cut off before it put back the pending entries it had set aside: "
            "they are back, still pending"), splits, dropped, sha


def recover_pull(store, until: float | None = None) -> tuple:
    """_recover_unlocked under the lock, taken only when a copy is left:
    the pending rows go back even when no fetch can run. `until` is
    _lock's."""
    store = Path(store)
    aside = _pull_aside(store)
    if aside is None or not aside.exists():
        return None, [], [], None
    with _lock(store, undoable=False, until=until, gated=False):
        return _recover_unlocked(store)


def merge_upstream(store, upstream: str, until: float | None = None) -> dict:
    """`git merge` of `upstream` around the pending rows, for `symbion pull`,
    under the lock. notes.jsonl and arcs.jsonl go back to HEAD for the merge,
    the driver merges them, and their pending changes go back on after it
    (merge.reapply, merge_arcs, against the old HEAD): still pending, and
    still the session's that wrote them. Until they are back, a copy of them
    sits in the git dir (PULL_ASIDE), and a pull a kill cut off is put back
    first. A merge git refuses or stops changes nothing here, and the paths
    it names go in `conflicts`, read from git's plumbing, since its words are
    localized. Returns old_head (HEAD before the merge, or before the
    cut-off pull it put back), merged (its commits), splits, dropped,
    conflicts and recovered. `until` is _lock's."""
    store = Path(store)
    require_store(store)

    def run(*args, **kw):
        return _git_said(store, *args, **kw)

    def head():
        return run("rev-parse", "-q", "--verify", "HEAD").stdout.strip()

    with _lock(store, undoable=False, until=until, gated=False):
        recovered, splits, dropped, was = _recover_unlocked(store)
        if run("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            # Undoing a failed pull runs `merge --abort`, which would take this one too.
            raise RuntimeError(_mid_merge(store))
        old_head = head()
        n = run("rev-list", "--count", f"HEAD..{upstream}").stdout.strip()
        out = {"old_head": was or old_head, "merged": int(n) if n.isdigit() else 0,
               "splits": splits, "dropped": dropped, "conflicts": [], "recovered": recovered}
        if not out["merged"]:
            return out
        # git refuses a merge over a change not committed to a file the merge
        # writes, and a merge that is no fast-forward over any staged change.
        # The two files here go back to HEAD first, so they never block it.
        run("update-index", "-q", "--refresh")               # a touched file is no change
        # --no-renames: a rename lists its old name too, which a local change may hold.
        theirs = _git_paths(store, "diff", "--no-renames", "--name-only", f"HEAD...{upstream}")
        local = _git_paths(store, "diff-index", "--name-only", "HEAD") | \
            _git_paths(store, "ls-files", "--others", "--exclude-standard")
        ff = run("merge-base", "--is-ancestor", "HEAD", upstream).returncode == 0
        staged = set() if ff else _git_paths(store, "diff-index", "--cached", "--name-only",
                                             "HEAD")
        if blocked := sorted((local & theirs | staged) - set(PULLED)):
            out.update(merged=0, conflicts=blocked)
            return out
        before, at_head = _on_disk(store), {f: _blob(store, "HEAD", f) for f in PULLED}
        old, now = ({f: _body_lines(t[f]) for f in PULLED} for t in (at_head, before))
        # The copy goes in whole or not at all: a kill while it is written
        # leaves the files as they were and no copy to put back. `holds`
        # lists the files it holds, so a part of it reads as damaged.
        aside = _pull_aside(store)
        if aside is None:
            raise RuntimeError(f"git names no git dir for {store}, so nothing was merged")
        new = aside.with_name(PULL_ASIDE + ".new")
        for left in (new, aside.with_name(PULL_ASIDE + ".gone")):
            shutil.rmtree(left, ignore_errors=True)
        new.mkdir()
        _put(new, before)
        _replace_atomically(new / "holds", "".join(f"{f}\n" for f in PULLED if before[f] is not None))
        _replace_atomically(new / "HEAD", old_head + "\n")
        os.replace(new, aside)
        merged, ok = {}, False
        try:
            run("reset", "-q", "--", *PULLED)
            # git writes HEAD's copies and records their size. Bytes written
            # here differ in size from a CRLF checkout (core.autocrlf, on by
            # default in Git for Windows), and git then calls the file changed
            # unread and refuses the merge (a Windows VM, 2026-10-28).
            _put(store, {f: None for f in PULLED if at_head[f] is None})
            if there := [f for f in PULLED if at_head[f] is not None]:
                run("checkout-index", "-f", "-u", "--", *there)
            stamp = _now_iso()           # in UTC by the one clock, as `commit` dates its own
            r = run("merge", "--no-edit", upstream,
                    env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp, **os.environ})
            if r.returncode:
                out["conflicts"] = sorted(_git_paths(store, "diff", "--name-only",
                                                     "--diff-filter=U"))
            else:
                if CONFIG_FILE in theirs:    # under the lock: no write runs its commands first
                    distrust(store, old_head)
                merged = _on_disk(store)
                renames = {}
                for said in r.stderr.split("\n"):            # the driver's lines
                    if said.startswith("symbion: "):
                        if m := merge.SAID_SPLIT.fullmatch(said.removeprefix("symbion: ")):
                            renames[m[1]] = m[2]
                        else:
                            dropped.append(said.removeprefix("symbion: "))
                texts, moved = _pending_back(old, now, {f: _body_lines(merged[f]) for f in PULLED},
                                             renames, dropped)
                # A kill from here on is put back through these, so a row
                # already back under its new id is not put back again.
                _replace_atomically(aside / "splits.json", json.dumps(dict(moved)))
                _put(store, texts)
                _keep_sessions(store, old, now, moved, texts)
                _drop_aside(aside)
                splits += moved
                _forget_undo(store)                  # a pull ends the undo level, as a commit does
                ok = True
        finally:
            if not ok:
                # Take back the merge, one git stopped or one it made, then
                # the files as they were. `reset --merge` stops at a file
                # changed since the merge, so those go back to the merge's
                # first, and at one rewritten as it was until the index is
                # refreshed ("not uptodate", measured 2026-10-26).
                # A step git refuses leaves the files as git left them and
                # the copy, which the next pull puts back.
                if run("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0 and \
                        (r2 := run("merge", "--abort")).returncode:
                    raise RuntimeError(f"git could not abort the merge this pull began, so "
                                       f"{_mid_merge(store)}: {_come_back('this pull')}. git "
                                       f"said:\n{(r2.stderr + r2.stdout).strip()}")
                if head() != old_head:
                    _put(store, merged)
                    run("update-index", "-q", "--refresh")
                    if (r2 := run("reset", "-q", "--merge", old_head)).returncode:
                        raise RuntimeError(f"git could not take back the merge this pull made, "
                                           f"so the store keeps it, and "
                                           f"{_come_back('this pull')}. git said:"
                                           f"\n{(r2.stderr + r2.stdout).strip()}")
                _put(store, before)
                _drop_aside(aside)
        if not ok:
            if not out["conflicts"]:
                said = (r.stderr + r.stdout).strip() or f"exit {r.returncode}, no output"
                raise RuntimeError(f"git refused the merge, so nothing here changed. "
                                   f"git said:\n{said}")
            out["merged"] = 0
        return out


# ==========================================================================
# Arc registry: serialization, mutators, membership/progress, seeding.
#
# Lifted from the reference, with two structural changes from it (same
# reasoning as the I/O layer above):
#
# 1. `create_arc`, `rename_arc`, and `archive_arc` each hold
#    ONE `_lock` spanning load -> modify -> write. The reference's
#    `_write_arcs` locks only the write, so two racing creates can both
#    load the same registry snapshot and one rewrite clobbers the other —
#    this file has no supersede history to recover the lost row from.
# 2. `_mint_unique` takes an injectable `_gen`, since the reference calls
#    bare `new_id()`, making its retry loop unreachable from a test.
# ==========================================================================

def arc_from_dict(d: dict, legal_scopes=None) -> Arc:
    if d["target_scope"] == "global":
        raise ValueError(f"retired scope 'global': {_VOCAB_HINT}; rename it in arcs.jsonl")
    if legal_scopes is not None and d["target_scope"] not in legal_scopes:
        raise ValueError(f"unknown scope: {d['target_scope']!r}")
    ph, pl = d.get("phases"), d.get("placement")
    if ph is not None and not (isinstance(ph, list) and all(isinstance(x, str) for x in ph)):
        raise ValueError("phases is not a list of names")
    if pl is not None and not (isinstance(pl, list) and all(
            isinstance(x, dict) and isinstance(x.get("id"), str) for x in pl)):
        raise ValueError("placement is not a list of objects with an id")
    return Arc(
        id=d["id"], name=d["name"], description=d.get("description", ""),
        target_scope=d["target_scope"], created_at=d["created_at"],
        author=d.get("author", "unknown"),
        archived=bool(d.get("archived", False)),
        archived_at=d.get("archived_at"),
        side=d.get("side"),
        phases=tuple(d["phases"]) if d.get("phases") is not None else None,
        placement=tuple(d["placement"]) if d.get("placement") is not None else None,
        changed_by=d.get("changed_by"), changed_at=d.get("changed_at"),
        extra={k: v for k, v in d.items() if k not in _ARC_KEYS} or None,
    )


def arc_to_dict(act: Arc) -> dict:
    return {
        "id": act.id, "name": act.name, "description": act.description,
        "target_scope": act.target_scope, "created_at": act.created_at,
        "author": act.author, "archived": act.archived,
        "archived_at": act.archived_at,
        **({"side": act.side} if act.side is not None else {}),
        **({"phases": list(act.phases)} if act.phases is not None else {}),
        **({"placement": list(act.placement)} if act.placement is not None else {}),
        **({"changed_by": act.changed_by, "changed_at": act.changed_at}
           if act.changed_by is not None else {}),
        **(act.extra or {}),
    }


def _read_arcs(store) -> tuple[list, list]:
    """(arcs, [(lineno, raw, error)]), in file order."""
    file = arcs_path(store)
    if not file.exists():
        return [], []
    return _parse_jsonl(_read_utf8(file), arc_from_dict)


def load_arcs(store):
    """Every readable arc, in file order. An unreadable line is counted by
    `summary` (load_arcs_malformed) and kept by every rewrite."""
    return _read_arcs(store)[0]


def load_arcs_malformed(store):
    """[(lineno, raw, error)] for arc lines that failed to read."""
    return _read_arcs(store)[1]


def _write_arcs_unlocked(store, acts) -> None:
    """Rewrite the whole registry (it's tiny; git tracks the diff). Atomic:
    this is the only file here with no supersede history to recover from, so
    a crash mid-write would truncate the one thing that cannot be
    reconstructed. Caller must already hold `_lock`.

    A line load_arcs could not read is written back verbatim: the rewrite
    was built from what load_arcs returned, so the next `arc create` erased
    a hand-edited line for good (reproduced 2026-09-27)."""
    kept = [raw for _, raw, _ in _read_arcs(store)[1]]
    body = "\n".join([*(_dumps(arc_to_dict(a)) for a in acts), *kept])
    _replace_atomically(arcs_path(store), body + ("\n" if body else ""))


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", nfc(name).lower()).strip("-")
    return (s or "arc")[:48]


def create_arc(store, name, description, target_scope, author="unknown",
                     legal_scopes=None) -> Arc:
    if legal_scopes is not None and target_scope not in legal_scopes:
        raise ValueError(f"unknown scope: {target_scope!r}")
    require_store(store)
    side = shared_side(store)
    with _lock(store):                       # outer: load + mint slug + write
        acts = load_arcs(store)
        ids = {a.id for a in acts}
        base = _slugify(name)
        slug, n = base, 2
        while slug in ids:                       # stable, URL-safe, collision-free slug
            suffix = f"-{n}"
            slug = base[:max(0, 48 - len(suffix))] + suffix  # cap the FINAL id, not base
            n += 1
        act = Arc(id=slug, name=name, description=description,
                       target_scope=target_scope, created_at=_now_iso(),
                       author=author, archived=False, archived_at=None, side=side)
        _write_arcs_unlocked(store, acts + [act])
    return act


def _edit_arc(store, id, change, author="unknown") -> Arc:
    """Arc `id` as `change(arc)` returns it, written under the lock and
    stamped with its writer; KeyError if there is none. The id stays, so its
    rows' arc_id still resolve. Unstamped, every edit but a move counted
    `unknown` in commit's `Arcs:` trailer (dogfood, 2026-11-06)."""
    require_store(store)
    with _lock(store):
        acts = load_arcs(store)
        old = next((a for a in acts if a.id == id), None)
        if old is None:
            raise KeyError(id)
        new = change(old)
        if new != old:
            new = _stamped(new, author)
            _write_arcs_unlocked(store, [new if a.id == id else a for a in acts])
    return new


def _stamped(act: Arc, author) -> Arc:
    """`act` with its writer."""
    return replace(act, changed_by=author, changed_at=_now_fine())


def rename_arc(store, id, new_name, author="unknown") -> Arc:
    return _edit_arc(store, id, lambda a: replace(a, name=new_name), author)


def archive_arc(store, id, _clock=None, author="unknown") -> Arc:
    """An arc archived already is returned as it is: archiving it again
    moved `archived_at`, the date it left the summary."""
    clock = _clock or _utcnow
    return _edit_arc(store, id, lambda a: a if a.archived else replace(
        a, archived=True, archived_at=clock().isoformat(timespec="seconds")), author)


def unarchive_arc(store, id, author="unknown") -> Arc:
    """Back on the summary: `undo` takes an archive back only before a commit."""
    return _edit_arc(store, id, lambda a: replace(a, archived=False, archived_at=None), author)


def describe_arc(store, id, description, author="unknown") -> Arc:
    return _edit_arc(store, id, lambda a: replace(a, description=description), author)


# ---- arc membership, progress, seeding ----
def arc_items(notes, arc_id):
    """Current checklist rows for an arc: every HEAD row in it that
    carries a status, in insertion order. The status bit, not the task
    label alone, so a bug attached to a campaign is part of that campaign --
    otherwise `arc todo` and `list --arc` disagree about the same
    campaign and the progress count is short by the bugs.

    This does NOT collapse by target. It used to, defending the case of a
    hand-edited resolved task with no `supersedes` link -- but heads()
    already collapses real supersede chains, so the collapse only ever cost
    correctness on well-formed stores: three tickets on one file read back as
    one, silently, in `todo` and in the count while `list --arc` showed
    all three. Measured 2026-09-05 in a bootstrap, where an agent lost rows
    to it and rebuilt its whole store to route around it.

    A parked kind (idea) is never an item: the status bit without the parked
    bit is what makes a checklist box."""
    return [n for n in heads(notes)
            if n.spec.status and not n.spec.parked and n.arc_id == arc_id]


def arc_progress(notes, arc_id):
    """(done, total) over arc_items heads — a ticked item counts once."""
    items = arc_items(notes, arc_id)
    done = sum(1 for n in items if n.status == "resolved")
    return done, len(items)


def arc_last_change(notes, act: Arc) -> tuple[str, str]:
    """(stamp, author) of the arc's newest change: the latest version of any
    entry filed in it, or a move on its board, else its creation. A rename or a
    phase edit does not count: until 2026-11-09 the arc's own line held no time."""
    marks = [(n.created_at, n.author) for n in heads(notes) if n.arc_id == act.id]
    marks += [(p["at"], p.get("by") or "") for p in act.placement or () if p.get("at")]
    return max([(act.created_at, act.author), *marks], key=lambda m: written_at(None, m[0]))


DONE = "done"  # the board's last column; reserved as a phase name


def box_phase(act: Arc, note, placed=None) -> str | None:
    """The board column a box sits in: DONE once resolved; None for an open box
    on an arc without phases; else its placed phase, where a null phase, a
    phase no longer in `act.phases` or no placement reads as the first.
    `placed`, from `_placed(act)`, spares a board one scan per box."""
    if note.status == "resolved":
        return DONE
    if not act.phases:
        return None
    phase = (_placed(act) if placed is None else placed).get(note.id)
    return phase if phase in act.phases else act.phases[0]


def _placed(act: Arc) -> dict:
    """{box id: its placed phase}; an id placed twice keeps its first."""
    return {p["id"]: p.get("phase") for p in reversed(act.placement or ())}


def arc_board(notes, act: Arc) -> list[tuple[str | None, list]]:
    """The arc's columns in board order, each (name, boxes): one per phase, or one
    unnamed column without phases, then DONE. An open column holds its placed
    boxes in `placement` order, then the rest in write order; DONE, newest first.
    A placement for an entry that is not a box of the arc is ignored."""
    boxes = arc_items(notes, act.id)
    names = list(act.phases or (None,))
    cols: dict = {n: [] for n in names}
    done = []
    placed = _placed(act)       # a scan per box was boxes x placements (audit, 2026-11-08)
    for b in boxes:
        ph = box_phase(act, b, placed)
        (done if ph == DONE else cols[ph]).append(b)
    rank = {p["id"]: i for i, p in enumerate(act.placement or ())
            if p.get("phase") is None or p["phase"] in (act.phases or ())}
    for rows in cols.values():
        rows.sort(key=lambda n: (n.id not in rank, rank.get(n.id, 0), written_at(n), n.id))
    return [*cols.items(), (DONE, newest_first(done))]


def _box_of(acts, notes, entry_id):
    """(entry, its live arc) for a box a placement may name; KeyError for an
    unknown entry, ValueError saying why any other entry is no box."""
    entry = next((n for n in heads(notes) if n.id == entry_id), None)
    if entry is None:
        raise KeyError(entry_id)
    act = next((a for a in acts if a.id == entry.arc_id), None)
    if act is None:
        raise ValueError(f"{entry_id} is in no arc: put it in one with `supersede "
                         f"{entry_id} --arc ID`")
    if entry.spec.parked or not entry.spec.status:
        raise ValueError(f"{entry_id} is {'an' if entry.kind[0] in 'aeiou' else 'a'} {entry.kind}, which is never a box on a board: "
                         f"only a kind with the status bit that is not parked is")
    if act.archived:
        raise ValueError(f"arc {act.id} is archived: `arc unarchive {act.id}` first")
    return entry, act


def _place(act: Arc, notes, entry, phase, before, after, top, author, now) -> Arc:
    """`act` with `entry` placed in column `phase` (else the one it reads in) at
    the top, before or after a box of it, else at the end. No lock, no I/O: the
    caller writes. Only the column prefix ahead of the insertion point gains
    order-only placements, so a move writes one per box ahead of it."""
    if sum((before is not None, after is not None, top)) > 1:
        raise ValueError("give one of --before, --after and --top")
    if phase is not None:
        if phase.casefold() == DONE:
            raise ValueError(f"{DONE} is the board's last column, reached by "
                             f"`resolve {entry.id}`")
        if not act.phases:
            raise ValueError(f"arc {act.id} has no phases: `arc phases {act.id} NAME...` "
                             f"sets them")
        if phase not in act.phases:
            raise ValueError(f"no phase {phase!r} in arc {act.id}: its phases are "
                             f"{', '.join(act.phases)}")
    target = phase if phase is not None else box_phase(act, replace(entry, status="open"))
    col = [b.id for b in dict(arc_board(notes, act))[target] if b.id != entry.id]
    anchor = before if before is not None else after
    if anchor is not None and anchor not in col:
        raise ValueError(f"{anchor} is not an open box in phase {target!r} of arc {act.id}: "
                         f"`arc todo {act.id}` shows the open boxes")
    k = 0 if top else len(col) if anchor is None else col.index(anchor) + (after is not None)
    held = {p["id"]: p for p in act.placement or ()}
    ahead = [{**held[i], "phase": target} if i in held else {"id": i, "phase": target}
             for i in col[:k]]
    behind = [held[i] for i in col[k:] if i in held and held[i].get("phase") in (None, target)]
    mine = {"id": entry.id, "phase": target, "by": author, "at": now}
    if (was := box_phase(act, replace(entry, status="open"))) not in (None, target):
        mine["from"] = was          # the board's foot names it; a reorder keeps none
    gone = {*col, entry.id}
    rest = [p for p in act.placement or () if p["id"] not in gone]
    return replace(act, placement=tuple(rest + ahead + [mine] + behind))


def set_phases(store, arc_id: str, names: list[str], author="unknown") -> tuple[Arc, int]:
    """The arc with its phases `names` (none clears them), and how many resolved
    boxes' placements that dropped. Refused: a bad name, or dropping a phase an
    open box is placed in."""
    for i, name in enumerate(names):
        if not name or name != name.strip():
            raise ValueError(f"phase name {name!r} is empty or has a leading or trailing "
                             f"space: a name is text with none at its ends")
        if "," in name:
            raise ValueError(f"phase name {name!r} has a comma: the GUI editor splits names "
                             f"on commas")
        if name.casefold() == DONE:
            raise ValueError(f"{name!r} is reserved: {DONE} is the board's last column, "
                             f"in any case")
        if name in names[:i]:
            raise ValueError(f"phase name {name!r} is given twice: names are unique")
    require_store(store)
    with _lock(store):
        acts = load_arcs(store)
        act = next((a for a in acts if a.id == arc_id), None)
        if act is None:
            raise KeyError(arc_id)
        notes = load(store)
        items = {n.id: n for n in arc_items(notes, arc_id)}
        placed = tuple(act.placement or ())
        if not names and not act.phases:
            return act, 0                   # nothing to clear: no write, no undo level
        if not names:
            held = {p["id"]: p for p in placed}
            opened = [b.id for _, rows in arc_board(notes, act)[:-1] for b in rows]
            rest = [p for p in placed if p["id"] not in opened]
            new = replace(act, phases=None, placement=tuple(
                {**held.get(i, {"id": i}), "phase": None} for i in opened)
                + tuple({**p, "phase": None} for p in rest) or None)
            dropped = 0
        else:
            held = sorted((p["phase"], p["id"][-10:]) for p in placed if p.get("phase") not in names
                          and p.get("phase") in (act.phases or ())
                          and p["id"] in items and items[p["id"]].status != "resolved")
            if held:
                raise ValueError(f"phase {held[0][0]!r} holds open boxes: "
                                 f"{', '.join(i for _, i in held)}: move each "
                                 f"out with `arc move ENTRY --phase NAME` first")
            gone = set(act.phases or ()) - set(names)
            keep = tuple(p for p in placed if not (
                p.get("phase") in gone and p["id"] in items and items[p["id"]].status == "resolved"))
            dropped = len(placed) - len(keep)
            new = replace(act, phases=tuple(names), placement=keep or None)
        if new != act:
            new = _stamped(new, author)
            _write_arcs_unlocked(store, [new if a.id == arc_id else a for a in acts])
    return new, dropped


def move_box(store, entry_id: str, *, phase: str | None = None, before: str | None = None,
             after: str | None = None, top: bool = False, author: str) -> Arc:
    """Place an open box of a live arc: in `phase` (else where it is), at the top,
    before or after another open box of that column, else at its end."""
    require_store(store)
    with _lock(store):
        acts, notes = load_arcs(store), load(store)
        entry, act = _box_of(acts, notes, entry_id)
        if entry.status == "resolved":
            raise ValueError(f"{entry_id} is resolved, so it sits in {DONE}: "
                             f"`supersede {entry_id} --status open` reopens it")
        new = _place(act, notes, entry, phase, before, after, top, author, _now_fine())
        _write_arcs_unlocked(store, [new if a.id == act.id else a for a in acts])
    return new


def reopen_and_place(store, entry_id: str, *, phase: str | None = None,
                     before: str | None = None, after: str | None = None,
                     top: bool = False, author: str) -> tuple[Note, Arc]:
    """Reopen a resolved box and place it, as one write under one lock: a
    placement that is refused leaves the box resolved."""
    require_store(store)
    side = shared_side(store)
    with _lock(store):
        acts, notes = load_arcs(store), load(store)
        entry, act = _box_of(acts, notes, entry_id)
        if entry.status != "resolved":
            raise ValueError(f"{entry_id} is open already: `arc move {entry_id}` places it")
        new = _place(act, notes, entry, phase, before, after, top, author, _now_fine())
        note = _supersede_unlocked(store, notes, entry_id, author=author, side=side,
                                   status="open")
        _write_arcs_unlocked(store, [new if a.id == act.id else a for a in acts])
    return note, new


def _mint_unique(used: set, _gen=None) -> str:
    """An id not in `used`. `_gen` is injectable so a test can script
    collisions; the reference called new_id() bare, which made the retry loop
    untestable (and unreachable from new_id's own _clock/_rand injectables)."""
    gen = _gen or new_id
    nid = gen()
    while nid in used:
        nid = gen()
    used.add(nid)
    return nid


def seed_arc(store, arc_id, target_type, names, author="unknown", kind="task", _gen=None,
             canonicalize=None, stamp=None):
    """One open row of `kind` per name that has no head item yet. Idempotent.

    `item` scope never runs a catalog — names are always explicit (the caller
    resolves catalog names before calling).

    Eligible kinds have the status bit and neither parked nor verdict: a
    seeded row is a checklist box that is nothing but its name, and nothing
    here stamps provenance, so a seeded verdict row would be a prediction
    with no commitment sha and no falsifier.

    `canonicalize(names) -> names` runs inside the lock, so idempotence is judged on the canonical name.
    `stamp(names) -> {name: blob}` stamps each new row's file as `add` does,
    so its commits_since counts from the content it was seeded over."""
    kinds = K.read_kinds(store)
    spec = kinds.get(kind)
    if spec is None:
        raise ValueError(f"no kind {kind!r} in this store; its kinds are {', '.join(kinds)} "
                         f"(declared under [kinds] in symbion.toml); pass --kind")
    if not spec.status:
        raise ValueError(f"kind {kind!r} cannot be seeded: it has no status bit, "
                         f"and a seeded entry is a checklist box")
    if spec.parked:
        raise ValueError(f"kind {kind!r} cannot be seeded: it is parked, and a "
                         f"parked entry is never a checklist box")
    if spec.verdict:
        raise ValueError(f"kind {kind!r} cannot be seeded: it has the verdict bit, "
                         f"and seed stamps no provenance and writes no falsifier")
    require_store(store)
    side = shared_side(store)
    with _lock(store):                       # outer: load + mint + append
        notes = load(store)
        if canonicalize is not None:
            names = canonicalize(names)
        members = {(n.target.type, n.target.name)
                   for n in arc_items(notes, arc_id)}
        used = {n.id for n in notes}
        blobs = stamp([n for n in names if (target_type, n) not in members]) if stamp else {}
        created = []
        for name in names:
            if (target_type, name) in members:
                continue
            note = note_from_dict({
                "id": _mint_unique(used, _gen), "kind": kind,
                "target": {"type": target_type, "name": name},
                "created_at": _now_iso(), "author": author, "body": "",
                "status": "open", "arc_id": arc_id, "target_blob": blobs.get(name),
                "side": side,
            }, kinds=kinds)
            _append_note_unlocked(store, note)
            created.append(note)
            members.add((target_type, name))
    return created


# ---- reconcile & safe apply ----
def reconcile_arc(notes, arc_id, live_for, rename_map, catalog_types):
    """Classify each OPEN task's target against the live catalog.

    Arc tasks freeze the target NAME at mint time, so after a
    catalog rebuild/rename a task can point at an object that no longer
    exists. This surfaces those without a hand-rolled set-diff.

    `live_for(target_type) -> set[str]` and `catalog_types` are injected —
    this module never imports config or a catalog constant. `rename_map`
    is {target type: {old name: current name}} (see catalog.rename_map):
    one map for every type moved a repo's task on another repo's rename
    (2026-10-15).

    Returns rows [{id, target_type, target_name, status, suggestion}] where
    status is 'live' (target still exists), 'renamed' (old name -> a live
    new name via rename_map), 'stale' (no live target, no known rename), or
    'uncheckable' (a target type with no catalog, e.g. item/project).

    `needs_result` marks a status+verdict row (a pre-registration):
    `apply_reconciliation` will not close it as stale, because its verdict
    cannot be 'the target disappeared'."""
    rename_map = rename_map or {}
    rows = []
    for n in arc_items(notes, arc_id):
        if read_status(n) != "open":
            continue
        name = n.target.name
        if n.target.type not in catalog_types:
            status, sugg = "uncheckable", None
        else:
            live, new = live_for(n.target.type), rename_map.get(n.target.type, {}).get(name)
            if name in live:
                status, sugg = "live", None
            elif new in live:
                status, sugg = "renamed", new
            else:
                status, sugg = "stale", None
        rows.append({"id": n.id, "target_type": n.target.type,
                     "target_name": name, "status": status, "suggestion": sugg,
                     "needs_result": bool(n.spec.status and n.spec.verdict)})
    return rows


def apply_reconciliation(store, rows, resolve_stale=False, author=None):
    """Act on reconcile rows. A 'renamed' task's old name is renamed onto the
    live one for EVERY head, in the arc or not, target or ref (the
    `rename_target` sweep): the rename is a fact about the object, and moving
    the arc item alone split its history across two names. Status is kept
    (there is still something to check); this is evidence-backed and
    reversible, so it always runs.

    Closing 'stale' tasks is opt-in via resolve_stale. The reference
    resolves them inside plain apply with a body asserting the target was
    retired/renamed. With [renames] unconfigured — the shipping default —
    every rename classifies as stale, so that default would silently close
    real open work and report the arc as further along than it is. The
    body here says only that the target disappeared, not why.

    A stale row with `needs_result` is left open: closing a pre-registration
    needs its verdict, which the CLI reports as `needs --result`.

    `author` is the person who ran reconcile, not whoever wrote the task
    being re-targeted or closed -- forwarded to each internal supersede,
    never inherited from it.

    ONE outer `_lock` spans the whole batch, via `_supersede_unlocked`
    rather than the public `supersede` -- calling `supersede` per row would
    acquire a second `LOCK_EX` from this same process and deadlock on the
    first row (flock is per open-file-description, not per call), which is
    why an earlier version of this function held no lock at all and reloaded
    the whole notes file once per row instead.

    Returns (retargeted, repointed, resolved): rows re-targeted and rows
    whose refs were re-pointed, across the store, and stale tasks closed."""
    retargeted = repointed = resolved = 0
    require_store(store)
    kinds = K.read_kinds(store)
    side = shared_side(store)
    with _lock(store):
        notes, pending = load(store), []
        for r in rows:
            if r["status"] == "renamed":
                moved, refs = _rename_unlocked(store, notes, kinds, r["target_type"],
                                               r["target_name"], r["suggestion"], author,
                                               side=side, pending=pending)
                retargeted += moved
                repointed += refs
            elif r["status"] == "stale" and resolve_stale and not r.get("needs_result"):
                new_note = _supersede_unlocked(
                    store, notes, r["id"], author=author, kinds=kinds, side=side,
                    pending=pending, status="resolved",
                    append_body=f"reconcile: target {r['target_name']!r} disappeared "
                                f"from the {r['target_type']} catalog; no rename evidence.")
                notes.append(new_note)
                resolved += 1
        if pending:
            _rewrite_notes_unlocked(store, pending)
    return retargeted, repointed, resolved
