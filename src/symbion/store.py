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
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field, fields as dc_fields, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import catalog  # for nfc() only -- catalog does not import store, so
                        # this does not invert the layering.

from . import kinds as K

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


def due_state(due: str, now: datetime | None = None) -> tuple[bool, int] | None:
    """(past due, calendar days from today to the due date), both at `now`'s
    offset, the display zone's by default. A date is due through the end of
    its day; a datetime is past due from that instant, so it can be past due
    with 0 days left. None for a value the writer would refuse -- a
    hand-edited row still loads."""
    now = now or now_shown()
    try:
        d = date.fromisoformat(due)
        return now.date() > d, (d - now.date()).days
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(due)
    except ValueError:
        return None
    dt = (dt if dt.tzinfo else dt.astimezone()).astimezone(now.tzinfo)
    return now >= dt, (dt.date() - now.date()).days


def is_overdue(note, now: datetime | None = None) -> bool:
    """Open and past due. False for an unreadable due, which no write mints."""
    state = due_state(note.due, now) if note.due else None
    return read_status(note) == "open" and state is not None and state[0]


def check_self_ref(target: dict, refs) -> None:
    """A ref to the row's own target adds nothing, and `context --target`
    reported the row twice. Checked on RESOLVED names: `file:cli` and
    `file:src/cli.py` are one object once the catalog has run."""
    own = (target["type"], target.get("name"))
    if any((r["type"], r.get("name")) == own for r in refs or ()):
        raise ValueError(f"a ref to the entry's own target ({own[0]}:{own[1] or ''}) "
                         f"adds nothing; drop it")


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
        raise ValueError(f"options on kind {kind!r}: only a question takes answers")


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


def note_from_dict(d: dict, target_types=None, kinds=None) -> Note:
    """`kinds` is the store's table (label -> Kind); None means the defaults.
    An undeclared label is refused with the declared set, and the three
    retired names get the migration hint on top."""
    kinds = K.DEFAULT_KINDS if kinds is None else kinds
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
        body=d.get("body", ""), status=d.get("status"),
        checked=d.get("checked"), result=d.get("result"),
        arc_id=d.get("arc_id"), supersedes=d.get("supersedes"), due=d.get("due"),
        tags=tuple(d.get("tags") or ()),
        refs=refs,
        provenance=d.get("provenance"),
        measurements=dict(sorted(m.items(), key=lambda kv: kv[0].casefold())) if m else m,
        # In the order given: the first is the default (the user, 2026-10-16).
        options=tuple(o) if (o := d.get("options")) else None,
        target_blob=d.get("target_blob"),
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
    s = (dt if dt.tzinfo else dt.astimezone()).astimezone(zone()).isoformat(sep=" ")
    return s.removesuffix("+00:00") + "Z" if s.endswith("+00:00") else s


def _now_iso() -> str:
    return _utcnow().isoformat(timespec="seconds")


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


@contextmanager
def _lock(store, undoable: bool = True):
    """Exclusive lock over the whole store, held for one mutation.

    NEVER NEST — flock is per open-file-description, so a second LOCK_EX from
    this same process would deadlock. Acquire only at a top-level mutating entry
    point, and after require_store so the directory exists.

    An `undoable` hold copies the files a write changes before it runs, and
    keeps the copy as the one undo level if the write changed them (`undo`)."""
    if read_only():
        raise ReadOnly()
    p = Path(store) / LOCK_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl:
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
        snap = _snapshot(store) if undoable else None
        try:
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


def _replace_atomically(path: Path, body: str) -> None:
    """Write via temp + fsync + rename, so a crash leaves the old file whole."""
    tmp = path.with_name(f".tmp.{os.getpid()}.{path.name}")
    try:
        # LF on every platform: Windows text mode writes CRLF, and a store
        # is shared by clones on all of them.
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
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
    """Copy the files a write may change into the store's git dir, where git
    never sees them. A copy, not `git add`: each add left a blob of the whole
    file that nothing references, over a quarter of its size, and git
    prunes those after two weeks (measured 2026-10-13; a copy took 5-8 ms).
    A failed copy costs the undo, never the write."""
    sess = _sessions_path(store)
    if sess is None:
        return None
    new = sess.with_name(UNDO_DIR + ".new")
    try:
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


def undoable(store) -> bool:
    """Can this session take back the store's last write: one this session
    made (none, for a person), the files as it left them? The GUI's undo
    button reads it on every page and every few seconds, so it reads no
    rows: `undo` checks the author on the click."""
    try:
        last = _undo_level(store)[2]
    except UndoRefused:
        return False
    return last.get("session", "") == (session_key() or "")


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
                              f"only the last write, which was {undo_words(out)}")
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
# file -> (text, kinds, notes, malformed), its last parse. A serve page parsed
# the store two or three times and `summary` twice, at 40 ms a parse on a large
# one (the user, 2026-10-09). The file is read every time and its parse kept only for an
# equal text, so no write, git checkout or hand edit can leave it stale.
# ponytail: one entry per store path a process reads, each a copy of the file.
_PARSED: dict = {}


def _parse_jsonl(text: str, parse) -> tuple[list, list]:
    """(parse(row) for each line, [(lineno, raw, error)]). The error travels
    with the row so a reader that skips it can still say WHY: an un-migrated
    or hand-edited store must not read as an empty one. A second line with
    an id already read is skipped too: two copies of a store that edit one
    row, merged keeping both sides, give two heads one id (2026-10-13)."""
    good, bad, seen = [], [], {}
    for i, raw in enumerate(jsonl_lines(text), 1):
        if not raw.strip():
            continue
        try:
            row = parse(json.loads(raw))
        except Exception as e:                  # malformed hand-edit can't take down a read
            bad.append((i, raw, f"missing key {e}" if isinstance(e, KeyError) else str(e)))
            continue
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


def _read_notes(file: Path, kinds):
    """(notes, [(lineno, raw, error)]). Each call gets its own lists:
    callers append."""
    file = Path(file)
    if not file.exists():
        return [], []
    text = file.read_text(encoding="utf-8")
    hit = _PARSED.get(file)
    if hit and hit[0] == text and hit[1] == kinds:
        return list(hit[2]), list(hit[3])
    good, bad = _parse_jsonl(text, lambda d: note_from_dict(d, kinds=kinds))
    # A revision is appended after the row it revises, so one pass in file
    # order finds each chain's start; a predecessor not yet read leaves the
    # revision's own time.
    first = {}
    for i, n in enumerate(good):
        if n.supersedes in first:
            good[i] = n = replace(n, first_written=first[n.supersedes])
        first[n.id] = n.first_written or n.created_at
    _PARSED[file] = (text, kinds, good, bad)
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
    _append_line_unlocked(notes_path(store), _dumps(note_to_dict(note)))
    _record_session(store, note.id)


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
    with _lock(store):                       # outer: covers canonicalize + mint + write
        # ponytail: lock held across canonicalize = rows x command_timeout worst
        # case; a per-row release would let a neighbour mint between rows, so
        # the ceiling stays until it hurts
        if canonicalize is not None:
            rows = canonicalize(rows)
        for fields in rows:
            check_self_ref(fields["target"], fields.get("refs"))
        existing = load(store)
        used = {n.id for n in existing}
        notes = []
        for fields in rows:
            fields = dict(fields)
            fields.setdefault("id", _mint_unique(used))
            fields.setdefault("created_at", _now_iso())
            fields.setdefault("author", "unknown")
            fields.setdefault("body", "")
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
    by_supersedes = {n.supersedes: n for n in notes if n.supersedes}
    cur = next((n for n in notes if n.id == note_id), None)
    if cur is None:
        raise KeyError(note_id)
    seen = {cur.id}
    while cur.id in by_supersedes:
        cur = by_supersedes[cur.id]
        if cur.id in seen:
            raise ValueError(f"cycle in supersede chain at {cur.id}")
        seen.add(cur.id)
    return cur


def _supersede_unlocked(store, notes, old_id: str, author: str | None = None,
                        kinds=None, append_body: str | None = None, add_refs=None,
                        in_place=None, prepend_body: str | None = None,
                        add_measurements=None, **fields) -> Note:
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
    may omit it.

    A caller that will look up further ids in the same transaction must
    append the returned Note to its `notes` list itself -- this function
    does not mutate the list it was given.

    `append_body` adds to the TIP's body, read here under the caller's lock,
    so a revision that landed after the caller read the row is kept.
    `prepend_body` goes above it, the same way, as a new lead; a
    pre-registration refuses it, since its registered text stays on top.

    `in_place(tip) -> bool`, when given, may turn the append into a rewrite
    of the tip's own line; `supersede` passes `rewritable`. A sweep passes
    none and always appends."""
    for added in (append_body, prepend_body):
        if added is None:
            continue
        if "body" in fields or (append_body is not None and prepend_body is not None):
            raise ValueError("body, append_body and prepend_body are sources for one field")
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
    if add_refs:
        # Onto the TIP's refs, as append_body is onto its body; the
        # inherited ones are never re-resolved.
        have = list(base.get("refs") or ())
        base["refs"] = have + [r for r in add_refs if r not in have]
    if "refs" in fields or add_refs or "target" in fields:
        check_self_ref(base["target"], base["refs"])
    if add_measurements:
        # Onto the TIP's too: a name given replaces that name only.
        base["measurements"] = {**(base.get("measurements") or {}), **add_measurements}
    if fields.get("due") is not None:
        base["due"] = canon_due(fields["due"])
    spec = kinds.get(base["kind"])
    if spec is None:
        raise ValueError(f"unknown kind {base['kind']!r}; this store's kinds are "
                         f"{', '.join(kinds)} (declared under [kinds] in symbion.toml)")
    # Parked may change, so a task parks as an idea in place. Across the other
    # bits a note became a task with no status, and a task a pre-registration
    # with no registered run and no stamp (2026-10-14).
    if base["kind"] != old.kind and \
            (spec.status, spec.verdict) != (old.spec.status, old.spec.verdict):
        raise ValueError(f"{old.kind} to {base['kind']}: a kind change keeps the status and "
                         f"verdict bits, and these differ (`symbion schema` lists each kind's "
                         f"bits); a different thing is a new entry")
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
            raise ValueError(f"{old.id} is a pre-registration: its registered text and each "
                             f"committed amendment stay as written, so a body may change only "
                             f"text added since the last `symbion commit` (--append adds "
                             f"after it)")
        if base.get("checked") != old.checked:
            raise ValueError(f"{old.id} is a pre-registration: --checked names its registered "
                             f"run and stays as written; a different run is a new entry")
    # A legacy row (a hand-written migration, or a kind whose bits changed)
    # can carry a field its kind cannot hold. The reader loads it as written;
    # the WRITER owns the invariants, so an INHERITED illegal field is dropped
    # from the new row rather than refused with a message naming flags the
    # caller never passed (measured 2026-09-22: a `checked` on a `note` blocked
    # every later supersede). The same field passed in `fields` still refuses,
    # and so does a KIND CHANGE that would shed a field: that is not a legacy
    # row, it is the caller moving a row somewhere its fields do not fit.
    dropped = [k for k in unholdable(old) if k not in fields] if base["kind"] == old.kind else []
    for key in dropped:
        base[key] = None
    for key in ("checked", "result"):          # rule 5, on a legacy row
        if key not in fields and base.get(key) in NULL_WORDS:
            base[key] = None
    check_fields(base["kind"], spec, base)
    if in_place is not None and not dropped and base.get("status") == old.status \
            and in_place(old):
        # Keeps the id, the time and the row it revised: see `rewritable`.
        # A status change never comes here: a resolve's time is when it closed.
        # Nor a drop: the old row is then the only copy of what was dropped.
        note = note_from_dict(base, kinds=kinds)
        _rewrite_note_unlocked(store, note)
        return note
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
    note = note_from_dict(base, kinds=kinds)
    _rewrite_note_unlocked(store, note, kept_id=kept.id)
    notes[notes.index(old)] = kept      # a sweep reads this list for its next row
    return note


class RefsDropped(ValueError):
    """A write that would drop refs the entry holds, under `keep_refs`.
    `dropped` is the tip's Targets the new refs leave out."""
    def __init__(self, dropped):
        super().__init__("would drop " + ", ".join(f"{t.type}:{t.name}" for t in dropped))
        self.dropped = dropped


def supersede(store, old_id: str, author: str | None = None, canonicalize=None,
              append_body: str | None = None, add_refs=None,
              prepend_body: str | None = None, add_measurements=None, target_types=None,
              keep_refs: bool = False, **fields) -> Note:
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
    never an inherited one. `keep_refs` refuses new refs that leave out one
    the tip holds (RefsDropped), compared once the names resolve: a writer
    who meant to add a ref dropped the others (more than once by 2026-10-15)."""
    require_store(store)
    with _lock(store):
        notes = load(store)
        if canonicalize is not None:
            fields = canonicalize(fields)
            if add_refs:
                add_refs = canonicalize({"refs": add_refs})["refs"]
        check_types([o for o in (fields.get("target"), *(fields.get("refs") or ()),
                                 *(add_refs or ())) if o], target_types)
        if keep_refs and fields.get("refs") is not None:
            now = {(r.type, r.name) if isinstance(r, Target) else (r["type"], r.get("name"))
                   for r in fields["refs"]}
            if gone := [r for r in chain_tip(notes, old_id).refs if (r.type, r.name) not in now]:
                raise RefsDropped(gone)
        return _supersede_unlocked(store, notes, old_id, author=author,
                                   append_body=append_body, add_refs=add_refs,
                                   prepend_body=prepend_body, in_place=lambda tip: rewritable(store, notes, tip, author),
                                   add_measurements=add_measurements, **fields)


def rewritable(store, notes, tip: Note, author: str | None) -> bool:
    """Whether an edit by `author` rewrites `tip` in place instead of
    appending a revision. A wording fix 54 s after the row left two rows
    for one thought (2026-10-01): the window for an edit is the time before
    `symbion commit`. Append-only keeps a conclusion's history, and a row
    git never saw has none. So: git has not seen the row, its author is the
    editor, no other row's body cites it, and it is no pre-registration,
    whose registered text is the point. The caller also keeps the status:
    a resolve's time is when the row closed."""
    return not citing(notes, tip) and own_draft(store, tip, author)


def own_draft(store, tip: Note, author: str | None) -> bool:
    """Every condition of `rewritable` but the citations: `author` wrote
    `tip` in this session (or both outside any), git has not seen it, and
    it is no pre-registration."""
    if not author or author != tip.author or (tip.spec.status and tip.spec.verdict):
        return False
    if sessions(store).get(tip.id) != session_key():
        return False
    committed = _committed_lines(store)
    if committed is None:
        return False
    # By line, not by id: an edit keeps the row's id, so an id git has seen
    # can hold an edit it has not.
    lines = jsonl_lines(notes_path(store).read_text(encoding="utf-8"))
    return not any(tip.id in raw and raw in committed and _line_id(raw) == tip.id
                   for raw in lines)


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


def citing(notes, tip: Note) -> list[str]:
    """The ids of the other rows whose body cites `tip`, whole or by the
    10-character tail SKILL.md says to cite."""
    return [n.id for n in notes
            if n.id != tip.id and n.body and (tip.id in n.body or tip.id[-10:] in n.body)]


def _committed_lines(store) -> set | None:
    """The lines of notes.jsonl in the store's last commit: none before the
    first one. None when git cannot say, so the edit keeps a version, as
    every edit once did."""
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
# session key can. The record lives in the store's git dir, so no commit
# takes it, and a commit forgets the rows it took.
SESSIONS_FILE = "symbion-sessions"


def session_key() -> str | None:
    """The agent session this process runs in, when its runtime names one.
    Codex first: one launched from Claude Code inherits both keys, and the
    innermost agent is the one writing (as in api.author_default)."""
    return os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def _sessions_path(store) -> Path | None:
    r = subprocess.run(["git", "-C", str(store), "rev-parse", "--git-path", SESSIONS_FILE],
                       capture_output=True, text=True, encoding="utf-8")
    return Path(store, r.stdout.strip()) if r.returncode == 0 else None


def _record_session(store, note_id: str, kept_id: str | None = None,
                    prior: str | None = None) -> None:
    """`note_id` is this session's now. An edit's `kept_id` stays with the
    session that wrote the version it holds (`prior`), and an edit outside
    any session takes the row from the session that had it."""
    key = session_key()
    lines = ([f"{kept_id}\t{prior}"] if kept_id and prior else []) + \
        ([f"{note_id}\t{key or ''}"] if key or prior else [])
    path = lines and _sessions_path(store)
    for line in lines if path else []:
        _append_line_unlocked(path, line)


def sessions(store) -> dict:
    """{row id: session key} for the pending rows an agent session wrote."""
    path = _sessions_path(store)
    try:
        text = path.read_text(encoding="utf-8") if path else ""
    except FileNotFoundError:
        return {}
    known = dict(ln.split("\t", 1) for ln in text.splitlines() if "\t" in ln)
    return {i: k for i, k in known.items() if k}


def own_rows(store, author: str):
    """keep() for `commit`: the caller's own pending rows. In an agent
    session, the rows it wrote, as whoever (a row in the user's words is written as the
    person); with no session, the author's rows that no session wrote."""
    known, key = sessions(store), session_key()
    if key:
        return lambda r: known.get(r.get("id")) == key
    return lambda r: r.get("author") == author and r.get("id") not in known


def _forget_committed(store) -> None:
    """Keep the session of each row still pending, and no other."""
    known = sessions(store)
    committed = _committed_lines(store) if known else None
    if committed is None:
        return
    live = {_line_id(ln) for ln in jsonl_lines(notes_path(store).read_text(encoding="utf-8"))
            if ln and ln not in committed}
    _replace_atomically(_sessions_path(store),
                        "".join(f"{i}\t{k}\n" for i, k in known.items() if i in live))


def _rewrite_note_unlocked(store, note: Note, kept_id: str | None = None) -> None:
    """Replace the line holding note.id, whole-file and atomically: a reader
    without the lock sees the old file or the new one, never half. With
    `kept_id`, that line stays as written but under `kept_id`, and the note
    is appended: one write, so no crash leaves the id on neither line.
    # ponytail: a sweep rewrites the file once per row; write a sweep's rows
    # in one pass if a rename over a large store gets slow."""
    path = notes_path(store)
    raws = jsonl_lines(path.read_text(encoding="utf-8"))
    at = [str(i) for i, raw in enumerate(raws, 1) if _line_id(raw) == note.id]
    if len(at) > 1:                 # a merge kept both sides (_parse_jsonl)
        raise ValueError(f"{note.id} is on lines {', '.join(at[:-1])} and {at[-1]} of "
                         f"{NOTES_FILE}, and this write would change each. Change the last "
                         f"3 characters of one line's id to other hex digits, then write again")
    prior = sessions(store).get(note.id)
    line = _dumps(note_to_dict(note))
    rows = [(line if kept_id is None else _dumps({**json.loads(raw), "id": kept_id}))
            if _line_id(raw) == note.id else raw for raw in raws]
    _replace_atomically(path, "\n".join(rows + ([line] if kept_id else [])) + "\n")
    _record_session(store, note.id, kept_id, prior)


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
        out = [n for n in out if n.kind == kind]
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
                     new_type=None) -> tuple[int, int]:
    """Move every HEAD off (target_type, old): its target, and each ref naming
    it, onto (new_type, new), one supersede per row; `new_type` defaults to
    `target_type`. The other refs keep their place.
    Returns (re-targeted, re-pointed) row counts; a row carrying both counts in
    each. The caller holds the lock and passes `notes` loaded under it; the new
    rows are appended to that list.

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
        # this rename would make (target old, ref new); either would trip
        # check_self_ref partway through the sweep.
        refs = tuple(r for r in dict.fromkeys(now if r == was else r for r in note.refs)
                     if r != target)
        fields = {}
        if target != note.target:
            fields["target"] = {"type": now.type, "name": new}
        if refs != note.refs:
            fields["refs"] = [{"type": r.type, "name": r.name} for r in refs]
        if fields:
            notes.append(_supersede_unlocked(store, notes, note.id, author=author,
                                             kinds=kinds, **fields))
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
    with _lock(store):
        if canonicalize is not None:
            new = canonicalize(new)
        notes = load(store)
        if target_type == "commit":
            old = _stored_commit(notes, old)
        return _rename_unlocked(store, notes, K.read_kinds(store),
                                target_type, old, new, author, new_type)


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
    with _lock(store):
        notes, kinds = load(store), K.read_kinds(store)
        names = sorted({o.name for n in heads(notes) for o in (n.target, *n.refs)
                        if o.type == from_type and o.name is not None})
        moved = repointed = 0
        # ponytail: one sweep per name, so a row holding two names of the type
        # gets two versions; one supersede per row if a converted store's
        # history reads noisy. Each sweep sees the last: _rename_unlocked
        # appends what it writes to `notes`.
        for name in names:
            m, r = _rename_unlocked(store, notes, kinds, from_type, name, name, author, to_type)
            moved, repointed = moved + m, repointed + r
        return moved, repointed


def added_rows(diff: str) -> list[dict]:
    """The rows a notes.jsonl diff adds, an in-place rewrite included. A
    line that does not parse is `{}`: a hand edit must not block a commit.
    A version an edit kept is not one: it is a removed line under a new id."""
    rows = {"+": [], "-": []}
    for line in jsonl_lines(diff):
        if line[:2] in ("+{", "-{"):
            try:
                row = json.loads(line[1:])
            except ValueError:
                row = None
            rows[line[0]].append(row if isinstance(row, dict) else {})
    kept = {_sans_id(r) for r in rows["-"]}
    return [r for r in rows["+"] if not r or _sans_id(r) not in kept]


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


def pending_rows(store) -> list[dict]:
    """What `commit` would take from notes.jsonl: the rows added or
    rewritten since HEAD, or every row before the first commit. Plumbing:
    a porcelain `git diff` rewrites the index under index.lock when a file's
    stat data is stale, and a `symbion commit`'s `git add` beside it refused
    while the GUI polled this (2026-10-16)."""
    git = ["git", "-C", str(store)]
    if subprocess.run([*git, "rev-parse", "-q", "--verify", "HEAD"],
                      capture_output=True).returncode:
        f = Path(store) / NOTES_FILE
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        return added_rows("".join(f"+{ln}\n" for ln in jsonl_lines(text)))
    return added_rows(subprocess.run([*git, "diff-index", "-p", "-U0", "HEAD", "--", NOTES_FILE],
                                     capture_output=True, text=True, encoding="utf-8").stdout)


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
    wrap commits even with nothing staged."""
    store = Path(store)
    require_store(store)
    git = ["git", "-C", str(store)]
    with _lock(store, undoable=False):
        _forget_undo(store)          # after a commit there is nothing to undo
        r = (subprocess.run([*git, "add", "-A"], capture_output=True, text=True, encoding="utf-8")
             if keep is None else _stage_kept(store, keep))
        if r.returncode == 0 and keep is not None and files:
            r = subprocess.run([*git, "add", "-A", "--", ".", f":(exclude){NOTES_FILE}"],
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
                return False
            if r.returncode in (0, 1):
                # A commit once named another agent's rows as its own work
                # (2026-10-02), and `--all` still takes them. The trailer
                # says whose they are.
                diff = subprocess.run([*git, "diff", "--cached", "-U0", "--", NOTES_FILE],
                                      capture_output=True, text=True, encoding="utf-8").stdout
                rows = rows_by_author(added_rows(diff))
                trailers = ([f"Rows: {rows}"] if rows else []) + ([f"Wrap: {wrap}"] if wrap else [])
                # In UTC by the one clock, as the rows are: git's own read the
                # local zone, and ignored a stopped clock. A date the caller
                # set in git's own variables still wins.
                now = _now_iso()
                r = subprocess.run(
                    [*git, "-c", f"user.name={cfg.git_name}",
                     "-c", f"user.email={cfg.git_email}", "commit", "-q",
                     *(["--allow-empty"] if empty else []), "-m", message,
                     *(["-m", "\n".join(trailers)] if trailers else [])],
                    capture_output=True, text=True, encoding="utf-8",
                    env={"GIT_AUTHOR_DATE": now, "GIT_COMMITTER_DATE": now, **os.environ})
                if r.returncode == 0:
                    _forget_committed(store)
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
    olds = {_sans_id(r): r.get("id") for r in (added_rows(f"+{ln}")[0] for ln in old) if r}
    moved = {olds[_sans_id(r)]: ln for ln, r in pending if r and _sans_id(r) in olds}
    kept_ids = {_line_id(ln) for ln in moved.values()}
    taken = with_bases([r for _, r in pending], lambda r: keep(r) and r.get("id") not in kept_ids)
    take = {r["id"] for _, r in pending if r.get("id") and taken(r)}
    old_ids = [_line_id(ln) for ln in old]
    head_rows = [moved[i] if i in take and i in moved else ln for ln, i in zip(old, old_ids)]
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
    return Arc(
        id=d["id"], name=d["name"], description=d.get("description", ""),
        target_scope=d["target_scope"], created_at=d["created_at"],
        author=d.get("author", "unknown"),
        archived=bool(d.get("archived", False)),
        archived_at=d.get("archived_at"),
        extra={k: v for k, v in d.items() if k not in _ARC_KEYS} or None,
    )


def arc_to_dict(act: Arc) -> dict:
    return {
        "id": act.id, "name": act.name, "description": act.description,
        "target_scope": act.target_scope, "created_at": act.created_at,
        "author": act.author, "archived": act.archived,
        "archived_at": act.archived_at, **(act.extra or {}),
    }


def _read_arcs(store) -> tuple[list, list]:
    """(arcs, [(lineno, raw, error)]), in file order."""
    file = arcs_path(store)
    if not file.exists():
        return [], []
    return _parse_jsonl(file.read_text(encoding="utf-8"), arc_from_dict)


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
                       author=author, archived=False, archived_at=None)
        _write_arcs_unlocked(store, acts + [act])
    return act


def _edit_arc(store, id, change) -> Arc:
    """Arc `id` as `change(arc)` returns it, written under the lock; KeyError
    if there is none. The id stays, so its rows' arc_id still resolve."""
    require_store(store)
    with _lock(store):
        acts = load_arcs(store)
        old = next((a for a in acts if a.id == id), None)
        if old is None:
            raise KeyError(id)
        new = change(old)
        if new != old:
            _write_arcs_unlocked(store, [new if a.id == id else a for a in acts])
    return new


def rename_arc(store, id, new_name) -> Arc:
    return _edit_arc(store, id, lambda a: replace(a, name=new_name))


def archive_arc(store, id, _clock=None) -> Arc:
    """An arc archived already is returned as it is: archiving it again
    moved `archived_at`, the date it left the summary."""
    clock = _clock or _utcnow
    return _edit_arc(store, id, lambda a: a if a.archived else replace(
        a, archived=True, archived_at=clock().isoformat(timespec="seconds")))


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
    with _lock(store):
        notes = load(store)
        for r in rows:
            if r["status"] == "renamed":
                moved, refs = _rename_unlocked(store, notes, kinds, r["target_type"],
                                               r["target_name"], r["suggestion"], author)
                retargeted += moved
                repointed += refs
            elif r["status"] == "stale" and resolve_stale and not r.get("needs_result"):
                new_note = _supersede_unlocked(
                    store, notes, r["id"], author=author, kinds=kinds, status="resolved",
                    append_body=f"reconcile: target {r['target_name']!r} disappeared "
                                f"from the {r['target_type']} catalog; no rename evidence.")
                notes.append(new_note)
                resolved += 1
    return retargeted, repointed, resolved
