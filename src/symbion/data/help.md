# How symbion works

symbion is a notebook and a ticket list for one project. Agents and people
write to it as they work: what they found, what is broken, what is left to
do, what they decided and what they checked. It lives in its own git
repository beside the project's, so it keeps a history and stays out of the
project's code.

This page explains what you see. Press `?` for the keyboard shortcuts.

## Entries

Everything here is an **entry**: a dated piece of writing, signed by its
author, about one thing. Each card is one entry. Its chips say what kind it
is, whether it is open, what it is about, what else it points at, and its
tags. Its author, age and id sit at the right.

Every chip is a link. Click one to list every entry that shares it; click
another on that list to narrow it. Click an entry's id to open it alone,
with its earlier versions.

Drop a file on any page to start an entry from it. A file of this project
becomes the entry's target. Other text becomes its body: markdown as it is,
anything else as a block of code. Text over 2,000 characters can go to the
store's `docs/` instead, and the entry points at it.
While you write an entry, a dropped file joins what it points at, or its
text goes in where you are typing.

## Kinds

An entry's **kind** says what sort of entry it is. The store chooses its
kinds. Each kind has up to three properties:

- **Open until resolved.** The entry is work to do. It counts under OPEN in
  the sidebar until someone resolves it, with the box on its card.
- **Parked.** The entry is open, but kept out of the open lists so that it
  waits quietly. The sidebar lists it under PARKED. Agents see it again
  when its due date comes.
- **Records a run.** The entry says what was checked and what that said,
  and carries the project's commit at the time. See Checks, below.

A kind with none of these is a record: a fact, a decision, a how-to. The
sidebar lists those under RECORDS.

<!-- kinds -->
These are the default kinds. A store can rename them, add its own, and set
each one's properties, in its `symbion.toml`. In the web page, this list
shows the store's own kinds, each with the `when` its `symbion.toml` gives it.

- **note** (record): a fact worth keeping that fits no other kind: a trap, a quirk, how something works.
- **decision** (record): a choice that was made and why, where the code alone would not tell you.
- **bug** (open until resolved): something broken, to come back to. Resolve it when it is fixed.
- **task** (open until resolved): a concrete next step. Filed in an arc, it is one of the arc's boxes.
- **question** (open until resolved): something only a person can answer before the work goes on. It may offer up to three answers, the default first; the answer is written below it.
- **idea** (parked): a thought to keep without acting on it yet. When it is taken up or dropped, resolve it and say why.
- **check** (records a run): a test or an inspection that ran: what was checked, and what it showed.
- **measurement** (records a run): numbers and what produced them, kept so that later runs can be compared with them.
- **prediction** (open until judged, then records the verdict): what someone expects, and what would prove it wrong, written before the evidence. It is resolved with the verdict.
<!-- /kinds -->

## Targets, refs and tags

These three look alike on a card. They do different jobs.

The **target** is the one thing an entry is about. An entry has exactly
one. Click it to open the target's page: every entry on it or pointing at
it, open work first. The Targets page lists every target that has entries.
The Docs page lists every markdown file an entry is on or points at, the
project's and the store's `docs/`, the most named first, each with a
button that opens it.

A target has a type and a name:

- `file: src/app.py` is a file in the project. The name is checked against
  the project's files, so part of a path finds the whole one. The `<>`
  button beside the chip opens the file, from the default branch when the
  branch checked out lacks it. The card counts the commits that changed the
  file since the entry was written: a high count means the entry may be out
  of date.
- `doc: recipe.md` is a file in the store's own `docs/` folder, where an
  entry's long detail goes. The name is its path inside `docs/`. The button
  beside the chip opens it, as it does a file.
- `item: login flow` is anything without a list of its own: a feature, a
  host, a test. The exact name is the key, so `login flow` and `login-flow`
  are two items. The name field suggests the names already in use.
- `commit: 3f2a1c9` is one of the project's commits. The name field offers
  the newest commits first, each with its subject.
- `project` is the project as a whole, for an entry with nothing narrower
  to be about.

A store can add types of its own, each with a list of names, such as hosts
or tests. A list can give each name a short description, which the name
field shows under it.

A **ref** points the entry at a second thing as well. It shows as a dashed
chip with an arrow. A note on a file can ref the commit that changed it,
and then it shows on the commit's page too.

A **tag** groups entries by a theme that cuts across targets: `#gui`,
`#perf`, `#release`. An entry can carry any number of tags. A tag has no
type and no list: it is exactly the word typed, so `flaky-test` and
`flaky-tests` are two tags. The Tags page counts each tag and points out
pairs that look like one. Writing `#word` in an entry's text adds the tag.

To choose: if it is a thing that can be renamed, moved or finished, or you
want one page that gathers everything about it, make it the target or a
ref. If it is a theme across many things, make it a tag.

## Special tags

Three tags mean something to symbion:

- `priority` stars an entry. It shows on the Summary's priority list and
  at the start of every agent's session until it is resolved or unstarred.
- `gotcha` marks a trap on a file. An agent is shown it the first time it
  touches that file in a session.
- `retired` marks a record that no longer holds. symbion stops offering it
  to agents as a current fact, gotcha warnings included. It stays in the
  history.

## Arcs

An **arc** is a checklist across many entries: a release, a migration, a
review. Create one on the Arcs page. Each box is an entry filed in the arc,
of a kind that is open until resolved: a task, a bug, a question, on any
target. Pick the arc in the new-entry form, or in an entry's edit dialog.
The Arcs page shows each arc's progress, and an arc is done when every box
is resolved.

An arc's **scope** says what it is mostly about, such as `item` or `file`.
The scope does not limit it: a file task, a project bug and an item question
can all be boxes in one arc. On an arc scoped to a type with a list of
names, such as `file`, **Seed all** adds a box for each name that has none
yet: an empty task, on that name.

Archive an arc when it is finished. It moves to the archived list. Its
entries stay, and any still open go back into the open lists.

## Checks

An entry that records a run shows what was checked and what that said at
its top. Named numbers from the run, such as `passed: 1341`, sit below its
text. The entry carries the project's commit at the time, and its state
chip says how that commit relates to the project now:

- `current`: no commit has landed since the run.
- `behind 5`: five commits have landed since. The run may no longer
  describe the code.
- `diverged`: the run was on another branch.
- `dangling`: no branch holds the run's commit any more, as after an amend
  or a rebase. Run the check again.
- `unverifiable`, with the reason, such as uncommitted changes when it ran.
  This never clears.
- `external`, with its age: the run was on something outside the project,
  such as a server. Only time makes it stale.
- `pending`: a prediction not yet judged.

Hover a state chip to see the commit, and for `behind`, the commits since.

## Nothing committed is deleted

Once the store is committed, no entry in it is deleted, and an edit keeps
the earlier version: open the entry and expand "earlier versions" to see
each change. Until then, editing an entry you just wrote changes it in
place. Resolving an entry closes it; the newest closed entries show under
RECENTLY CLOSED in the sidebar. A record that no longer holds is retired,
not removed.

A click on an entry's words opens it in the peek, beside the page: its
whole text and its earlier versions. **j** and **k** move it to the next
or the previous entry; in it, **x** resolves, **s** stars, **e** edits,
**a** answers a question, **o** opens the entry's page, and **Esc** closes
it. A write in the peek reloads the page, and the peek opens again on the
entry.
A click with ⌘, Ctrl or Shift opens the entry's page in a new tab.

**⌘K** (Ctrl K off a Mac) opens the palette: one box to go to a place, run
what the top bar offers, or find an entry by its words or the end of its
id. It looks here first, then counts the entries that match in each other
store a `serve` runs on, each a link to that store's search. The arrows
move, Enter goes (an entry here opens in the peek), and ⌘ Enter opens the
row's page in a new tab.

A question someone else wrote last takes an answer on its card: a button
for each answer its asker offered, the default first, or your own words
and Enter. The answer is added below the question, as you; whoever asked
acts on it and resolves the question.

The undo arrow appears in the top bar while the store's last write is
yours and not yet committed. It takes that write back. There is one undo,
for the store's last write: once someone else writes, or a commit runs, the
arrow dims, and clicking it says which write holds the undo now.

A page shows the store as it was when the page opened. When an entry is
written or edited after that, by an agent, at a terminal or in another tab,
a reload button with a count appears in the top bar. In the installed app,
the icon's badge counts the entries others wrote since you last opened a
page; your own are not counted. The app's window has no status bar, so the
address of the link under the pointer shows at its bottom left.

A write goes into the store's files at once. **commit** saves it into the
store's git history, and **push** sends that history to the store's remote,
if it has one. Their counts say what is waiting.

The name at the foot of the sidebar is who you are writing as: every write
made here carries it.

## More

- [README](https://github.com/phreakocious/symbion#readme): what symbion
  does, install and a quick start.
- [Guide](https://github.com/phreakocious/symbion/blob/main/docs/guide.md):
  install in full, and adopting symbion in a project.
- [Reference](https://github.com/phreakocious/symbion/blob/main/docs/reference.md):
  how it behaves, in detail.
