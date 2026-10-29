# How symbion keeps and shows entries

Read this when symbion's output shows something `SKILL.md` does not explain: a
label on a summary line, an entry's earlier versions under `--all`, a line a
write printed about someone else's entry.

## Versions

An entry keeps the id `add` printed for it. Its current version is its head,
and the head always has that id. Every edit, before `symbion commit` or after,
saves the version it replaces under a new id, stamped with that version's own
time. An entry's versions are its chain, and any id in a chain acts on the
head, so a cited id never goes stale. `show <id>` of an old version shows the head; `show --all
<id>` shows that version. `show` names the version an edit replaced
(`earlier version saved as <id>`), and `list --all --target TYPE:NAME` lists every version on
that object; `list --all --id <id>` of a head shows only the head. An earlier
version's line reads `superseded -> <head id> (open)`: the parentheses hold the
head's status, and the line prints no state or due phrase of its own.

In `notes.jsonl` an edit appends the new version under the entry's id, and the
version it replaces stays on its own line under its new id. Of a committed
line, only that id ever changes.

`undo ID` puts back the whole of the store's last write, a batch too: each
write first copies `notes.jsonl`, `arcs.jsonl` and the sessions file into the
store's git dir (`symbion-undo/`, which git never sees). It works when ID is an
entry or arc that write touched, the write was this agent session's (with no
session, your author's), no commit has run since, and nothing outside symbion
changed the files. One level: an undo leaves nothing more to undo. In
`symbion serve`, an undo icon sits in the top bar while the store's last write
is yours and uncommitted; it shows each entry the undo removes or puts back,
and asks first.

An `add` whose every field equals an uncommitted entry's writes nothing: it
prints that entry's id and says so, so a retried command stores one entry.
The time of an `--external` stamp is not compared. A `--from-json` batch
holding such a row writes nothing and names the line. After a commit, the
same add is a new entry.

Two copies of a store that is not shared, written to between pulls, conflict in
`notes.jsonl`, since each appends at its end. Keep both sides. If both copies
edited one entry, two lines then share its id: `summary` names the later one as
unreadable, reads skip it, and a write to that id is refused. Change the last 3
characters of one line's id to other hex digits, and both edits read as
entries.

A shared store (`shared = true` in `symbion.toml`) merges its copies itself, at
`symbion pull`, which session start and `commit` run. Do not fix its lines by hand.

- Entries either side added all stay, and your pending entries stay pending.
- When both sides edited one entry, the upstream's edit keeps the id and the
  local edit gets a new one. The pull prints both ids. The entry is then two
  entries, each with its own earlier versions: read both, and resolve or rewrite
  the one you do not want.
- An arc merges by id, field by field. A field only one side changed takes that
  side's value. Any other field both sides changed takes the upstream's, and the
  pull prints the value it dropped.
- A conflict in `symbion.toml`, or in a file under `docs/` or `archive/`, is the
  one case that needs git by hand. The pull changes nothing, names the files and
  prints the git commands that finish the merge. Until you run them, `commit`
  commits and says its push waits.

A pull that merged something, or put back a cut-off pull's entries, ends the undo
level, as a commit does. A pull that found nothing new leaves it.

## The session-start summary

The hook prints `symbion summary`, in this order. In a shared store it pulls first,
and the lines that pull printed come above the first item, in this order:

- Entries a cut-off pull had set aside, now put back.
- Each entry that both sides edited, with both ids.
- Each arc value or in-place rewrite the merge dropped.
- `pulled N commits from …`.
- A changed `symbion.toml`. Read it: it names commands symbion runs.
- While a pulled `symbion.toml` waits on `symbion trust`, a `note:` that says so,
  at each session start. Writes refuse until then. `trust` is the user's, at a
  terminal: show them the change and ask them to run it.
- A clone that runs an older symbion.

Past 5 of the split and dropped lines, `+N more not shown` ends them. The other
lines always print. If the fetch failed or ran past 4 seconds, one line says so
and that the summary reads the local store (`pull: git fetch origin failed: …;
the summary below reads the local store`).

1. `open outside arcs:` the open count of each status kind that is not
   parked, then `; N open in arcs` when arcs hold any, and `; N parked` when
   any open entry is parked. A parked entry and an entry in an arc are not
   in the per-kind counts, so each says so in its brackets (`[idea,
   parked]`, `[task, arc release]`).
2. `repos in <store>:`, in a store over several repos: where you stand, and
   each other repo's path from here.
3. `N open entries wait on you`, and below it each of them: the open entries
   whose newest version someone else wrote, up to 10, a due one with its
   phrase. An edit by someone else that changes no text (options, a star,
   a tag, a due date, an arc) does not count. A pre-registration waits on
   no one until it comes due. No block below repeats them; elsewhere, such
   as in what a write prints, a line starts `waits on you`.
4. The due block: each open entry past due or due within 7 days, and each
   parked one from its due date on (`overdue 2d`, `due in 3d`).
5. A line per arc: its id, done/total, age and name.
6. `priority`: each unresolved entry tagged `priority` that no block above
   printed, with `starred Nd ago` from a day on.
7. `closed by <author>`: an entry you raised or closed that someone else then
   resolved, or wrote to the body of, since your newest write.
8. Open entries someone else raised and you answered last, that no block
   above printed, parked included: 3, then `+N more from others (--full)`.
9. The open entries outside arcs, newest first, dealt one kind at a time so
   no kind crowds out the rest: pre-registrations first, up to 10, then 8
   more.
10. `N open entries untouched for 5 or more active days`, then up to 3 of
    them, oldest first, each led by `untouched N active days`. An active day
    is a day anyone wrote an entry in the store (in a shared store, only your
    side's entries and entries with no side count); an edit to the entry
    restarts its count. Left out:
    an entry with a due date, a pre-registration, a starred entry, one waiting
    on you, an arc's box, and one an open question cites, until the question
    closes.
    The line names the ways out, and asks for a plan after the user's first
    request.
11. What is not yet in the store's git, any unreadable line, and the command
    that fixes each.
12. `last wrap by <author>, <age>, N commits since: <first line>`, and the
    rest of that wrap's commit message below it: the previous session's
    handoff. `since the wrap:` under it names each entry the handoff cites
    that changed after the wrap, with what changed and who changed it.
13. In a shared store, a line for each other side that wrote since your side's
    last wrap, up to 3: `other side, NAME: 7 rows and 1 wrap since your last
    wrap: symbion list --side NAME --since wrap`. With no wrap by your side, it
    counts from the store's start. It prints no entries.
14. While `symbion serve` runs, its URL and the link form for one entry,
    `<url>/notes?id=<id>`.

At a terminal the summary is a person's: each entry's id opens its page in the
serve, and items 10 and 14 print without what they ask of an agent.

In a shared store, items 1 to 12 are your side's. Your side is the clone's
`git config user.name`. An entry the other side raised is left out of them, and
the other side's agents are someone else even under your agent's name: their
versions read `claude (NAME)`. An entry your side raised, and the other side
amended last, waits on you. An entry with no side (written before the switch)
reads for both sides. An arc of the other side lists while your side, or no side,
has an open box in it, and its done/total counts both sides' boxes. `last wrap
by` is your side's wrap. `list`, `show` and `context` read both sides.
`list --side NAME` keeps the entries raised on side NAME, so an entry with no side
is in no side's list.

Each elided list ends with the flag that shows it: `+N more open (--full)`.
The hook's copy stays under Claude Code's 10,000-character limit for hook
text: past it, it drops rows, the open list first and the waiting rows last,
and a line says how many; `symbion summary` prints them.

An entry's line reads `[labels] [kind, …] TYPE:NAME  lead  id`:

- `priority`: the entry is starred, in whichever block prints it.
- `from <author>`: someone else raised it. `last by <author>`: someone else
  wrote its newest version. Both print when both hold.
- In the brackets: `parked`, `arc <id>`, a pre-registration's `registered
  <date>`, `+N amendments`, and a star's age.
- The lead is the body's first 100 characters, flattened to one line; the due
  and priority blocks print 200. Text an edit added below the lead shows only
  when the lead runs under 100 characters; either way the line reads
  `+1 amendment`.
- When someone else amended the entry, a line below it gives the first line
  their newest amendment added (`alice added 3h ago: …`), or the first line it
  changed when it wrote no paragraph of its own (`alice edited 3h ago: …`),
  unless the lead already shows it. Each other author gets such a line, oldest
  first. `lead rewritten since` after the age says the lead
  changed after that line: the line answered an earlier question, so read the
  entry before you relay it.

You are whoever your writes default to: `$SYMBION_AUTHOR`, else the harness
(`claude` in Claude Code, `codex` in Codex, `hermes` in Hermes Agent), else
`git config user.name`, else `user`. The default names the harness, not the
model, since no harness tells its shell which model runs: a review by another
model passes `--author` to read apart. A person typing `! symbion add …` in a
session is recorded as the agent unless they pass `--author`. At a terminal the
reader is the person, so no entry is someone else's and none of these labels
prints.

## Between session starts

A reply that lands mid-session reaches you on your next write. `add`,
`resolve` and `supersede` end by naming, on stderr and in the summary's form,
each entry someone else wrote or amended since your previous write, or since
the session-start summary if that came later.

`add` also prints a `note:` line on stderr for the open entries already on its
target, an id in the body that matches no entry, a catalog name stored as
typed, a verdict entry with no `--checked` or stamped on a dirty tree, and a
parked entry of yours with no `--due`. In an agent session, it lists a
target's open entries at your first add there and gives only their count after
that, until the next session start. `add`, `resolve` and `supersede` say when
the text they write runs past 500 characters. Each write ends with the count
of entries not yet committed; an arc write with none pending says
`arcs.jsonl not committed`.

In a shared store, `add`, `resolve` and `supersede` also say `origin has N new
store commits: symbion pull` when origin holds commits this clone lacks. They fetch
at most every 10 minutes, and a fetch that fails says nothing. They never merge:
your view of the store changes when you run `symbion pull`.

## What a terminal and a pipe get

A terminal gets columns: the id's last 10 characters (which `show` takes), the
age, a `○`/`✓` status mark, the kind, the target, tags, a ref count, and one
body line cut to fit. A pipe, which scripts and agents read, gets the plain
line; in a shared store, the plain line of an entry the other side raised ends with
`side=NAME`. Bodies are markdown: `list --full`, `show` and `context` render them at
a terminal and print them as written on a pipe; a plain `list` page and
`summary` flatten each to one clipped line. `list --full` and `show` mark
where each amendment joined the body, naming who added it and when (`-- ada
added below, 2026-10-10 04:22:11Z --`); `--json` gives the body as stored.

`show` ends an entry with a line for each entry of this store its body cites,
by whole id or its 10- or 17-character tail, read through that entry's head
(`cites 412907-3be -> 520114-a1b bug [resolved]: …`), then a line for each
entry whose body cites any version of it (`cited by …`). An id from another
store, or a tail two entries share, names no entry.

## Store commits

`symbion commit` takes the entries this agent session wrote, under any author
(with no session, your author's), and every changed file in the store. An edit
of another session's uncommitted entry brings that entry into your commit: a
commit never holds an edit without the version it edits. Its
commit message ends with a `Rows: claude 3` trailer naming them by author.
`commit --wrap -m MSG` adds `Wrap: <author>`: that message is the handoff
`summary` prints as `last wrap by`. With nothing pending, a wrap commits the
message alone, where a plain `commit` exits 1. The store's git log is the
journal: `git -C <store> log --grep '^Wrap: '`.

In a shared store, a store commit is authored by the clone's git identity
(`user.name`), not by `git_name`, and a wrap adds a `Side: NAME` trailer after
`Wrap:`. After it commits, `commit` pulls, then pushes. A pull that stops, or a
push that fails or runs past 30 seconds, prints the commands that go on. The
commit stays made, and `commit` still exits 0.
