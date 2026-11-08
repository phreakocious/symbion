# Symbion arc phases: a board over an arc's boxes

**Date:** 2026-11-05
**Status:** Designed with the user on 2026-11-05, in conversation, with a mockup of
the arc page. A builder's read the same day raised five questions: the user answered
one (the fallback order), and four are settled below as calls open to the user's
veto. Built 2026-11-05, 29f0e56..e52243d, one commit per task and per review fix;
the build's calls that depart from this text carry dated notes at their decisions.
**Relates to:** the first design (2026-09-04), for arcs as checklists; the kinds
design (2026-09-10), for the status and parked bits that make a box; the GUI design
(2026-09-08), whose rule that the GUI adds no capability the CLI lacks holds here;
the undo design (2026-10-13), which already takes back an arc write; the shared
stores design (2026-10-26), for how `arcs.jsonl` merges.

## The need

A user's request, relayed on 2026-11-05: "arc item order should be updatable, maybe
by dragging. a kanban/board view". Today an arc's boxes have no stored order. The
checklist sorts open boxes first, then by target, then by write time, and nothing
can move one. An entry is open, resolved or parked, so a board by status is a
grouped list, not a kanban.

The user's frame for the answer: this is metadata and accounting over existing
shapes. Agents already learn what is in flight from the session-start summary; a
board is for a person to see it, and for agents to drive as first-tier users.

## What this design does not change

No entry field is added, removed or reinterpreted. Status, kinds, tags and the
parked bit mean what they mean today. Stages as reserved tags were weighed and
rejected (2026-11-05): a tag is not exclusive, so one entry could sit in two stages,
and each reserved tag takes a name from the writers. A `stage` field on the entry was
weighed and rejected the same day, for adding to the entry contract and writing an
entry version on every move.

A claim on a box bound to the session key (considered 2026-10-16, from Beads'
`--claim`) is a separate idea. Nothing here builds or forecloses it.

## The design

### Two optional fields on the arc row

An arc row in `arcs.jsonl` gains two optional keys. `store.Arc` declares both.

- `phases`: a list of phase names, in board order, left to right. Absent means the
  arc has no phases.
- `placement`: a list of objects, one per placed box, each
  `{"id": ENTRY_ID, "phase": NAME | null, "by": AUTHOR, "at": UTC_ISO}`. The list's
  order is the order within each phase: a box's position is its index among the
  entries with the same `phase`. `id` is the entry's full id, which no edit changes.
  `phase` is `null` on an arc with no phases, where a placement stores order alone.
  `by` and `at` are the last move's writer and time: the writer resolved as for any
  write (`--author`, `$SYMBION_AUTHOR`, the harness name, in the GUI the user), the
  time in UTC to the second. A placement symbion writes only to hold an order (below)
  has neither, and its card reads `not moved`.
  *(2026-11-07, the user: a move that changes a box's phase also stores `from`, the
  phase it left, so its card can say where it came from; a reorder stores none.
  `not moved` is gone, below.)*

```json
{"id": "release", "name": "Release", "...": "...",
 "phases": ["next", "doing", "review"],
 "placement": [
   {"id": "20260110-120000-000000-aaa", "phase": "doing", "by": "claude", "at": "2026-01-10T12:00:00+00:00"},
   {"id": "20260110-120100-000000-bbb", "phase": "next", "by": "human", "at": "2026-01-10T12:05:00+00:00"}]}
```

`phase` names are unique, non-empty and free of leading or trailing whitespace.
`done` is reserved, in any case: the board's last column already carries that name.

An arc row is one line with no stored versions; a write rewrites it under the lock
(`store._edit_arc`). So a move adds nothing to `notes.jsonl`; the store's git history
holds the trail at `symbion commit`, which already takes a changed `arcs.jsonl`.

### Reading a board

A board is derived at read time from the arc's boxes (`store.arc_items`) and its
`placement`:

- A resolved box shows in the last column, `done`, whatever its placement says. Its
  placement is kept, so a reopened box returns to its phase and position.
- An open box with a placement whose phase is in `phases` shows there, at its
  position.
- On an arc with phases, a `null`-phase placement reads as the first phase, at its
  position: an order set before the arc had phases carries over.
- An open box with no placement, or one naming a phase no longer in `phases`, shows
  at the end of the first phase, after the placed boxes, in write order
  (`store.written_at`), as `arc todo` sorts today. The GUI checklist's sort by target
  name goes: one order everywhere.
- A placement whose id is not one of the arc's boxes (the entry moved to another arc,
  or was parked as an idea) is ignored. Nothing prunes it.
- Parked entries on the arc are never boxes (the kinds design), so they are never on
  the board. The page names how many there are, as a link to them.

The list view, the board and `arc todo` use one order: the phases in board order,
each phase's boxes by position, then `done`. `list --arc ID` is a list of entries,
not a read of the arc, and keeps its newest-first order.

### Writes

Both verbs write through `api.py`, which the GUI calls for the same effect.

- `arc phases ID NAME…` sets an arc's phases, replacing the list. It refuses to drop
  a phase that holds an open box, and names those boxes. A phase holds an open box
  whose placement names it; unplaced boxes belong to no phase and flow to the new
  first one. Resolved boxes placed in a dropped phase lose their placement, and the
  command says how many.
- `arc phases ID` with no names clears the phases and never refuses: each placement's
  phase becomes `null`, in board order, so the order survives.
- `arc move ENTRY [--phase NAME] [--before ENTRY | --after ENTRY | --top]` places one
  box. The caller gives the entry id; symbion finds its arc. Without `--phase` the box
  stays in its phase; without a position it goes to the end of the phase. It writes
  `by` and `at`. A move into a phase first gives that phase's unplaced open boxes
  placements in their current order, without `by` or `at`, so `--before` an unplaced
  box, and "the end of the phase", mean what the board shows. It refuses:
  - an entry that is not a box of a live arc, and names why (no arc, parked, a kind
    without the status bit, the arc archived: `arc unarchive` names the way back);
  - a phase not in the arc's `phases`, listing them;
  - `--phase done`, naming `resolve ENTRY`;
  - a resolved box, naming `supersede ENTRY --status open`;
  - a `--before` or `--after` entry that is not in the target phase.

A move is a write `undo` takes back by the arc's id, as any arc write is. Whether
`undo ENTRY` also takes back a move of that entry is the builder's call.

In the GUI, a drag into `done` resolves the entry. A drag out of `done` reopens it
and places it in one write, under one lock, so one `undo` takes back both and a
refused placement leaves the entry resolved. No other drag writes an entry.

### What agents read

- `arc todo ID` lists the open boxes in board order. On an arc with phases each line
  names its box's phase. `--json` carries `phase`, `position`, `by` and `at` per box.
- `show ID` on a box names its arc, and on an arc with phases, its phase. (Today
  `show` names no arc at all; this closes that gap.)
- The session-start summary's arc line, and `arc list`, add the count of open boxes
  in each phase past the first, for an arc with phases: for example
  `release 4/9 doing 1, review 2`. An arc without phases prints as today.
- SKILL.md's Arcs section gains the facts in a few lines: an arc can have phases, a
  box is moved with `arc move`, and `arc todo` reads in board order. The SKILL.md size
  test holds.

### The GUI

Each arc's page has a List | Board toggle, remembered per tab (`app.storage.tab`).
The user chose this over a board that replaces the checklist (2026-11-05, mockup
option B).

- **List:** today's checklist in board order, with a drag grip and, on an arc with
  phases, a phase chip on each row. An arc with no phases still gets drag-to-reorder
  here, which stores placements with a `null` phase.
  *(2026-11-05, at the build: the List moves boxes through the chip menu, the phases
  plus up and down, and drag lives on the Board, one toggle away; the controller's
  call, open to the user's veto.)*
  *(2026-11-06: the user asked for drag on the List as specified. Built: an open
  row drags by its grip and drops above or below the open row nearest the pointer,
  in that row's phase, so both ends of a phase are reachable. A drop on the List
  never resolves; the tick does. The page redraws the list after the write; the
  scroll place held in a browser check.)*
- **Phases editor:** the arc page sets phases through the same API function as
  `arc phases`, with the same refusal. The user needs no agent to start a board.
- **Board:** one column per phase, then `done`. An arc with no phases draws one
  column, `open`, then `done`. Each card shows its kind mark, its
  target, the first lines of its body and its short id; its last line is the
  placement's `by` and `at` as an age (`claude · 2h`), or `not moved` for a box no
  one has moved. `done` shows the most recent resolved boxes and a count of the rest.
  *(2026-11-07, the user, aligning the GUI with its design system: the line reads
  `moved by claude · 2h`, since a card's foot ends in its author and the bare form
  read as one, and a done card shows who ticked it, not `not moved`. A column's
  head is a board's, its phase and its count. A card moves by the phase chip, as
  below; the build had put an icon there, which read as a drag handle.)*
  *(2026-11-07, later, the user: `not moved` on every card said nothing, and the
  `order` chip read oddly as a control. A board card's last line is its arrows, then
  its mover, then its id; a box no one moved has no mover there. On a phased board
  the column names where the box is, so the line reads `from next · 2h` (`moved ·
  2h` for a reorder), the whole move in its tip; without phases, `moved · claude ·
  2h`.)*
- **No drag required.** Every move has a path without a drag: the phase chip opens a
  menu of the phases, plus up and down. Keyboard, screen reader and phone users move
  boxes that way; HTML5 drag and drop is unreliable on iPhone Safari.
  *(2026-11-07, the user: on the board, arrows replace the chip: up and down in the
  column, and on a phased arc left and right to the phase beside. One that cannot
  move is drawn disabled, so each keeps its place. The List keeps the chip.)*
- **Narrow windows** open the toggle on List: a row of phase columns does not fit a
  phone.
- **No redraw on a drag.** The card moves in the browser at once and the write runs
  behind it. A whole-board redraw after each move would flicker and lose the scroll
  position. A refused write puts the card back and says why.
- **Other writers' moves show.** The page's change poll watches `arcs.jsonl` as well
  as `notes.jsonl`, so an agent's move reaches an open board. The page's own writes
  must not light its "store changed" notice.
- A read-only serve (the public demo) shows the board and the list and offers no
  move.
  *2026-11-07:* the demo drags, so a visitor can see that a box moves. A drop
  there says what it does in your own store, writes nothing, and puts the card
  back. The move chip stays off the demo.

### Older symbions and shared stores

- An older symbion keeps both keys on an arc it edits: `store.Arc.extra` writes back
  any key it does not know.
- Shared stores merge `arcs.jsonl` one top-level field at a time (`merge.merge_arcs`).
  If two sides move boxes on one arc before a pull, the upstream's whole `placement`
  wins and the merge names what it dropped. Shared stores are experimental, so this
  stands, marked in the code as a deliberate simplification; a per-box merge is the
  upgrade path.

## Decided by the user, 2026-11-05

- The board is accounting over existing shapes; the entry contract does not change.
- Board state lives on the arc row (over a field on each entry).
- No reserved tags for stages.
- Agents get full control, as first-tier users.
- The word is `phases`, not columns: it names where the work stands, not how a page
  draws it.
- Each placement records `by` and `at`.
- The arc page has a List | Board toggle.
- The summary's arc line shows the per-phase counts.
- Unplaced boxes fall back to write time, everywhere: a hand-written checklist keeps
  its step order and a seeded one its catalog's. The GUI checklist's order by target
  name is dropped.

## Calls made in the design, open to the user's veto

- Drag into `done` resolves, out of it reopens and places in one write; `arc move`
  refuses both and names the verb that does each.
- `arc phases` refuses to drop a phase holding a placed open box; clearing all phases
  never refuses and keeps the order; there is no rename verb.
- A move into a phase first writes order-only placements (no `by`, `at`) for that
  phase's unplaced boxes, so positions next to them mean what the board shows.
- A `null`-phase order carries into the first phase when phases are added.
- The GUI can set phases, and an arc without phases has a one-column board.
- `done` is a reserved phase name.
- A resolved box keeps its placement.
- Unplaced boxes sort after placed ones.
- Stale placements are ignored, never pruned.
- Narrow windows open on List.
- Parked entries stay off the board, named by a count.
- Shared-store merges keep the upstream's whole `placement`.

## Not in scope

WIP limits, swimlanes, a board across the whole store, a phase rename verb, and any
claim or lease on a box.

## Left to the builder

- Where each `arc todo` line carries its phase, given that agents parse those lines
  (`cli._print_note` is the parsed line, `term.py` the person's view): the existing
  fields must not move.
- How the board's drag and drop is wired into NiceGUI, and its look within the
  existing palette and card mark.
- The exact wording of each refusal, in symbion's existing refusal shape.
- Whether `arc todo --json`'s `position` counts from 0 or 1; say which in `-h`.

## Tests

Before trusting each new test, break the code it guards in a scratch copy and watch
it fail (CLAUDE.md).

- The derivation rules above, one test each: resolved shows in `done` and keeps its
  placement; unplaced and stale-phase boxes land at the end of the first phase; a
  stale id is ignored; parked entries are never placed.
- On an arc with no placements, the GUI list and `arc todo` give the same order,
  write order.
- A `null`-phase order carries into the first phase; clearing phases keeps the order.
- A move `--before` an unplaced box lands there, and the boxes ahead of it gain
  order-only placements with no `by` or `at`.
- A drag out of `done` is one write: a refused placement leaves the entry resolved,
  and one `undo` takes back the reopen and the placement together.
- Each `arc move` and `arc phases` refusal.
- An older symbion's edit keeps `phases` and `placement` (`Arc.extra`).
- A shared-store merge with both sides moving boxes keeps the upstream's and names
  the drop.
- `undo` takes back a move.
- The summary's arc line with and without phases, within the hook's text cap.
- The GUI: a move through the chip menu writes the same placement as `arc move`; a
  read-only serve offers no move (a drag there, since 2026-11-07, writes nothing);
  a refused move puts the card back.
