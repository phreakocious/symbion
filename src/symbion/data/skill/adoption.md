# Adopting symbion in an existing project

Read this in full before a bootstrap writes its first row. `SKILL.md`
beside it covers everything after; nothing here is needed again once the
sources are retired.

The store starts empty and the project does not. Populate it with a
**working set**, not a ledger: what a future session needs in order to not
redo or misread work. Measured twice on 2026-09-04: several hundred lines of
gitignored scratch and a few hundred commits became two or three dozen rows
each, and some scratch items were already closed by later commits.

1. **Enable a `file` catalog first** (`git ls-files --cached --others
   --exclude-standard`, with a glob when notes will only attach to code;
   dry-run the seed) so notes can attach to files, including one not yet
   committed. Without it every row is `item` or `project`.
2. **Sources, in order.** Gitignored scratch (`TODO.md`, `IDEAS.md`,
   `NEXT_SESSION.md`, `.superpowers/`) first: it is the only copy and it is
   about to go away. Tracked docs (`CLAUDE.md`, `NOTES.md`, `docs/audits/`)
   get a pointer, never a copy. `~/.claude` memory only when no tracked file
   already says it. A `CLAUDE.md` that is gitignored or `/affirm`-gated is
   the owner's: propose, do not write. Several candidate rules go in as one
   arc, one `question` per rule, so each is accepted or
   declined on its own. Retros and handoffs: their
   open items, plus any lesson that names a file and that no tracked file
   already states; the narrative and the generic lessons stay where they are.
3. **Git log since the last handoff is for striking, not minting.** Read the
   commit subjects and bodies (some repos put everything in the subject) and
   verify every open scratch item against HEAD (grep the
   function, run the `jq`) before writing it. A closed item gets no row
   unless its body carries a lesson the commit does not. A commit body can
   also carry an open item nothing else does (an assertion added and never
   run): mint that one.
4. **Kind mapping.** Decided, declined, rejected, accepted: `decision`.
   Known broken: `bug`. A concrete next action: `task`. A priced
   idea: `idea`. Something that needs the owner's call rather than
   work: `question`, so `summary` shows the owner's queue at session start
   and `resolve <id> --body "decided: …"` (or `declined: …`) closes it with
   the answer on the resolving row. A
   verification you did not run today: `note` on
   the commit with a pointer to the report, never `check`, because
   provenance is stamped at write time and a retroactive check reads
   `current` against today's HEAD. A `task` minted from scratch is read
   against the rest of its file first: a prior ruling in the file's preamble goes
   into the ticket body as the counter-position, or the ticket is not minted.
   Measured: a ticket to add a file to a never-ship list, when the deleted
   file's own header had ruled that unnecessary.
5. **Present the candidate table and wait for the owner's go:** kind,
   target, one line, source. An autonomous run with no owner to ask writes,
   and puts the table in its final report. Then write it as one `add --from-json -`, not one `add` per row
   (one bootstrap ran a shell command per row, each with a quoted markdown
   body). Rows are append-only; a bad one is superseded, not gone.
6. **After: retire every source that migrated,** in this session, so no
   list survives in two places. Per source:
   - **Scratch.** Untracked: `git add` a verbatim copy under
     `<store>/archive/` (it was never tracked, so this is the only way the
     compression stays auditable), then delete it. Tracked: delete it; git
     keeps it. Either way, first grep the tree for inbound references and
     fix them, and carry the file's preamble, which has no kind and says
     what the file was FOR, into the project `decision` that records the
     migration.
   - **The handoff file.** Replace its open-threads section with the one
     SKILL.md's "Session end" describes, and strike the other items that became rows;
     its prose stays. Continuity reads that section as delegation, and a
     hand edit only marks the file `stamp:edited`. Left for the next
     `/wrap`, migrated threads stayed in it, the list `/next` would brief
     from (2026-09-23).
   - **Auto-memory** (`~/.claude/projects/<slug>/memory/`). Copy the migrated
     topic files verbatim to `<store>/archive/claude-memory/`, delete them
     and their `MEMORY.md` lines, and make `MEMORY.md` state the split: memory
     keeps how to work with the owner and context that attaches to no object;
     the rest is a row. Leave auto-memory on.

   Then `symbion commit`, and end with at least one `check` row, dirty tree
   or not.
