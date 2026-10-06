"""Note and Arc schema: dataclasses, serialization, kind/status invariants.

Lifted from a working reference: the research notebook symbion was
extracted from. Field names on disk are a contract with every store already
written: renaming one takes a migration and a dated note in the specs, as
the 2026-09-08 vocabulary change did for `activity_id` and `global`.

This module holds the schema, the locked file I/O and the arc registry.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
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
    evidence: tuple = ()              # store-relative paths, never absolute
    target_blob: str | None = None    # the target file's blob id when written (gitref.target_blob)
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
    from datetime import timedelta
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


def newest_first(rows) -> list:
    """Rows by when they were written, newest first; id breaks a same-second tie."""
    return sorted(rows, key=lambda n: (written_at(n), n.id), reverse=True)


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
        raise ValueError(f"a ref to the row's own target ({own[0]}:{own[1] or ''}) "
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
    not reach a state `add` itself would have refused. Four rules:

    1. `status` only on a kind with the status bit.
    2. `checked`/`result` only on a kind with the verdict bit.
    3. A status+verdict row (a pre-registration) cannot be `resolved` with an
       empty `result`: the resolving row IS the verdict. `d` is the whole
       resulting row, so an inherited result satisfies it.
    4. `due` only on a kind with the status bit -- only an open row can be
       past due -- and only in a form `canon_due` reads.
    5. No `checked`/`result` that is a null's spelling (`None`, `null`): the
       word passed rule 3 as a verdict (measured 2026-09-26).

    The reader never calls this: a row already on disk loads as written."""
    for key in ("checked", "result"):
        if d.get(key) in NULL_WORDS:
            raise ValueError(f"--{key} {d[key]!r} stores the word, not an absent {key}: "
                             f"omit the flag, or say what it found")
    if d.get("status") is not None and not spec.status:
        raise ValueError(f"status is not valid for kind {kind!r}: it has no status bit, so "
                         f"there is nothing to open or resolve. A plain row leaves the "
                         f"default views with `supersede <id> --add-tag retired`; "
                         f"`symbion schema` lists each kind's bits")
    if not spec.verdict and (d.get("checked") is not None or d.get("result") is not None):
        raise ValueError(f"--checked/--result are not valid for kind {kind!r}: "
                         f"it has no verdict bit; put the evidence in --body "
                         f"(`symbion schema` lists the verdict kinds)")
    if (spec.status and spec.verdict and d.get("status") == "resolved"
            and not has_result(d.get("result"))):
        raise ValueError(f"kind {kind!r} cannot be resolved without --result: it is a "
                         f"pre-registration, and the resolving row carries the verdict: "
                         f"`resolve <id> --result \"what the data said\"`")
    if d.get("due") is not None:
        if not spec.status:
            raise ValueError(f"--due is not valid for kind {kind!r}: it has no status bit, "
                             f"so it is never open and can never be past due "
                             f"(`symbion schema` lists the status kinds)")
        canon_due(d["due"])


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
    if target_types is not None and target.type not in target_types:
        raise ValueError(f"unknown target type: {target.type!r}")
    return Note(
        id=d["id"], kind=d["kind"], target=target,
        created_at=d["created_at"], author=d.get("author", "unknown"),
        body=d.get("body", ""), status=d.get("status"),
        checked=d.get("checked"), result=d.get("result"),
        arc_id=d.get("arc_id"), supersedes=d.get("supersedes"), due=d.get("due"),
        tags=tuple(d.get("tags") or ()),
        refs=tuple(r if isinstance(r, Target) else Target(type=r["type"], name=r.get("name"))
                   for r in (d.get("refs") or ())),
        provenance=d.get("provenance"),
        measurements=d.get("measurements"),
        evidence=tuple(d.get("evidence") or ()),
        target_blob=d.get("target_blob"),
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
        "evidence": list(note.evidence),
        "target_blob": note.target_blob,
    }


def _dumps(obj) -> str:
    # ensure_ascii=False keeps diacritics readable in the JSONL; names are
    # NFC-normalized on the way in, so the bytes are stable.
    return json.dumps(obj, ensure_ascii=False)


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
def _utcnow() -> datetime:
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


def new_id(_clock=None, _rand=None) -> str:
    """Microsecond-resolution, lexically-sortable id: YYYYMMDD-HHMMSS-ffffff-xxx,
    in UTC. _clock/_rand are injectable for deterministic tests."""
    clock = _clock or _utcnow
    rand = _rand or (lambda: secrets.randbelow(4096))
    now = clock()
    return f"{now.strftime('%Y%m%d-%H%M%S-%f')}-{rand():03x}"


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


@contextmanager
def _lock(store):
    """Exclusive lock over the whole store, held for one mutation.

    NEVER NEST — flock is per open-file-description, so a second LOCK_EX from
    this same process would deadlock. Acquire only at a top-level mutating entry
    point, and after require_store so the directory exists."""
    p = Path(store) / LOCK_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _replace_atomically(path: Path, body: str) -> None:
    """Write via temp + fsync + rename, so a crash leaves the old file whole."""
    tmp = path.with_name(f".tmp.{os.getpid()}.{path.name}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def require_store(store) -> None:
    """Every write's first step: only `symbion init` creates a store. A write
    that created one forked a renamed repo's, a mistyped --dir's or a stale
    SYMBION_DIR's store, printing an ordinary id (2026-09-11). A store that
    exists still gets any piece it lacks."""
    if not exists(store):
        raise ValueError(f"no store at {store}; run `symbion init`")
    ensure_store(store)


def ensure_store(store) -> bool:
    """Create the store dir + both empty files + `git init` if absent.
    Idempotent. Returns True iff it had to bootstrap anything. `init` is the
    one caller that may create; writes go through require_store."""
    store = Path(store)
    created = not store.exists()
    store.mkdir(parents=True, exist_ok=True)
    for f in (notes_path(store), arcs_path(store)):
        if not f.exists():
            f.touch()
            created = True
    if not (store / ".git").exists():
        r = subprocess.run(["git", "init", "-q", str(store)], capture_output=True, text=True)
        if r.returncode:
            # git can fail after making `.git`, and a left `.git` makes every
            # later call skip this init: remove the one this call made.
            shutil.rmtree(store / ".git", ignore_errors=True)
            said = (r.stderr + r.stdout).strip() or f"exit {r.returncode}, no output"
            raise RuntimeError(f"git init failed in {store}; git said:\n{said}")
        created = True
    ignore = store / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCK_FILE}\n.tmp.*\n", encoding="utf-8")
        created = True
    return created


# ---- notes I/O ----
def _read_notes(file: Path, kinds):
    """Returns (notes, malformed) where malformed is a list of
    (lineno, raw, error). The error travels with the row so a reader that
    skips it can still say WHY -- an un-migrated or hand-edited store must
    not read as an empty one."""
    file = Path(file)
    if not file.exists():
        return [], []
    good, bad = [], []
    for i, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            good.append(note_from_dict(json.loads(raw), kinds=kinds))
        except Exception as e:                  # malformed hand-edit can't take down a read
            bad.append((i, raw, str(e)))
    # A revision is appended after the row it revises, so one pass in file
    # order finds each chain's start; a predecessor not yet read leaves the
    # revision's own time.
    first = {}
    for i, n in enumerate(good):
        if n.supersedes in first:
            good[i] = n = replace(n, first_written=first[n.supersedes])
        first[n.id] = n.first_written or n.created_at
    return good, bad


def _load_unlocked(store):
    """Notes in file order. Callers that mutate must already hold the lock;
    `load` below is the public, lock-free read. The kinds table is read from
    the store's own symbion.toml on every load: it is one small file, and
    reading it here is what lets every consumer take bits off the row
    instead of carrying a table around."""
    return _read_notes(notes_path(store), K.read_kinds(store))[0]


def load(store):
    """All notes, in file order (oldest first). Skips malformed lines silently."""
    return _load_unlocked(store)


def load_malformed(store):
    """[(lineno, raw, error)] for lines that failed to read — for a surfaced warning."""
    return _read_notes(notes_path(store), K.read_kinds(store))[1]


def _append_line_unlocked(file, line: str) -> None:
    with open(file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _append_note_unlocked(store, note: Note) -> None:
    _append_line_unlocked(notes_path(store), _dumps(note_to_dict(note)))


def add_many(store, rows, target_types=None, canonicalize=None) -> list:
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
    The api owns that callback; the store only guarantees where it runs."""
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
        used = {n.id for n in _load_unlocked(store)}
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
        for note in notes:
            _append_note_unlocked(store, note)
    return notes


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
                             ("status", note.spec.status), ("due", note.spec.status))
            if not bit and getattr(note, k) is not None]


def _supersede_unlocked(store, notes, old_id: str, author: str | None = None,
                        kinds=None, append_body: str | None = None, add_refs=None,
                        in_place=None, prepend_body: str | None = None, **fields) -> Note:
    """The guts of `supersede`, assuming the caller already holds `_lock` and
    passes in a `notes` list loaded under that same lock. Extracted so a
    multi-row aggregate (`rename_target`, `apply_reconciliation`) can hold
    ONE outer lock spanning its whole sweep instead of nesting a fresh lock
    per row -- flock is per open-file-description, so a second LOCK_EX from
    this same process would deadlock, and reloading the whole notes file once
    per affected row is O(n^2) on a sweep of n.

    Inherits the superseded row's fields (notably `target` — never
    re-resolved, so a departed target stays editable), overridden by
    **fields, with a fresh id/created_at and supersedes=old_id. The old row
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
    old = next((n for n in notes if n.id == old_id), None)
    if old is None:
        raise KeyError(old_id)
    # Fast-forward past any supersede that already landed while this call
    # waited for the lock. Without this, two racing supersede(old_id) calls
    # both build on the literal old_id and fork the chain; walking to the
    # live tip makes the second call chain onto the first's result instead,
    # so the lock actually serializes the mutation, not just the write.
    # `seen` bounds the walk to len(notes) steps and turns a cyclic
    # supersedes chain (hand-edited, or two ordinary unvalidated add() calls)
    # into an error instead of an infinite loop held under the store-wide
    # lock.
    by_supersedes = {n.supersedes: n for n in notes if n.supersedes}
    seen = {old.id}
    while old.id in by_supersedes:
        nxt = by_supersedes[old.id]
        if nxt.id in seen:
            raise ValueError(f"cycle in supersede chain at {nxt.id}")
        seen.add(nxt.id)
        old = nxt
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
    if "refs" in fields or add_refs:
        check_self_ref(base["target"], base["refs"])
    if fields.get("due") is not None:
        base["due"] = canon_due(fields["due"])
    spec = kinds.get(base["kind"])
    if spec is None:
        raise ValueError(f"unknown kind {base['kind']!r}; this store's kinds are "
                         f"{', '.join(kinds)} (declared under [kinds] in symbion.toml)")
    if prepend_body is not None and spec.status and spec.verdict:
        raise ValueError(f"{old.id} is a pre-registration: its registered text stays on "
                         f"top, so nothing goes above it (--append adds after it)")
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
    base["supersedes"] = old.id
    base["id"] = new_id()
    base["created_at"] = _now_iso()
    # Never inherited: a correction is authored by whoever wrote the
    # correction, not by the person being corrected. The row gets a fresh
    # id and a fresh timestamp; a stale author beside them is incoherent.
    base["author"] = author or "unknown"
    note = note_from_dict(base, kinds=kinds)
    _append_note_unlocked(store, note)
    return note


def supersede(store, old_id: str, author: str | None = None, canonicalize=None,
              append_body: str | None = None, add_refs=None,
              prepend_body: str | None = None, **fields) -> Note:
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
    built: new refs resolve against the store as it is now; the inherited
    target is never in `fields` (api.supersede refuses it)."""
    require_store(store)
    with _lock(store):
        notes = _load_unlocked(store)
        if canonicalize is not None:
            fields = canonicalize(fields)
            if add_refs:
                add_refs = canonicalize({"refs": add_refs})["refs"]
        return _supersede_unlocked(store, notes, old_id, author=author,
                                   append_body=append_body, add_refs=add_refs,
                                   prepend_body=prepend_body, in_place=lambda tip: rewritable(store, notes, tip, author),
                                   **fields)


def rewritable(store, notes, tip: Note, author: str | None) -> bool:
    """Whether an edit by `author` rewrites `tip` in place instead of
    appending a revision. A wording fix 54 s after the row left two rows
    for one thought (2026-10-01): the window for an edit is the time before
    `symbion commit`. Append-only keeps a conclusion's history, and a row
    git never saw has none. So: git has not seen the row, its author is the
    editor, no other row's body cites it, and it is no pre-registration,
    whose registered text is the point. The caller also keeps the status:
    a resolve's time is when the row closed."""
    if not author or author != tip.author or (tip.spec.status and tip.spec.verdict):
        return False
    if any(n.id != tip.id and n.body and (tip.id in n.body or tip.id[-10:] in n.body)
           for n in notes):
        return False
    committed = _committed_ids(store)
    return committed is not None and tip.id not in committed


def _committed_ids(store) -> set | None:
    """The row ids in the store's last commit: none before the first one.
    None when git cannot say, so the edit appends, as every edit once did."""
    git = ["git", "-C", str(store)]
    if subprocess.run([*git, "rev-parse", "-q", "--verify", "HEAD"],
                      capture_output=True).returncode:
        return set()
    r = subprocess.run([*git, "show", f"HEAD:{NOTES_FILE}"], capture_output=True, text=True)
    return {_line_id(raw) for raw in r.stdout.splitlines()} if r.returncode == 0 else None


def _line_id(raw: str):
    try:
        return json.loads(raw).get("id")
    except (ValueError, AttributeError):
        return None


def _rewrite_note_unlocked(store, note: Note) -> None:
    """Replace the line holding note.id, whole-file and atomically: a reader
    without the lock sees the old file or the new one, never half."""
    path = notes_path(store)
    line = _dumps(note_to_dict(note))
    rows = [line if _line_id(raw) == note.id else raw
            for raw in path.read_text(encoding="utf-8").splitlines()]
    _replace_atomically(path, "\n".join(rows) + "\n")


def heads(notes):
    """Collapse supersede chains: a note is a head iff nothing supersedes it."""
    superseded = {n.supersedes for n in notes if n.supersedes}
    return [n for n in notes if n.id not in superseded]


def heads_for(store, target_type, target_name=None):
    """Current heads attached to one object, newest-first — the read-side hook
    for surfacing prior conclusions where the next investigation starts.
    heads() runs on the FULL row set before filtering, so a superseding row
    never hides from the chain collapse."""
    allheads = heads(load(store))
    rows = query(allheads, target_type=target_type, target_name=target_name)
    if target_name is not None:                    # also surface notes that REF this object
        seen = {n.id for n in rows}
        rows = rows + [n for n in allheads if n.id not in seen
                       and any(r.type == target_type and r.name == target_name for r in n.refs)]
    return newest_first(rows)


def query(notes, *, id=None, target_type=None, target_name=None, kind=None,
          status=None, arc_id=None, tag=None, author=None, structured=None, grep=None,
          overdue=None, since=None):
    """Filter raw notes by any combination of fields. Callers compose heads()
    when they want current state (see data-flow in the spec).

    `structured` is a PREDICATE over measurements, not a stored column: True
    selects `measurements is not None`, False its complement. A stored boolean
    could drift out of agreement with the data it describes, which is precisely
    the class of claim this layer exists to retract.

    `grep` is a compiled pattern searched over body, target name, checked,
    result, each ref as `type:name` and each tag as `#tag` -- the text a
    reader would otherwise pipe a paged list to grep for, losing the page's
    header and reading the silence as absence. Refs are in because SKILL.md
    tells writers to put a second object there instead of in prose. Tags
    are in because a writer tags a subject the text never names.

    `id` is an exact match on the row, not a walk of its supersede chain: it
    filters whatever set it is handed, so pass raw notes to reach a superseded
    row and heads() to reach only live ones."""
    out = notes
    if id is not None:
        out = [n for n in out if n.id == id]
    if target_type is not None:
        out = [n for n in out if n.target.type == target_type]
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
    if structured is not None:
        out = [n for n in out if (n.measurements is not None) == structured]
    if grep is not None:
        out = [n for n in out if grep.search("\n".join(
            [f for f in (n.body, n.target.name, n.checked, n.result) if f]
            + [f"{r.type}:{r.name or ''}" for r in n.refs] + [f"#{t}" for t in n.tags]))]
    if overdue:
        out = [n for n in out if is_overdue(n)]
    if since is not None:
        out = [n for n in out if written_at(n) >= since]
    return out


def tag_counts(notes):
    """{tag: count} over heads — surfaces freeform-tag drift into near-synonyms
    (e.g. refuted vs retracted) so the vocabulary can be kept tight."""
    import collections
    c = collections.Counter()
    for n in heads(notes):
        for t in n.tags:
            c[t] += 1
    return dict(c)


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
                  canonicalize=None, new_type=None) -> tuple[int, int]:
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
    `old` is never resolved: it names a departed object, which a live
    catalog match would redirect."""
    require_store(store)
    with _lock(store):
        if canonicalize is not None:
            new = canonicalize(new)
        return _rename_unlocked(store, _load_unlocked(store), K.read_kinds(store),
                                target_type, old, new, author, new_type)


def commit(store, message: str, cfg) -> bool:
    """git add -A + commit the store. Hermetic identity so it works in CI/tmp.
    The lock is here because `add -A` would otherwise stage whatever another
    writer has half-written. Returns True on a commit, False when nothing
    changed, and raises RuntimeError with git's words when git refuses: a
    hook's refusal (a secret scanner's catch) once read as 'nothing to
    commit', its output dropped (2026-09-28)."""
    store = Path(store)
    require_store(store)
    git = ["git", "-C", str(store)]
    with _lock(store):
        r = subprocess.run([*git, "add", "-A"], capture_output=True, text=True)
        if r.returncode == 0:
            r = subprocess.run([*git, "diff", "--cached", "--quiet"],
                               capture_output=True, text=True)
            if r.returncode == 0:                 # nothing staged
                return False
            if r.returncode == 1:                 # something staged
                # Every pending row goes in, whoever wrote it, so a commit
                # once named another agent's rows as its own work (2026-10-02).
                # The trailer says whose they are.
                diff = subprocess.run([*git, "diff", "--cached", "-U0", "--", NOTES_FILE],
                                      capture_output=True, text=True).stdout
                by: dict[str, int] = {}
                for line in diff.splitlines():
                    if line.startswith("+{"):
                        try:                      # a hand edit must not block a commit
                            a = json.loads(line[1:]).get("author") or "?"
                        except ValueError:
                            a = "?"
                        by[a] = by.get(a, 0) + 1
                rows = ", ".join(f"{a} {n}" for a, n in sorted(by.items()))
                r = subprocess.run(
                    [*git, "-c", f"user.name={cfg.git_name}",
                     "-c", f"user.email={cfg.git_email}", "commit", "-q", "-m", message,
                     *(["-m", f"Rows: {rows}"] if rows else [])],
                    capture_output=True, text=True)
    if r.returncode:
        said = (r.stderr + r.stdout).strip() or f"exit {r.returncode}, no output"
        raise RuntimeError(f"git refused the commit; the notes are on disk, "
                           f"not committed. git said:\n{said}")
    return True


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
    )


def arc_to_dict(act: Arc) -> dict:
    return {
        "id": act.id, "name": act.name, "description": act.description,
        "target_scope": act.target_scope, "created_at": act.created_at,
        "author": act.author, "archived": act.archived,
        "archived_at": act.archived_at,
    }


def _read_arcs(store) -> tuple[list, list]:
    """(arcs, [(lineno, raw, error)]), in file order."""
    file = arcs_path(store)
    if not file.exists():
        return [], []
    good, bad = [], []
    for i, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            good.append(arc_from_dict(json.loads(raw)))
        except Exception as e:
            bad.append((i, raw, f"missing key {e}" if isinstance(e, KeyError) else str(e)))
    return good, bad


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


def rename_arc(store, id, new_name) -> Arc:
    require_store(store)
    with _lock(store):
        acts = load_arcs(store)
        found = None
        out = []
        for a in acts:
            if a.id == id:
                found = replace(a, name=new_name)   # id preserved -> checkboxes still resolve
                out.append(found)
            else:
                out.append(a)
        if found is None:
            raise KeyError(id)
        _write_arcs_unlocked(store, out)
    return found


def archive_arc(store, id, _clock=None) -> Arc:
    clock = _clock or _utcnow
    require_store(store)
    with _lock(store):
        acts = load_arcs(store)
        found = None
        out = []
        for a in acts:
            if a.id == id:
                found = replace(a, archived=True,
                                archived_at=clock().isoformat(timespec="seconds"))
                out.append(found)
            else:
                out.append(a)
        if found is None:
            raise KeyError(id)
        _write_arcs_unlocked(store, out)
    return found


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
                         f"and a seeded row is a checklist box")
    if spec.parked:
        raise ValueError(f"kind {kind!r} cannot be seeded: it is parked, and a "
                         f"parked row is never a checklist box")
    if spec.verdict:
        raise ValueError(f"kind {kind!r} cannot be seeded: it has the verdict bit, "
                         f"and seed stamps no provenance and writes no falsifier")
    require_store(store)
    with _lock(store):                       # outer: load + mint + append
        notes = _load_unlocked(store)
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
    maps an old target name to its current one (see catalog.rename_map).

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
            live = live_for(n.target.type)
            if name in live:
                status, sugg = "live", None
            elif rename_map.get(name) in live:
                status, sugg = "renamed", rename_map[name]
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
        notes = _load_unlocked(store)
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
