"""The read side. A store Claude writes to and never reads is a pacifier."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from . import gitref, kinds as K, store as S

ARC_CAP = 10
PRIORITY_CAP = 10
HEAD_CAP = 8
PREREG_CAP = 10
DUE_CAP = 10
DUE_DAYS = 7      # how far ahead the due block looks
HEAD_CHARS = 100
BODY_CHARS = 200
# A read error is not a note body: the actionable half of the retired-kind hint
# is the remedy at its END, and BODY_CHARS cut it mid-word. Still capped --
# the refused `kind` is attacker-controlled text straight off disk.
ERROR_CHARS = 400
NAME_CHARS = 120
# `list` text: rows shown by default, and the one line a body clips to. The
# bare command was 512 lines / 88 KB on one store (2026-09-21). `--json`
# is never capped by default: the agent indexes that array whole.
LIST_CAP = 25
LIST_BODY_CHARS = 160
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# `**x**`, `__x__`, and `*x*`/`_x_` when the marker is not inside a word, so
# `arc_id` and `'*.py'` survive. Group 2 or 4 is the wrapped text.
_EMPHASIS = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"
                       r"|(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\3(?!\w)")


def flatten(s) -> str:
    """Control characters to a single space. Applied to EVERY interpolated
    field — an arc name can carry a newline too."""
    return _CONTROL.sub(" ", s or "").strip()


def clip(s, n: int) -> str:
    """One line of at most `n` chars for a note's text: flattened, markdown
    emphasis dropped, and a cut made at a word boundary with an ellipsis so
    a cut reads as a cut. The ONE place every capped field routes through --
    ERROR_CHARS grew out of a mid-word cut at one call site while the shared
    truncation kept doing it (measured 2026-09-20: one head rendered
    its asterisks literal, another ended mid-word). `--body` is documented as markdown, so
    emphasis keeps arriving."""
    s = _EMPHASIS.sub(lambda m: m.group(2) or m.group(4), flatten(s))
    if len(s) <= n:
        return s
    cut = s.rfind(" ", n // 2, n)      # a word boundary in the back half, else hard
    return s[:cut if cut > 0 else n - 1].rstrip() + "…"


def age_days(created_at: str, now: datetime | None = None) -> int:
    """Whole days since an ISO stamp. A stamp without an offset (rows older
    than store._now_iso's offset) is read as local time, the same clock that
    wrote it."""
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.astimezone()
    return max(0, ((now or datetime.now().astimezone()) - created).days)


def due_phrase(state) -> str:
    """`store.due_state`'s pair as words. Past due with 0 days left is a
    datetime that passed earlier today."""
    past, days = state
    if past:
        return f"overdue {-days}d" if days < 0 else "overdue today"
    return "due today" if days == 0 else f"due in {days}d"


def open_notes(notes, kind):
    """Heads of one kind that are still open."""
    return [n for n in notes if n.kind == kind and S.read_status(n) == "open"]


def no_status(notes):
    """Heads on a status kind carrying no status at all -- neither open nor
    resolved, so invisible to every status view.

    `add` cannot mint one (store.default_status_for), so these come from a
    write that bypassed symbion: a hand-written JSONL migration, which is the
    only faithful way to move a store with supersede chains. read_status used
    to guess "open" for them, which turned a hand-migrated store's finished
    results into a permanent phantom backlog. Reported OUTSIDE any import
    branch, so a store that still holds them says so on EVERY run, not once
    on the run that made them."""
    return [n for n in notes if n.spec.status and n.status is None]


def open_outside_arcs(notes, kind):
    """Open heads of one kind belonging to no arc -- an arc's rows, of every
    kind, are tracked on its own checklist and reached through its progress
    line, not here."""
    return [n for n in open_notes(notes, kind) if n.arc_id is None]


def due_soon(notes, now=None):
    """[(note, due_state)] for open rows past due or due within DUE_DAYS,
    soonest first. In an arc or not, parked or not: a date is an explicit ask
    to be reminded, and past due outranks every other block."""
    out = []
    for n in notes:
        state = S.due_state(n.due, now) if n.due and S.read_status(n) == "open" else None
        if state and (state[0] or state[1] <= DUE_DAYS):
            out.append((n, state))
    out.sort(key=lambda p: (p[1][1], not p[1][0], p[0].id))
    return out


def starred(notes):
    """#priority notes that have not been explicitly closed.

    Deliberately `read_status != "resolved"` and NOT `status == "open"`:
    read_status is None for note/decision/check, so an == "open" test would
    make a starred decision unstarrable, and SKILL.md presents
    `supersede <id> --add-tag priority` as how ANY note gets starred.
    "not resolved" is the correct direction: only an explicit close should
    drop it from here."""
    return [n for n in notes
            if "priority" in n.tags and S.read_status(n) != "resolved"]


# The empty state is the onboarding surface:
# it prints while the store holds zero rows and never again, so it is bounded
# by store state, not by a session the tool does not keep.
FIRST_CONTACT = ('no notes yet: symbion add --kind task --type project --body "..."  '
                 '(agent surface: the symbion skill, ~/.claude/skills/symbion/SKILL.md)')


def empty_summary() -> dict:
    """The `--json` shape for a store that has never been created (spec:251).
    The TEXT path printing nothing there is correct and depended on by the
    SessionStart hook -- but a programmatic `--json` consumer needs valid
    JSON, not an empty string, so this mirrors the zero-valued shape
    `summary()` itself returns for an empty-but-initialized store."""
    return {
        "store": None,
        "open": {},
        "arcs": [],
        "arcs_elided": 0,
        "priority": [],
        "priority_elided": 0,
        "heads": [],
        "heads_elided": 0,
        "due": [],
        "due_elided": 0,
        "uncommitted_notes": 0,
        "registry_modified": False,
        "unreadable": 0,
        "unreadable_first": None,
        "no_status": 0,
        "no_status_first": None,
        "full": False,
        "rows": 0,
    }


def summary(store_dir, cfg, full=False, _now=None) -> dict:
    loaded = S.load(store_dir)
    notes = S.heads(loaded)
    bad = S.load_malformed(store_dir)
    acts = [a for a in S.load_arcs(store_dir) if not a.archived]
    kinds = K.read_kinds(store_dir)
    open_counts = {label: len(open_outside_arcs(notes, label))
                   for label, k in kinds.items() if k.status and not k.parked}
    # Each row prints once: in the due block, else the priority block, else
    # the heads. Otherwise priority rows repeat among the heads.
    due = due_soon(notes, _now)
    shown_above = {n.id for n, _ in due}
    priority = [n for n in starred(notes) if n.id not in shown_above]
    shown_above |= {n.id for n in priority}
    unstated = no_status(notes)
    heads = [n for label, k in kinds.items() if k.status and not k.parked
             for n in open_outside_arcs(notes, label) if n.id not in shown_above]
    heads.sort(key=lambda n: n.created_at, reverse=True)
    # A pre-registration (status + verdict) has a deadline, and newest-first
    # under one cap pushed an open prediction off the list for many sessions
    # while its answer went into a handoff file (2026-09-24). Its own cap,
    # ahead of the rest, keeps the ceiling.
    prereg = [n for n in heads if n.spec.verdict]
    rest = [n for n in heads if not n.spec.verdict]
    shown = prereg + rest if full else prereg[:PREREG_CAP] + rest[:HEAD_CAP]

    rows = []
    for a in acts:
        done, total = S.arc_progress(notes, a.id)
        # Age, because `0/10` on a 31-minute-old arc read as stalled and a
        # dead one reads the same (measured 2026-09-20).
        rows.append({"id": a.id, "done": done, "total": total,
                     "name": clip(a.name, NAME_CHARS), "open": total - done,
                     "created_at": a.created_at, "age_days": age_days(a.created_at, _now)})
    rows.sort(key=lambda r: (-r["open"], r["id"]))

    a_cap = len(rows) if full else ARC_CAP
    p_cap = len(priority) if full else PRIORITY_CAP
    d_cap = len(due) if full else DUE_CAP
    notes_n, registry = gitref.uncommitted(store_dir)
    return {
        # The resolved path, so a consumer can gate on "a store exists" and
        # see which one. An absent store answers `empty_summary()`, where this
        # is None; without it the two printed byte-identical JSON at exit 0.
        "store": str(store_dir),
        "open": open_counts,
        "arcs": rows[:a_cap],
        "arcs_elided": max(0, len(rows) - a_cap),
        "priority": [{"id": n.id,
                      "target": f"{n.target.type}:{clip(n.target.name, NAME_CHARS)}",
                      "kind": n.kind,
                      "body": clip(n.body, BODY_CHARS)}
                     for n in priority[:p_cap]],
        "priority_elided": max(0, len(priority) - p_cap),
        "heads": [{"id": n.id, "kind": n.kind,
                   "target": f"{n.target.type}:{clip(n.target.name, NAME_CHARS)}",
                   "body": clip(n.body, HEAD_CHARS)}
                  for n in shown],
        "heads_elided": len(heads) - len(shown),
        "due": [{"id": n.id, "kind": n.kind,
                 "target": f"{n.target.type}:{clip(n.target.name, NAME_CHARS)}",
                 "body": clip(n.body, BODY_CHARS), "due": n.due,
                 "past": past, "days": days, "phrase": due_phrase((past, days))}
                for n, (past, days) in due[:d_cap]],
        "due_elided": max(0, len(due) - d_cap),
        "uncommitted_notes": notes_n,
        "registry_modified": registry,
        "unreadable": len(bad),
        "unreadable_first": (f"line {bad[0][0]}: {flatten(bad[0][2])[:ERROR_CHARS]}"
                             if bad else None),
        "no_status": len(unstated),
        "no_status_first": unstated[0].id if unstated else None,
        "full": full,
        "rows": len(loaded),
    }


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def render_summary(d: dict) -> str:
    counts = ", ".join(f"{label} {n}" for label, n in d["open"].items())
    out = [f"symbion: open outside arcs: {counts or 'no status kinds declared'}"]
    for r in d.get("due", ()):
        out.append(f"  {r['phrase']} [{r['kind']}] {r['target']}  {r['body']}  {r['id']}")
    if d.get("due_elided"):
        out.append(f"  +{d['due_elided']} more due (--full)")
    w = max((len(r["id"]) for r in d["arcs"]), default=0)
    pw = max((len(f"{r['done']}/{r['total']}") for r in d["arcs"]), default=0)
    for r in d["arcs"]:
        prog = f"{r['done']}/{r['total']}"
        age = f"{r['age_days']}d" if "age_days" in r else ""
        out.append(f"  {r['id']:{w}} {prog:<{pw}}  {age:>4}  {r['name']}")
    # Each elided line names the flag that lifts it, as list's header does
    # with `(--all)`: a bare `+21 more open` sent a reader off to
    # `list --status open --json` and back (reported 2026-09-22).
    if d["arcs_elided"]:
        out.append(f"  +{d['arcs_elided']} more (--full)")
    # Labelled, not starred: the header counts open rows OUTSIDE arcs and
    # this block is every unresolved `priority` row, in an arc or not -- a
    # bare `*` beside a header that disagreed with it read as a bug in the
    # tool at first sight (measured 2026-09-20).
    for p in d["priority"]:
        out.append(f"  priority [{p['kind']}] {p['target']}  {p['body']}  {p['id']}")
    if d["priority_elided"]:
        out.append(f"  +{d['priority_elided']} more priority (--full)")
    for h in d.get("heads", ()):
        out.append(f"  [{h['kind']}] {h['target']}  {h['body']}  {h['id']}")
    if d.get("heads_elided"):
        out.append(f"  +{d['heads_elided']} more open (--full)")
    if d["uncommitted_notes"] or d["registry_modified"]:
        bits = []
        if d["uncommitted_notes"]:
            bits.append(f"{d['uncommitted_notes']} notes uncommitted")
        if d["registry_modified"]:
            bits.append("registry modified")
        out.append("  " + ", ".join(bits))
    if d.get("leftovers"):
        # Imported late: cli owns the paths, and imports this module.
        from .cli import _old_copies_line
        out.append("  " + _old_copies_line(d["leftovers"]))
    if d["unreadable"]:
        out.append(f"  {_count(d['unreadable'], 'row')} unreadable, "
                   f"first: {d['unreadable_first']}")
    if d.get("no_status"):
        # The first id and the verb, as the unreadable line does: a bare
        # count had no consumer and sat as furniture for a week.
        n = d["no_status"]
        out.append(f"  {_count(n, 'row')} on a status kind {'carries' if n == 1 else 'carry'} "
                   f"no status (a write that bypassed symbion): counted neither open nor "
                   f"resolved; first {d.get('no_status_first')} -- "
                   f"supersede <id> --status open|resolved")
    if d.get("full") and not (d["arcs_elided"] or d["priority_elided"] or d.get("heads_elided")
                              or d.get("due_elided")):
        # A --full that lifted nothing otherwise prints the default text
        # byte for byte, and the flag reads as dropped.
        out.append("  (--full: nothing was elided)")
    if d.get("store") and d.get("rows") == 0:
        out.append("  " + FIRST_CONTACT)
    elif d.get("skill"):
        # After bootstrap agents learned symbion from --help and guesses, not
        # from the skill: later sessions neither loaded it nor ran `schema`
        # (2026-09-24).
        out.append(f"  before a write: load the symbion skill ({d['skill']}); "
                   f"`symbion schema` lists this store's kinds")
    return "\n".join(out)


def context(store_dir, cfg, target=None, commit=None, branch=None, since=None) -> dict:
    """Composed list/heads_for calls. No new query logic.

    The bare view is every open non-parked row plus every head of a plain
    kind (no bits): a decision or a durable fact is context, a verdict is a
    record. A plain head has no status to resolve, so its exit is the
    `retired` tag (`supersede --add-tag retired`): a progress log or a
    "built at" marker leaves this view and stays on its object. The tag
    means nothing on a status kind here; open work is resolved, not hidden."""
    if target:
        ttype, _, tname = target.partition(":")
        tname = gitref.canon_name(cfg, ttype, tname or None)
        rows = S.heads_for(store_dir, ttype, tname)
    else:
        notes = S.heads(S.load(store_dir))
        if commit:
            sha = gitref.canonical_commit(cfg, commit)
            rows = S.query(notes, target_type="commit", target_name=sha)
        elif branch:
            shas = gitref.branch_commits(cfg, branch, since)
            rows = [n for n in notes
                    if n.target.type == "commit" and n.target.name in shas]
        else:
            rows = [n for n in notes
                    if (S.read_status(n) == "open" and not n.spec.parked)
                    or (not n.spec.status and not n.spec.verdict
                        and "retired" not in n.tags)]
    return {"notes": [S.read_dict(n) for n in rows]}


# ---- schema: the vocabulary, for the hook, the skill and a human ----
TARGET_ORDER = ("commit", "item", "project", "arc")
assert set(TARGET_ORDER) == S.BUILTIN_TARGET_TYPES, "TARGET_ORDER must list every built-in target type"


def schema(store_dir, cfg, kinds=None) -> dict:
    """The store's kinds with their bits and row counts, then its target
    types. `rows` counts every row ever written under the label, heads and
    superseded alike, because a rename has to rewrite all of them."""
    kinds = K.read_kinds(store_dir) if kinds is None else kinds
    counts = Counter(n.kind for n in S.load(store_dir))
    return {
        "declared": K.is_declared(store_dir),
        "kinds": [{"label": label, "status": k.status, "parked": k.parked,
                   "verdict": k.verdict, "when": k.when, "rows": counts.get(label, 0)}
                  for label, k in kinds.items()],
        "targets": ([{"type": t, "catalog": None, "resolver": None} for t in TARGET_ORDER]
                    + [{"type": t, "catalog": cmd, "resolver": cfg.resolvers.get(t)}
                       for t, cmd in sorted(cfg.catalogs.items())]),
    }


def _bits(k: dict) -> str:
    return " ".join(b for b in K.BITS if k[b]) or "plain"


def render_schema(d: dict) -> str:
    src = "symbion.toml [kinds]" if d["declared"] else "symbion.toml [kinds]; absent = these defaults"
    out = [f"kinds  ({src})   rows"]
    w = max((len(k["label"]) for k in d["kinds"]), default=4)
    for k in d["kinds"]:
        out.append(f"  {k['label']:<{w}}  {_bits(k):<15}{k['rows']:>5}  {flatten(k['when'])}")
    out.append("targets  (--type; symbion.toml [catalogs])")
    builtin = " ".join(t["type"] for t in d["targets"] if t["catalog"] is None)
    out.append(f"  {builtin:<{w + 22}}  built in")
    for t in d["targets"]:
        if t["catalog"] is not None:
            line = f"  {t['type']:<{w + 22}}  catalog: {flatten(t['catalog'])}"
            if t.get("resolver"):
                line += f"\n  {'':<{w + 22}}  resolver: {flatten(t['resolver'])}"
            out.append(line)
    return "\n".join(out)
