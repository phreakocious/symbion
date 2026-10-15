"""The read side. A store Claude writes to and never reads is a pacifier."""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from itertools import pairwise, zip_longest
from pathlib import Path

from . import catalog, config, gitref, kinds as K, store as S
from .gui import servers

ARC_CAP = 10
PRIORITY_CAP = 10
HEAD_CAP = 8
PREREG_CAP = 10
PEOPLE_CAP = 3
WAITING_CAP = 10
CLOSED_CAP = 5
DUE_CAP = 10
DUE_DAYS = 7      # how far ahead the due block looks
HEAD_CHARS = 100
BODY_CHARS = 200
GOTCHA_CHARS = 200    # what the gotcha hook gives a session of a row
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
# `**x**` and `*x*` when the opening stars follow a space, the start, a `(` or
# a quote, and come before text or a code span, not before a space, a star or
# a glob's `.`, `/` or `:`; and the closing stars end a word, not a path's
# middle or a `***`. So a glob (`run-*.csv`, `'*.py'`, `app.*.gz`), a
# cron field and a `***` marker survive, even two in one body. Underscores
# are never emphasis here: in row bodies `_x_` and `__x__` are identifiers
# (`__init__`, `_cache … type_`), not emphasis. Group 2 is the wrapped text.
_EMPHASIS = re.compile(r"(?<![^\s(\"'])(\*\*?)(?=[^\s*./:,;)\]}>`]|`\S)"
                       r"(.+?)(?<=[^\s*])\1(?![\w*]|[./-]\w)")


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
    s = _EMPHASIS.sub(lambda m: m.group(2), flatten(s))
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


_UNITS = ((3600, 60, "m"), (86400, 3600, "h"), (14 * 86400, 86400, "d"),
          (63 * 86400, 7 * 86400, "w"), (365 * 86400, 30.44 * 86400, "mo"))


def age(stamp: str, now: datetime | None = None) -> str:
    """How long ago, in one short unit: `now`, `40m`, `8h`, `3d`, `5w`,
    `4mo`, `2y`. A stamp without an offset is local time, as in age_days."""
    then = datetime.fromisoformat(stamp)
    if then.tzinfo is None:
        then = then.astimezone()
    s = max(0.0, ((now or datetime.now().astimezone()) - then).total_seconds())
    if s < 60:
        return "now"
    for below, size, unit in _UNITS:
        if s < below:
            return f"{int(s // size)}{unit}"
    return f"{int(s // (365 * 86400))}y"


def when_added(stamp: str) -> str:
    """`3d ago`, or `just now`: when an amendment came, as its line says it."""
    a = age(stamp)
    return "just now" if a == "now" else f"{a} ago"


def measured(m: dict) -> str:
    """A row's measurements on one line, by name (store.note_from_dict)."""
    return ", ".join(f"{k}={v}" for k, v in m.items())


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
    soonest first. In an arc or not: a date is an explicit ask to be reminded,
    and past due outranks every other block. A parked row's date is when to
    come back to it, so it shows from that day on, not a week before (the
    owner, 2026-10-14)."""
    out = []
    for n in notes:
        state = S.due_state(n.due, now) if n.due and S.read_status(n) == "open" else None
        if state and (state[0] or state[1] <= (0 if n.spec.parked else DUE_DAYS)):
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


def pending(n, state=None) -> bool:
    """A pre-registration not yet judged. `context` prints with no state, and
    an open one read `checked at <sha>` there, as if it had run (2026-10-14).
    One that carries a result has run, whatever its status."""
    if state is not None:
        return state[0] == "pending"
    return bool(n.spec.status and n.spec.verdict and S.read_status(n) == "open"
                and n.result is None)


def waits(reader, n) -> bool:
    """An open row whose newest word is someone else's waits on the reader:
    a request, or an answer to the reader's own row. One the reader wrote
    last is answered, though someone else raised it. A pre-registration asks
    for nothing until it comes due, when the due block prints it: one in the
    user's words read `waits on you` for days (2026-10-14)."""
    return S.read_status(n) == "open" and not (n.spec.status and n.spec.verdict) \
        and wrote_it(reader, n.author)


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
FIRST_CONTACT = ('no entries yet: symbion add task --target project --body "..."  '
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
        "waiting": [],
        "waiting_elided": 0,
        "people": [],
        "people_elided": 0,
        "closed": [],
        "closed_elided": 0,
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
        "wrap": None,
        "repos": [],
        "unlisted_repo": None,
        "skill": None,
        "named_store": None,
        "commit_hint": None,
    }


def row_digest(by_id, n, chars, reader, active=frozenset()) -> dict:
    """One row's fields, the same in every block: a star or a near due
    date moved a row to a block that dropped its `from` and its
    registration date (2026-09-29). The labels are the row's. `parked`
    and `arc` say why a printed row is not in the header's counts: a
    session summed the rows against them and reported a counting bug
    (2026-10-12). `active` is the unarchived arc ids."""
    root = _root(by_id, n)
    return {"id": n.id, "kind": n.kind,
            "target": f"{n.target.type}:{clip(n.target.name, NAME_CHARS)}",
            "body": clip(n.body, chars),
            **({"parked": True} if n.spec.parked else {}),
            **({"arc": n.arc_id} if n.arc_id in active else {}),
            **({"waits": True} if waits(reader, n) else {}),
            **who(reader, root.author, n.author),
            **({"registered": S.shown(root.created_at)[:10]}
               if n.spec.status and n.spec.verdict else {}),
            **({"amendments": k} if (k := _amendments(by_id, n)) else {}),
            **added(by_id, n, reader, chars)}


def repo_rows(cfg) -> list[dict]:
    """`summary --json`'s `repos`: each listed repo, its path from the cwd,
    and whether the cwd stands in it. The path from the cwd is how an agent
    opens a row's file (`app:main.tf` is `../terraform-app/main.tf` from
    here), so it never keeps the map itself."""
    def from_cwd(p):
        try:
            return Path(os.path.relpath(p, Path.cwd())).as_posix()
        except ValueError:                   # Windows: another drive
            return p.as_posix()
    return [{"name": n, "path": str(p), "from_cwd": from_cwd(p), "here": n == cfg.here}
            for n, p in cfg.repos.items()]


def unlisted_repo(store_dir, cfg) -> dict | None:
    """The cwd's repo when its tree names this store and [repos] does not
    list it: the line to add, so its rows go on its own type."""
    root = cfg.cwd_root
    if not cfg.repos or cfg.here or root is None:
        return None
    try:
        if config.tree_store(root) != Path(store_dir).resolve():
            return None
    except config.PointerError:
        return None
    return {"name": root.name, "path": Path(os.path.relpath(root, store_dir)).as_posix()}


def repos_line(d: dict) -> str:
    """The store and its repos, the cwd's marked. A `.symbion` pointer makes
    the header the same in every repo, so this line is where a session sees
    which store it reads and where it stands, and each other repo's path
    from the cwd."""
    names = [r["name"] + (" (here)" if r["here"] else f" {r['from_cwd']}")
             for r in d["repos"]]
    line = f"repos in {Path(d['store']).name}: {', '.join(names)}"
    if u := d.get("unlisted_repo"):
        return (f"{line}; this repo, {u['name']}, names the store and is not listed: add "
                f"`{u['name']} = \"{u['path']}\"` under [repos] in symbion.toml")
    return f"{line}; file:PATH is read from where you stand, REPO:PATH from that repo's root"


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
    # The rows that wait on the reader lead, as their own block under their
    # count: spread over the blocks below it, after a wrap they sat among the
    # reader's own rows, and three of four fresh agents read the lines under
    # the count as those rows (2026-10-14). One that is also due keeps its
    # phrase there.
    waiting = S.newest_first(n for n in notes if waits(reader, n))
    w_cap = len(waiting) if full else WAITING_CAP
    due_all = due_soon(notes, _now)
    due_of = {n.id: st for n, st in due_all}
    due = [(n, st) for n, st in due_all if n.id not in {w.id for w in waiting[:w_cap]}]
    shown_above = {n.id for n, _ in due} | {n.id for n in waiting[:w_cap]}
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
        return row_digest(by_id, n, chars, reader, active)

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
    # Resolved rows the reader raised or closed that someone else closed, or
    # wrote to the body of, since the reader's newest row. Every block above
    # lists open rows, so an owner's answer by `resolve --body` closed the
    # agent's question and never reached it (2026-10-11), and an owner's
    # correction to an agent's resolve read nowhere (2026-10-12). A re-tag is
    # neither. The reader's newest row stands in for "since you last looked":
    # symbion keeps no session.
    # ponytail: parallel sessions share one reader name, so one session's write
    # hides the block from the next; a session key on each row would fix it.
    # Ordered as newest_first orders: a stamp is to the second, and the file
    # order breaks the tie.
    pos = {n.id: i for i, n in enumerate(loaded)}
    since = max(((S.written_at(n), pos[n.id]) for n in loaded if n.author == reader),
                default=None)

    def answered(n) -> bool:
        if reader not in (_root(by_id, n).author, _closer(by_id, n).author):
            return False
        for new, old in pairwise(_chain(by_id, n)):
            if wrote_it(reader, new.author) and (
                    new.body != old.body or S.read_status(old) != "resolved"):
                return (S.written_at(new), pos[new.id]) > since
        return False
    closed = S.newest_first(
        n for n in notes if since and S.read_status(n) == "resolved" and answered(n))
    c_cap = len(closed) if full else CLOSED_CAP

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
        # The last wrap's commit message is the handoff (2026-10-12): what
        # the previous session left for this one, read before its rows.
        "wrap": gitref.last_wrap(store_dir, cfg),
        "repos": repo_rows(cfg),
        "unlisted_repo": unlisted_repo(store_dir, cfg),
        "open": open_counts,
        "open_in_arcs": sum(r["open"] for r in rows),
        "arcs": rows[:a_cap],
        "arcs_elided": max(0, len(rows) - a_cap),
        "priority": [{**digest(n, BODY_CHARS),
                      "starred_days": age_days(_starred_at(by_id, n), _now)}
                     for n in priority[:p_cap]],
        "priority_elided": max(0, len(priority) - p_cap),
        # The rule SKILL.md stated, which loaded after this text was read
        # and also covered the rows the reader answered last (2026-10-14).
        "waiting": [{**digest(n, HEAD_CHARS),
                     **({"due": n.due, "past": st[0], "days": st[1], "phrase": due_phrase(st)}
                        if (st := due_of.get(n.id)) else {})} for n in waiting[:w_cap]],
        "waiting_elided": max(0, len(waiting) - w_cap),
        "people": [{**digest(n, HEAD_CHARS), "author": n.author} for n in people[:o_cap]],
        "people_elided": max(0, len(people) - o_cap),
        "closed": [{**digest(n, HEAD_CHARS), "closed_by": (c := _closer(by_id, n)).author,
                    "closed_at": c.created_at} for n in closed[:c_cap]],
        "closed_elided": max(0, len(closed) - c_cap),
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


def _closer(by_id, n):
    """The version that resolved n: the oldest of the resolved versions at the
    head of its chain, so a later re-tag does not read as the close."""
    closer = n
    for m in _chain(by_id, n):
        if S.read_status(m) != "resolved":
            break
        closer = m
    return closer


def _root(by_id, n):
    """The first row of n's supersede chain: a pre-registration's commitment.
    An amendment is a new row, so the head's own date is not the
    registration; 14 of 14 open prediction heads in three adopter stores
    restated it in the body (measured 2026-09-26)."""
    return list(_chain(by_id, n))[-1]


_CHANGED = ("kind", "target", "status", "due", "tags", "refs", "checked", "result",
            "measurements", "arc_id")


def written(by_id, n) -> list[str]:
    """`written <when> by <author>` for the version that wrote n's body and,
    when a later link kept that body, the fields the newest link changed and
    who changed them. An edit keeps the id, so the head's author read as the
    body's: an agent's bug the owner starred read `written … by` the owner
    (2026-10-12). When someone else raised it, the line names them first: a
    gotcha the user wrote and the agent amended read `written … by` the
    agent (2026-10-14)."""
    chain = list(_chain(by_id, n))
    b = next((new for new, old in pairwise(chain) if new.body != old.body), chain[-1])
    root = chain[-1]
    out = [f"written {S.shown(b.created_at)} by {b.author}"] if root.author == b.author else [
        f"written {S.shown(root.created_at)} by {root.author}",
        f"body changed {S.shown(b.created_at)} by {b.author}"]
    if b is not n:
        prev = by_id[n.supersedes]
        what = ", ".join(f for f in _CHANGED if getattr(n, f) != getattr(prev, f)) or "edited"
        out.append(f"{what}{' changed' if what != 'edited' else ''} "
                   f"{S.shown(n.created_at)} by {n.author}")
    return out


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
    """The seams that mark text added below a body, one per link that added
    it. A digest prints the body's start, so an amendment never reaches it:
    a prediction's corrected time printed its typo until it resolved
    (dogfood, 2026-09-30). Read off seams(), not a prefix test of its own: a
    question the owner trimmed and answered in one save read `+1 amendment`
    beside the two that `show` marked (2026-10-11)."""
    return sum(not above for _, _, _, above, _ in seams(by_id, n))


def seams(by_id, n) -> list[tuple[int, str, str, bool, bool]]:
    """Where each amendment joins n's body, by offset: `(at, author,
    created_at, above, edited)`, `above` for a prepend, whose text ends at
    `at`; `edited` when the same link also changed the text it kept, so the
    line does not pass a rewrite off as the original (2026-10-14). Most
    supersedes are appends, and a diff of one is the whole old body unchanged
    (the owner, 2026-10-08). One link can add both above and below: a
    prepend, then an append that rewrote the uncommitted row in place. A
    link that edited the old body reads through _edit_seams. An in-place
    edit of an uncommitted row leaves no link, so no seam."""
    out = []
    for old, new in pairwise(reversed(list(_chain(by_id, n)))):
        if not old.body or new.body == old.body:
            continue
        i = new.body.find(old.body)
        if i < 0:
            out = _edit_seams(old.body, new.body, out, new.author, new.created_at)
            continue
        end = i + len(old.body)
        # A side that added only whitespace has no seam: there is no text to mark.
        out = ([(i, new.author, new.created_at, True, False)] if new.body[:i].strip() else []) \
            + [(at + i, *s) for at, *s in out] \
            + ([(end, new.author, new.created_at, False, False)] if new.body[end:].strip() else [])
    return out


_WORD = re.compile(r"\S+")


def _edit_seams(old: str, new: str, out, author: str, created_at: str):
    """seams() across a link that edited the old body, by a word diff as
    gui.notes.edited() reads one. A seam stays beside the kept words next to
    it. Text after the last kept words that starts a paragraph is the link's
    own: the owner cut a sentence from an agent's question and answered below
    it in one save, and read as a rewrite the answer joined the question with
    no seam (2026-10-11). An edit in the last paragraph writes no paragraph,
    so it gets none."""
    ow, nw = list(_WORD.finditer(old)), list(_WORD.finditer(new))
    # A run under 3 words is as likely chance as kept text: a lone `the` carried
    # a seam into a rewrite of the whole row. Kept runs measured 4 words and up.
    blocks = [m for m in SequenceMatcher(None, [w[0] for w in ow], [w[0] for w in nw],
                                         autojunk=False).get_matching_blocks() if m.size >= 3]
    copy = {i + d: j + d for i, j, size in blocks for d in range(size)}
    moved = {}                                  # offset -> seam; a later one wins
    for at, *s in out:
        k = sum(w.end() <= at for w in ow)      # the first old word that ends below the seam
        if k < len(ow) and ow[k].start() < at:  # inside it: an append went on with the sentence
            if k in copy:
                moved[nw[copy[k]].start() + at - ow[k].start()] = s
        elif k - 1 in copy:
            moved[nw[copy[k - 1]].end()] = s
        elif copy.get(k):
            moved[nw[copy[k] - 1].end()] = s
    # ponytail: no seam for a paragraph written above the first kept run (only
    # a hand-written --body makes one); mirror the tail's check if one turns up.
    if blocks:
        _, j, size = blocks[-1]
        j += size                               # the first new word after the last kept run
        if j < len(nw) and new.count("\n", nw[j - 1].end(), nw[j].start()) >= 2:
            moved[nw[j - 1].end()] = [author, created_at, False, True]
    out = sorted((at, *s) for at, s in moved.items())
    # A seam whose side the edit emptied marks no text.
    cuts = [0, *(at for at, *_ in out), len(new)]
    return [s for s, a, at, b in zip(out, cuts, cuts[1:], cuts[2:])
            if (new[a:at] if s[3] else new[at:b]).strip()]


def at_seams(body: str, seams) -> list[tuple[str, str | None]]:
    """`body` cut at each of seams()'s offsets: `(piece, line)` pairs, the
    line naming who added the text below it (above it, for a prepend) and
    when; the last piece's is None. A cut's blank lines go: the line
    stands between the pieces instead."""
    out, start = [], 0
    for at, who, when, above, edited in seams:
        out.append((body[start:at],
                    f"-- {who} {'edited the text above and ' if edited else ''}added "
                    f"{'the above' if above else 'below'}, {S.shown(when)} --"))
        start = at
    out.append((body[start:], None))
    return [(p.strip("\n"), line) for p, line in out] if seams else out


# A cite: a whole id, the 10-character tail SKILL.md says to cite, or the
# 17-character one `show` takes too. gui.notes links the same shape.
_CITE = re.compile(r"(?<![\w-])(\d{8}-\d{6}-\d{6}-[0-9a-f]{3}|(?:\d{6}-)?\d{6}-[0-9a-f]{3})(?![\w-])")


def cite_ids(ids) -> dict:
    """Each id, and each of its 10- and 17-character tails -> the one id it
    names, or None when several share the tail."""
    one = {}
    for i in ids:
        for t in (i[-10:], i[-17:]):
            one[t] = None if t in one else i
    return one | {i: i for i in ids}


def links(notes, n) -> list[str]:
    """A line for each row n's body cites, read through that row's head,
    then one for each head whose body cites any version of n, newest first.
    Across many stores, over half the cites between rows named a version
    since superseded, mostly by a resolve, and only a `show` of each said so
    (2026-10-11). A cite names one row here: another store's id, or a tail
    two rows share, names none; a cite of n's own chain is no link."""
    by_id = {m.id: m for m in notes}
    one = cite_ids(by_id)

    def cited(m) -> dict:
        """Each row m's body cites -> the first spelling it is cited by."""
        out = {}
        for t in _CITE.findall(m.body or ""):
            if (i := one.get(t)) and i not in out:
                out[i] = t
        return out

    def line(word, h):
        st = S.read_status(h)
        # A check with no body, SKILL.md's first example, printed no lead
        # (2026-10-11): what it found, labelled as `list` labels it.
        label, text = (("", h.body) if h.body else ("result: ", h.result) if h.result
                       else ("checked: ", h.checked))
        lead = text and label + clip(text, HEAD_CHARS)
        return f"{word} {h.kind}{f' [{st}]' if st else ''}{': ' + lead if lead else ''}"

    mine = {c.id for c in _chain(by_id, S.chain_tip(notes, n.id))}
    heads = {}
    for i, t in cited(n).items():
        h = S.chain_tip(notes, i)
        if i not in mine and h.id not in heads:
            heads[h.id] = line(f"cites {t}" + (f" -> {h.id[-10:]}" if h.id != i else ""), h)
    by = [h for h in S.newest_first(S.heads(notes))
          if h.id not in mine and mine & cited(h).keys()]
    return [*heads.values(), *(line(f"cited by {h.id[-10:]}", h) for h in by)]


def added(by_id, n, reader, chars) -> dict:
    """`added` and `added_by`: the first line that the newest amendment by
    someone other than the reader added, unless the body's clip to `chars`
    already shows it. An owner's answer appended to a question sits at the
    body's end, and a digest prints the start: two sessions in a row relayed
    answered decisions as pending, `last by <owner>` the only sign
    (2026-10-07). The newest such amendment, not the head: the reader's own
    `noted` after an answer would hide it again.

    `added_at`, and `lead_rewritten_since` when a later link changed the
    clipped lead, tell an answer to an older question: a `let's do this`
    the reader had already acted on, under a lead rewritten twice since, was
    briefed as a yes to the new lead's open question (2026-10-07). Only the
    lead: the reader's `noted` after an answer changes the body, and counted
    as a revision it made an answer nobody acted on read as handled.

    `added_earlier`: the same for each other author before the newest, oldest
    first. The owner answered `approved.`, a reviewer appended a relay, and
    the agent's summary printed the relay alone (2026-10-14)."""
    rewritten, out, seen = False, [], set()
    lead = clip(n.body, chars)
    for new, old in pairwise(_chain(by_id, n)):
        if wrote_it(reader, new.author) and new.author not in seen:
            was = set(old.body.splitlines())
            fresh = [ln for ln in new.body.splitlines() if ln.strip() and ln not in was]
            if fresh:
                seen.add(new.author)
                # The first new line the lead does not show: an owner who
                # trimmed the lead's last sentence and answered below it
                # changed the lead first, and the answer never printed
                # (2026-10-11).
                line = next((ln for ln in fresh
                             if ln in n.body and clip(ln, chars) not in lead), None)
                if line:
                    out.append({"added": clip(line, chars), "added_by": new.author,
                                "added_at": new.created_at, "lead_rewritten_since": rewritten,
                                "edited": not _seamed(by_id, n, new, line)})
        rewritten |= clip(new.body, chars) != clip(old.body, chars)
    if not out:
        return {}
    return out[0] | ({"added_earlier": out[:0:-1]} if out[1:] else {})


def added_lines(a) -> list[dict]:
    """Each line added() found, oldest first."""
    return [*a.get("added_earlier", ()), a] if a.get("added") else []


def _seamed(by_id, n, link, line) -> bool:
    """Does `line` sit in text a seam gives to `link`? An edit that wrote no
    paragraph of its own has none: an agent's rewrite of a row the owner had
    amended printed `claude added …` with a reworded sentence of the first
    version, beside no amendment count (2026-10-12). A body written onto an
    empty one is all the link's own, and seams() marks no seam there."""
    if not by_id[link.supersedes].body:
        return True
    i, ss = n.body.find(line), seams(by_id, n)
    cuts = [0, *(at for at, *_ in ss), len(n.body)]
    return any((who, when) == (link.author, link.created_at)
               and (cuts[k] <= i < at if above else at <= i < cuts[k + 2])
               for k, (at, who, when, above, _) in enumerate(ss))


def added_label(a) -> str:
    """`alice added 19h ago, lead rewritten since:`, the words before an
    `added` line in summary and list; `edited` for a line no paragraph of
    the link's own holds."""
    since = ", lead rewritten since" if a["lead_rewritten_since"] else ""
    verb = "edited" if a.get("edited") else "added"
    return f"{a['added_by']} {verb} {when_added(a['added_at'])}{since}:"


def json_row(n, by_id, since) -> dict:
    """A row as `list`, `context` and `arc todo` print it in --json:
    `read_dict` plus what they compute. `raised_by` is the author of the
    chain's first row: `author` is the head's, so one `supersede --append` by
    a teammate moved a row out of a consumer's filter on its raiser (an agent
    harness, 2026-10-07). `raised_at` is that row's time, for the same reason:
    by `created_at`, a row amended today read as filed today (2026-10-12)."""
    root = _root(by_id, n)
    return S.read_dict(n) | {"commits_since": since, "raised_by": root.author,
                             "raised_at": root.created_at}


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
    if n == 1:
        return f"{n} {noun}"
    return f"{n} {noun[:-1]}ies" if re.search(r"[^aeiou]y$", noun) else f"{n} {noun}s"


def _labelled(paint, r, first=None) -> str:
    """A summary row with its own labels: `from <author>`, on a
    pre-registration its registration date, and its amendment count, in
    whichever block it prints. `first` leads the author labels."""
    by = ", ".join([*([first] if first else []), *([paint("waits on you", "warn")]
                                                   if r.get("waits") else []),
                    *(f"{label} {r[k]}" for label, k in (("from", "from"), ("last by", "last"))
                      if r.get(k))])
    reg = (", parked" if r.get("parked") else "") + \
        (f", arc {r['arc']}" if r.get("arc") else "") + \
        (f", registered {r['registered']}" if r.get("registered") else "")
    amd = f", +{_count(r['amendments'], 'amendment')}" if r.get("amendments") else ""
    star = f", starred {r['starred_days']}d ago" if r.get("starred_days") else ""
    line = (by and by + " ") + row_line(paint, r["kind"], r["target"], r["body"], r["id"],
                                        label=r["kind"] + reg + amd + star)
    for a in added_lines(r):
        line += f"\n    {paint(added_label(a), 'meta')} {paint(a['added'], 'body')}"
    return line


def render_summary(d: dict, paint=plain) -> str:
    counts = ", ".join(f"{paint(label, 'kind:' + label)} {n}" for label, n in d["open"].items())
    # `bug 0` beside a bug filed in an arc read as bug-free (2026-09-27).
    in_arcs = f"; {d['open_in_arcs']} open in arcs" if d.get("open_in_arcs") else ""
    named = f" {d['named_store']}" if d.get("named_store") else ""
    out = [f"symbion{named}: open outside arcs: "
           f"{counts or 'no status kinds declared'}{in_arcs}"]
    if d.get("repos"):
        out.append("  " + repos_line(d))
    if n := len(d.get("waiting", ())) + d.get("waiting_elided", 0):
        out.append("  " + paint(f"{_count(n, 'open entry')} {'waits' if n == 1 else 'wait'} on you: "
                                f"someone else wrote the newest word on "
                                f"{'it' if n == 1 else 'each'}; act on {'it' if n == 1 else 'them'} "
                                f"or name {'it' if n == 1 else 'them'} to the user before other work",
                                "warn"))
    for r in d.get("waiting", ()):
        lead = f"{paint(r['phrase'], due_role(r['past'], r['days']))} " if r.get("phrase") else ""
        out.append("  " + lead + _labelled(paint, {k: v for k, v in r.items() if k != "waits"}))
    if d.get("waiting_elided"):
        out.append("  " + paint(f"+{d['waiting_elided']} more waiting (--full)", "meta"))
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
    for c in d.get("closed", ()):
        # `closed by` names the closer, so a `last by` the same name says it twice.
        out.append("  " + _labelled(paint, {k: v for k, v in c.items()
                                            if k != "last" or v != c["closed_by"]},
                                    first=paint("closed by " + c["closed_by"], "good")))
    if d.get("closed_elided"):
        out.append("  " + paint(f"+{d['closed_elided']} more closed (--full)", "meta"))
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
            bits.append(_count(d["uncommitted_notes"], "entry"))
        if d["registry_modified"]:
            bits.append("arc changes")
        out.append("  " + paint(f"{' and '.join(bits)} not yet in the store's git "
                                f"({d.get('commit_hint') or 'symbion commit'})", "warn"))
    if d["unreadable"]:
        out.append("  " + paint(f"{_count(d['unreadable'], 'entry')} unreadable, "
                                f"first: {d['unreadable_first']}", "bad"))
    if d.get("unreadable_arcs"):
        out.append("  " + paint(f"{_count(d['unreadable_arcs'], 'arc line')} unreadable, "
                                f"first: {d['unreadable_arcs_first']}", "bad"))
    if d.get("no_status"):
        # The first id and the verb, as the unreadable line does: a bare
        # count had no consumer and sat as furniture for a week.
        n = d["no_status"]
        out.append("  " + paint(f"{_count(n, 'entry')} on a status kind "
                                f"{'carries' if n == 1 else 'carry'} no status (a write that "
                                f"bypassed symbion): counted neither open nor resolved; first "
                                f"{d.get('no_status_first')} -- "
                                f"supersede <id> --status open|resolved", "bad"))
    if d.get("full") and not (d["arcs_elided"] or d["priority_elided"] or d.get("heads_elided")
                              or d.get("due_elided") or d.get("people_elided") or d.get("waiting_elided")
                              or d.get("closed_elided")):
        # A --full that lifted nothing otherwise prints the default text
        # byte for byte, and the flag reads as dropped.
        out.append("  " + paint("(--full: nothing was elided)", "meta"))
    if w := d.get("wrap"):
        since = (f", {_count(w['commits_since'], 'commit')} since"
                 if w["commits_since"] is not None else "")
        if w.get("commits_by_repo"):
            since += f" ({', '.join(f'{k} {v}' for k, v in w['commits_by_repo'].items())})"
        out.append(f"  last wrap by {w['author']}, {when_added(w['at'])}{since}: {w['gist']}")
        out.extend("    " + paint(ln, "body") for ln in w["body"].splitlines())
    if d.get("gui"):
        out.append(f"  symbion serve: {d['gui']}; when you point the user at an entry, "
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


def one_line(d: dict) -> str:
    """The person's line at session start: the header, the soonest due date
    and the GUI. The rest of the text is for the agent."""
    line = render_summary(d).split("\n", 1)[0]
    n = len(d["due"]) + d["due_elided"]
    if n == 1:
        line += f"; 1 entry {d['due'][0]['phrase']}"
    elif n:
        line += f"; {n} entries due, the first {d['due'][0]['phrase']}"
    if d.get("gui"):
        line += f" · {d['gui']}"
    return line


def context(store_dir, cfg, target=None, commit=None, branch=None, since=None) -> dict:
    """Composed list/heads_for calls. No new query logic.

    The bare view is every open non-parked row plus every head of a plain
    kind (no bits): a decision or a durable fact is context, a verdict is a
    record. A plain head has no status to resolve, so its exit is the
    `retired` tag (`supersede --add-tag retired`): a progress log or a
    "built at" marker leaves this view and stays on its object. The tag
    means nothing on a status kind here; open work is resolved, not hidden."""
    loaded = S.load(store_dir)
    if target:
        ttype, _, tname = target.partition(":")
        ttype, tname = gitref.read_target(cfg, ttype, tname or None, loaded)
        rows = S.heads_for(store_dir, ttype, tname, loaded=loaded)
    else:
        notes = S.heads(loaded)
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
    by_id = {n.id: n for n in loaded}
    return {"store": str(store_dir) if S.exists(store_dir) else None,
            "gui": (r := servers.serving(store_dir)) and r["url"],
            "notes": [json_row(n, by_id, since[n.id]) for n in rows]}


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
                    + [{"type": t, "catalog": cmd, "resolver": cfg.resolvers.get(t),
                        **({"repo": str(cfg.repos[t])} if t in cfg.repos else {})}
                       for t, cmd in sorted(cfg.catalogs.items())]),
    }


def _bits(k: dict) -> str:
    return " ".join(b for b in K.BITS if k[b]) or "plain"


def render_schema(d: dict, paint=plain) -> str:
    src = "symbion.toml [kinds]" if d["declared"] else "symbion.toml [kinds]; absent = these defaults"
    out = [f"kinds  ({src})   entries"]
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
            own = "built in catalog: " if t["catalog"] == catalog.DOC_CATALOG else "catalog: "
            line = (f"  {t['type']:<{w + 22}}  {paint(own, 'meta')}"
                    f"{paint(flatten(t['catalog']), 'body')}")
            if t.get("resolver"):
                line += (f"\n  {'':<{w + 22}}  {paint('resolver: ', 'meta')}"
                         f"{paint(flatten(t['resolver']), 'body')}")
            if t.get("repo"):
                line += (f"\n  {'':<{w + 22}}  {paint('repo: ', 'meta')}"
                         f"{paint(t['repo'], 'body')}")
            out.append(line)
    return "\n".join(out)
