"""The read side. A store Claude writes to and never reads is a pacifier."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from itertools import pairwise, zip_longest

from . import gitref, kinds as K, store as S
from .gui import servers

ARC_CAP = 10
PRIORITY_CAP = 10
HEAD_CAP = 8
PREREG_CAP = 10
PEOPLE_CAP = 3
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


def age_phrase(stamp: str) -> str:
    """`age_days` as words. Under a day is not "today": a check run late
    last night is not today's."""
    days = age_days(stamp)
    return f"{days}d ago" if days else "<1d ago"


def due_phrase(state) -> str:
    """`store.due_state`'s pair as words. Past due with 0 days left is a
    datetime that passed earlier today."""
    past, days = state
    if past:
        return f"overdue {-days}d" if days < 0 else "overdue today"
    return "due today" if days == 0 else f"due in {days}d"


def plain(text: str, role: str) -> str:
    """The pipe's paint. Every text renderer takes a `paint(text, role)`
    and a terminal passes term.painter()'s; `role` is a colour's name there
    (`meta`, `body`, `warn`...) or `kind:<label>`. What a pipe prints must
    not change, so plain returns the text untouched."""
    return text


def due_role(past: bool, days: int) -> str:
    """The colour of a due phrase, in `list` and the summary alike."""
    return "bad" if past else "warn" if days <= DUE_DAYS else "meta"


def row_line(paint, kind: str, target: str, body: str, id_: str, label: str = "") -> str:
    """`[kind] type:name  body  id`: one row as the summary and `arc todo`
    print it. `label` fills the brackets when it says more than the kind."""
    type_, colon, name = target.partition(":")
    tgt = paint(type_ + colon, "meta") + paint(name, "text") if name else paint(target, "text")
    return (f"{paint(f'[{label or kind}]', 'kind:' + kind)} {tgt}  "
            f"{paint(body, 'body')}  {paint(id_, 'meta')}")


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


def open_outside_arcs(notes, kind, active):
    """Open heads of one kind on no ACTIVE arc -- an active arc's rows, of
    every kind, are tracked on its own checklist and reached through its
    progress line, not here. `active` is the set of unarchived arc ids. A row
    in an archived arc, or under an id that names no arc, has no progress
    line to reach it, so it lists here; keyed on `arc_id is None` it showed
    nowhere (2026-09-26)."""
    return [n for n in open_notes(notes, kind) if n.arc_id not in active]


def deal_by_kind(rows):
    """`rows` re-ordered by dealing one kind at a time, each kind newest-first
    and in the order the kinds first appear.

    The header counts open rows per kind; the heads were one newest-first cap
    over all of them, so a kind could be counted and never listed. Measured
    2026-09-28 on an adopter store: a bootstrap wrote its two `question` rows
    first -- the kind that exists for the owner to answer at session start --
    and 13 newer rows pushed both into `+6 more open`. Dealing spends the cap
    across the kinds instead, so the smallest kind is the one that survives it
    whole. Keyed on the kinds present, not on any label: a store's [kinds]
    table is its own."""
    lists = {}
    for n in rows:
        lists.setdefault(n.kind, []).append(n)
    return [n for turn in zip_longest(*lists.values()) for n in turn if n is not None]


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


def wrote_it(reader, author) -> bool:
    """Is this row someone else's, from the reader's point of view? The
    predicate the `from <author>` label uses wherever a row prints: at a
    terminal the reader IS the person, so there is no other author to name."""
    return reader is not None and author not in (reader, "unknown")


def who(reader, raised, last) -> dict:
    """The labels a row prints with: `from` who raised it, and `last` who
    wrote its newest version when that is someone else. Labelled by the head
    alone, an amendment relabelled a request: the owner's task, amended by
    Codex, printed `from codex`, and one the reader amended lost its label
    (the owner's call, 2026-10-02: name both). A row only the reader touched
    has no label: `last by` the reader on a row of no one else's is noise."""
    by = {"from": raised} if wrote_it(reader, raised) else {}
    if last not in (raised, "unknown") and (by or wrote_it(reader, last)):
        by["last"] = last
    return by


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
FIRST_CONTACT = ('no notes yet: symbion add task --target project --body "..."  '
                 '(before a write, an agent loads the symbion skill, SKILL.md)')


def empty_summary() -> dict:
    """The `--json` shape for a store that has never been created: the
    zero-valued shape `summary()` returns for an empty store, with `store:
    None`, the key a consumer gates on. The text path prints `no store at
    <path>`; the SessionStart hook stays silent by its own check first."""
    return {
        "store": None,
        "open": {},
        "open_in_arcs": 0,
        "arcs": [],
        "arcs_elided": 0,
        "priority": [],
        "priority_elided": 0,
        "people": [],
        "people_elided": 0,
        "heads": [],
        "heads_elided": 0,
        "due": [],
        "due_elided": 0,
        "uncommitted_notes": 0,
        "registry_modified": False,
        "unreadable": 0,
        "unreadable_first": None,
        "unreadable_arcs": 0,
        "unreadable_arcs_first": None,
        "no_status": 0,
        "no_status_first": None,
        "full": False,
        "rows": 0,
        "gui": None,
    }


def summary(store_dir, cfg, full=False, _now=None, reader=None) -> dict:
    loaded = S.load(store_dir)
    notes = S.heads(loaded)
    bad = S.load_malformed(store_dir)
    bad_arcs = S.load_arcs_malformed(store_dir)
    acts = [a for a in S.load_arcs(store_dir) if not a.archived]
    active = {a.id for a in acts}
    kinds = K.read_kinds(store_dir)
    open_counts = {label: len(open_outside_arcs(notes, label, active))
                   for label, k in kinds.items() if k.status and not k.parked}
    # Each row prints once: in the due block, else the priority block, else
    # the heads. Otherwise priority rows repeat among the heads.
    due = due_soon(notes, _now)
    shown_above = {n.id for n, _ in due}
    priority = [n for n in starred(notes) if n.id not in shown_above]
    shown_above |= {n.id for n in priority}
    unstated = no_status(notes)
    heads = [n for label, k in kinds.items() if k.status and not k.parked
             for n in open_outside_arcs(notes, label, active) if n.id not in shown_above]
    heads = S.newest_first(heads)
    # A pre-registration (status + verdict) has a deadline, and newest-first
    # under one cap pushed an open prediction off the list for many sessions
    # while its answer went into a handoff file (2026-09-24). Its own cap,
    # ahead of the rest, keeps the ceiling.
    by_id = {n.id: n for n in loaded}

    def digest(n, chars):
        """One row's fields, the same in every block: a star or a near due
        date moved a row to a block that dropped its `from` and its
        registration date (2026-09-29). The labels are the row's."""
        root = _root(by_id, n)
        return {"id": n.id, "kind": n.kind,
                "target": f"{n.target.type}:{clip(n.target.name, NAME_CHARS)}",
                "body": clip(n.body, chars),
                **who(reader, root.author, n.author),
                **({"registered": S.shown(root.created_at)[:10]}
                   if n.spec.status and n.spec.verdict else {}),
                **({"amendments": k} if (k := _amendments(by_id, n)) else {})}

    prereg = [n for n in heads if n.spec.verdict]
    rest = deal_by_kind([n for n in heads if not n.spec.verdict])
    shown = prereg + rest if full else prereg[:PREREG_CAP] + rest[:HEAD_CAP]
    # Open rows by anyone but the reader, parked included, that no block above
    # prints: an owner's `idea` went unseen by every agent session (measured
    # 2026-09-24). None for a reader at a terminal, who is the person.
    printed = shown_above | {n.id for n in shown}
    people = S.newest_first(
        n for n in notes if S.read_status(n) == "open" and n.id not in printed
        and (wrote_it(reader, n.author) or wrote_it(reader, _root(by_id, n).author)))
    o_cap = len(people) if full else PEOPLE_CAP

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
        # A running `serve`'s URL, so an agent can hand the person a row's
        # page (the owner, 2026-10-01).
        "gui": (r := servers.serving(store_dir)) and r["url"],
        "open": open_counts,
        "open_in_arcs": sum(r["open"] for r in rows),
        "arcs": rows[:a_cap],
        "arcs_elided": max(0, len(rows) - a_cap),
        "priority": [{**digest(n, BODY_CHARS),
                      "starred_days": age_days(_starred_at(by_id, n), _now)}
                     for n in priority[:p_cap]],
        "priority_elided": max(0, len(priority) - p_cap),
        "people": [{**digest(n, HEAD_CHARS), "author": n.author} for n in people[:o_cap]],
        "people_elided": max(0, len(people) - o_cap),
        # The people block names an author only for the rows no other block
        # printed, so a person's row that fell INSIDE the head cap lost its
        # attribution; which side of a cap a row falls on is not something a
        # reader can reason about. `digest` labels it in every block.
        "heads": [digest(n, HEAD_CHARS) for n in shown],
        "heads_elided": len(heads) - len(shown),
        "due": [{**digest(n, BODY_CHARS), "due": n.due,
                 "past": past, "days": days, "phrase": due_phrase((past, days))}
                for n, (past, days) in due[:d_cap]],
        "due_elided": max(0, len(due) - d_cap),
        "uncommitted_notes": notes_n,
        "registry_modified": registry,
        "unreadable_arcs": len(bad_arcs),
        "unreadable_arcs_first": (f"line {bad_arcs[0][0]}: "
                                  f"{flatten(bad_arcs[0][2])[:ERROR_CHARS]}"
                                  if bad_arcs else None),
        "unreadable": len(bad),
        "unreadable_first": (f"line {bad[0][0]}: {flatten(bad[0][2])[:ERROR_CHARS]}"
                             if bad else None),
        "no_status": len(unstated),
        "no_status_first": unstated[0].id if unstated else None,
        "full": full,
        "rows": len(loaded),
    }


def _chain(by_id, n):
    """n, then each row it superseded, back to the first; a cycle ends it."""
    seen = set()
    while n is not None and n.id not in seen:
        seen.add(n.id)
        yield n
        n = by_id.get(n.supersedes)


def _root(by_id, n):
    """The first row of n's supersede chain: a pre-registration's commitment.
    An amendment is a new row, so the head's own date is not the
    registration; 14 of 14 open prediction heads in three adopter stores
    restated it in the body (measured 2026-09-26)."""
    return list(_chain(by_id, n))[-1]


def _starred_at(by_id, n) -> str:
    """When n's star went on: the oldest row of its chain that carries it
    unbroken up to the head. A star on a row with no status never expires,
    so its age is what shows a stale one (2026-09-24: most of one store's
    stars were results starred while they were news). An uncommitted edit rewrites
    its row in place, keeping that row's time."""
    at = n.created_at
    for row in _chain(by_id, n):
        if "priority" not in row.tags:
            break
        at = row.created_at
    return at


def _amendments(by_id, n) -> int:
    """Links of n's chain that added text after a body (`supersede --append`,
    or a rewrite that kept the old body as its prefix). A digest prints the
    body's start, so an amendment never reaches it: a prediction's corrected
    time printed its typo until it resolved (dogfood, 2026-09-30)."""
    return sum(bool(old.body) and len(new.body) > len(old.body)
               and new.body.startswith(old.body)
               for new, old in pairwise(_chain(by_id, n)))


_SHA = re.compile(r"[0-9a-f]{40}")


def tag_key(t: str) -> str:
    """The form two tags share when they are one subject: case, `_`/`-`, a
    trailing s (`flaky-test` / `flaky-tests`). The CLI's new-tag notice and
    the GUI's tags page both group by it."""
    t = t.lower().replace("_", "-")
    return t[:-1] if t.endswith("s") and len(t) > 3 else t


def near_tags(counts: dict) -> list[list[str]]:
    """Groups of two or more tags in use that share a tag_key, largest first."""
    groups: dict = {}
    for t in counts:
        groups.setdefault(tag_key(t), []).append(t)
    return sorted((sorted(g, key=lambda t: (-counts[t], t)) for g in groups.values() if len(g) > 1),
                  key=lambda g: -sum(counts[t] for t in g))


def ref_label(t) -> str:
    """`type:name` for a ref or target, a full commit sha cut to 7: the CLI
    list line and the GUI chip both print refs."""
    name = t.name or ""
    return f"{t.type}:{name[:7] if t.type == 'commit' and _SHA.fullmatch(name) else name}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _labelled(paint, r) -> str:
    """A summary row with its own labels: `from <author>`, on a
    pre-registration its registration date, and its amendment count, in
    whichever block it prints."""
    by = ", ".join(f"{label} {r[k]}" for label, k in (("from", "from"), ("last by", "last"))
                   if r.get(k))
    reg = f", registered {r['registered']}" if r.get("registered") else ""
    amd = f", +{_count(r['amendments'], 'amendment')}" if r.get("amendments") else ""
    star = f", starred {r['starred_days']}d ago" if r.get("starred_days") else ""
    return (by and by + " ") + row_line(paint, r["kind"], r["target"], r["body"], r["id"],
                          label=r["kind"] + reg + amd + star)


def render_summary(d: dict, paint=plain) -> str:
    counts = ", ".join(f"{paint(label, 'kind:' + label)} {n}" for label, n in d["open"].items())
    # `bug 0` beside a bug filed in an arc read as bug-free (2026-09-27).
    in_arcs = f"; {d['open_in_arcs']} open in arcs" if d.get("open_in_arcs") else ""
    named = f" {d['named_store']}" if d.get("named_store") else ""
    out = [f"symbion{named}: open outside arcs: "
           f"{counts or 'no status kinds declared'}{in_arcs}"]
    for r in d.get("due", ()):
        out.append(f"  {paint(r['phrase'], due_role(r['past'], r['days']))} "
                   + _labelled(paint, r))
    if d.get("due_elided"):
        out.append("  " + paint(f"+{d['due_elided']} more due (--full)", "meta"))
    w = max((len(r["id"]) for r in d["arcs"]), default=0)
    pw = max((len(f"{r['done']}/{r['total']}") for r in d["arcs"]), default=0)
    for r in d["arcs"]:
        prog = f"{r['done']}/{r['total']}"
        age = f"{r['age_days']}d" if "age_days" in r else ""
        out.append(f"  {paint(r['id'].ljust(w), 'text')} "
                   f"{paint(prog.ljust(pw), 'good' if r['done'] == r['total'] else 'meta')}  "
                   f"{paint(age.rjust(4), 'meta')}  {paint(r['name'], 'body')}")
    # Each elided line names the flag that lifts it, as list's header does
    # with `(--all)`: a bare `+21 more open` sent a reader off to
    # `list --status open --json` and back (reported 2026-09-22).
    if d["arcs_elided"]:
        out.append("  " + paint(f"+{d['arcs_elided']} more (--full)", "meta"))
    # Labelled, not starred: the header counts open rows OUTSIDE arcs and
    # this block is every unresolved `priority` row, in an arc or not -- a
    # bare `*` beside a header that disagreed with it read as a bug in the
    # tool at first sight (measured 2026-09-20).
    for p in d["priority"]:
        out.append(f"  {paint('priority', 'warn')} " + _labelled(paint, p))
    if d["priority_elided"]:
        out.append("  " + paint(f"+{d['priority_elided']} more priority (--full)", "meta"))
    for p in d.get("people", ()):
        out.append("  " + _labelled(paint, p))
    if d.get("people_elided"):
        out.append("  " + paint(f"+{d['people_elided']} more from others (--full)", "meta"))
    for h in d.get("heads", ()):
        out.append("  " + _labelled(paint, h))
    if d.get("heads_elided"):
        out.append("  " + paint(f"+{d['heads_elided']} more open (--full)", "meta"))
    if d["uncommitted_notes"] or d["registry_modified"]:
        # Saved on disk, not yet in git: "2 notes uncommitted" read as unsaved.
        bits = []
        if d["uncommitted_notes"]:
            bits.append(_count(d["uncommitted_notes"], "note"))
        if d["registry_modified"]:
            bits.append("arc changes")
        out.append("  " + paint(f"{' and '.join(bits)} not yet in the store's git "
                                f"(symbion commit)", "warn"))
    if d.get("leftovers"):
        # Imported late: cli owns the paths, and imports this module.
        from .cli import _old_copies_line
        out.append("  " + paint(_old_copies_line(d["leftovers"]), "warn"))
    if d["unreadable"]:
        out.append("  " + paint(f"{_count(d['unreadable'], 'row')} unreadable, "
                                f"first: {d['unreadable_first']}", "bad"))
    if d.get("unreadable_arcs"):
        out.append("  " + paint(f"{_count(d['unreadable_arcs'], 'arc line')} unreadable, "
                                f"first: {d['unreadable_arcs_first']}", "bad"))
    if d.get("no_status"):
        # The first id and the verb, as the unreadable line does: a bare
        # count had no consumer and sat as furniture for a week.
        n = d["no_status"]
        out.append("  " + paint(f"{_count(n, 'row')} on a status kind "
                                f"{'carries' if n == 1 else 'carry'} no status (a write that "
                                f"bypassed symbion): counted neither open nor resolved; first "
                                f"{d.get('no_status_first')} -- "
                                f"supersede <id> --status open|resolved", "bad"))
    if d.get("full") and not (d["arcs_elided"] or d["priority_elided"] or d.get("heads_elided")
                              or d.get("due_elided") or d.get("people_elided")):
        # A --full that lifted nothing otherwise prints the default text
        # byte for byte, and the flag reads as dropped.
        out.append("  " + paint("(--full: nothing was elided)", "meta"))
    if d.get("gui"):
        out.append(f"  symbion serve: {d['gui']}; when you point the user at a row, "
                   f"link it: {d['gui']}/notes?id=<id>")
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
    # `store` and `gui` as summary's: `store` is null when no store exists,
    # since an absent store's read keeps exit 0 and `notes: []`, which a
    # client read as an object with no rows; `gui` spared a client a second
    # process only to learn the URL (2026-10-03).
    since = gitref.commits_since(cfg, rows)
    return {"store": str(store_dir) if S.exists(store_dir) else None,
            "gui": (r := servers.serving(store_dir)) and r["url"],
            "notes": [S.read_dict(n) | {"commits_since": since[n.id]} for n in rows]}


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


def render_schema(d: dict, paint=plain) -> str:
    src = "symbion.toml [kinds]" if d["declared"] else "symbion.toml [kinds]; absent = these defaults"
    out = [f"kinds  ({src})   rows"]
    w = max((len(k["label"]) for k in d["kinds"]), default=4)
    for k in d["kinds"]:
        out.append(f"  {paint(k['label'].ljust(w), 'kind:' + k['label'])}  "
                   f"{paint(_bits(k).ljust(15), 'meta')}{k['rows']:>5}  "
                   f"{paint(flatten(k['when']), 'body')}")
    out.append("targets  (--type; symbion.toml [catalogs])")
    builtin = " ".join(t["type"] for t in d["targets"] if t["catalog"] is None)
    out.append(f"  {builtin:<{w + 22}}  {paint('built in', 'meta')}")
    for t in d["targets"]:
        if t["catalog"] is not None:
            line = (f"  {t['type']:<{w + 22}}  {paint('catalog: ', 'meta')}"
                    f"{paint(flatten(t['catalog']), 'body')}")
            if t.get("resolver"):
                line += (f"\n  {'':<{w + 22}}  {paint('resolver: ', 'meta')}"
                         f"{paint(flatten(t['resolver']), 'body')}")
            out.append(line)
    return "\n".join(out)
