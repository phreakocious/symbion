# Adopting symbion in an existing project

Read this in full before a bootstrap writes its first row. `SKILL.md`
beside it covers everything after; nothing here is needed again once the
sources are retired.

The store starts empty and the project does not. Populate it with a
**working set**, not a ledger: what a future session needs in order to not
redo or misread work. Measured twice on 2026-09-04: several hundred lines of
gitignored scratch and a few hundred commits became two or three dozen rows
each, and some scratch items were already closed by later commits.

**One store per repo, even when you only ever sit in one of them.** A hub the
operator works from, with satellites whose files the work is really about,
still gets a store each: a `check` row stamps the HEAD of the repo its session
runs in, and a `commit` target must exist there, so a satellite's rows in the
hub's store carry the wrong provenance and can name none of its commits. The
hub's own store keeps only what is about the hub. To see both at session start,
give the hub a project `SessionStart` hook that runs `symbion --dir
../<sat>-notes summary`: the store belongs to the satellite, so its catalogs
and check state still resolve there, and the header names the store it read
(`symbion --dir ../<sat>-notes: open outside arcs: …`), which is also how to
write to it.

1. **Enable a `file` catalog first** (`git ls-files --cached --others
   --exclude-standard`, with a glob when notes will only attach to code;
   dry-run the seed) so notes can attach to files, including one not yet
   committed. Without it every row is `item` or `project`.
2. **Sources, in order.** Gitignored scratch (`TODO.md`, `IDEAS.md`,
   `NEXT_SESSION.md`, `.superpowers/`) first: it is the only copy and it is
   about to go away. Tracked docs (`AGENTS.md`, `CLAUDE.md`, `NOTES.md`, `docs/audits/`)
   get a pointer, never a copy. Claude Code memory, when present, only when no tracked file
   already says it. Other stores on the machine (`../*-notes`, and any a
   `.symbion` file names): before this project had a store, a session
   elsewhere wrote its findings about it into the store at hand. Sweep them
   in one pass, for the project's name and the names it goes by (a host, a
   domain) -- the glob enumerates the stores that EXIST, so a name you
   mistyped cannot read as a zero, and the printed path is each zero's
   denominator:

   ```bash
   for s in ../*-notes; do printf '%s: ' "$s"
     symbion --dir "$s" list --all --grep '<name>|<host>' --json 2>/dev/null | jq length
   done
   ```

   Then re-read the stores that answered non-zero, without `--json`. A row
   there that belongs here is a candidate, with that row as its source.
   Measured 2026-09-28: a sibling store held the handoff's oldest open
   thread, so skipping it would have minted that thread twice.

   Folders beside the repo that are part of the project but not in it (no
   git, or no store) come next: a tracked file may be generated from one,
   and nothing in the repo says so. List the sibling paths that tracked
   files name, and the siblings that share the repo's name:

   ```bash
   r=$(git rev-parse --show-toplevel)
   git grep -h -o -I -E "$(dirname "$r")/[^/\"' )\`]+" -- :/ | grep -v -x -F "$r" | sort | uniq -c
   ls -d "$r"*
   ```

   A scratch file there is a source like the ones above. A tracked file
   generated from there gets a `note` naming its source and the command that
   regenerates it, so no session edits the copy by hand; if you regenerate
   it today, the diff is a `check --external` on that file. Measured
   2026-09-28: a tracked export named its source in a sibling folder with no
   git and no store, every sweep above missed it, and the copy lagged its
   source.

   An agent instruction file (`AGENTS.md` or `CLAUDE.md`) that is gitignored, or whose owner reviews
   every change to it, is the owner's: propose, do not write. Several candidate rules go in as one
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
   work: `question`, so `summary` lists them at session start (it deals its
   capped head list one kind at a time; `list --kind question --status open`
   is the whole queue) and `resolve <id> --body "decided: …"` (or
   `declined: …`) closes it with the answer on the resolving row. A
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
   - **Scratch.** Untracked: `git add -f` a verbatim copy under
     `<store>/archive/` (it was never tracked, so this is the only way the
     compression stays auditable), then delete it. `-f` because a scratch
     DIRECTORY can carry its own `.gitignore` of `*`, which the copy brings
     along: `git add -A` then stages none of it, at exit 0. Tracked: delete
     it; git keeps it. Either way, first grep the tree for inbound references and
     fix them, and carry the file's preamble, which has no kind and says
     what the file was FOR, into the project `decision` that records the
     migration. **Shared with other projects** (one scratch file, outside any
     repo, that several projects append to): it is still live for them, so
     neither delete applies. Archive the verbatim copy as above, then replace
     only this project's section in place with a pointer naming each new row
     id, and leave the rest of the file alone.
   - **The handoff file.** Replace its open-threads section with the one
     SKILL.md's "Session end" describes, and strike the other items that became rows;
     its prose stays. Edit it now, by hand: left to a tool that rewrites the
     file at session end, the migrated threads stayed in it, and the next
     session was briefed from them (2026-09-23).
   - **Claude Code auto-memory, if present** (`~/.claude/projects/<slug>/memory/`).
     Skip this source in projects that do not use it. Copy the migrated
     topic files verbatim to `<store>/archive/claude-memory/`, delete them
     and their `MEMORY.md` lines, and make `MEMORY.md` state the split: memory
     keeps how to work with the owner and context that attaches to no object;
     the rest is a row. Leave auto-memory on. A memory dir that was COPIED
     when this repo was carved out of another holds the same topic files as
     the other project's, one of which may have drifted: `cmp` each pair,
     archive the superset, and put only the copies that are a subset on the
     owner's delete list -- a drifted copy is a second source, not a
     duplicate.
   - **Rows in another store.** Run `symbion --dir <store> resolve <id>` on
     each row that moved (a plain row: `supersede <id> --add-tag retired`),
     with a body naming its new id here, then `symbion --dir <store> commit`.
     When both projects touch the object (a shared host, a service), the
     migration's project `decision` says which store owns it, so later
     sessions in either project write to one place.

   **The deletes are the owner's to run.** An agent's own `rm` over migrated
   sources can be refused as irreversible destruction — including an `echo rm
   -- …` dry run — and one such refusal forbids the delete by every other
   route, leaving the sources in two places. So end the step by printing the
   list and handing over the single line to run: `rm -- <paths>`. Verify each
   copy first and say in the same breath that the copies are archived and
   committed -- two checks, because they answer different questions: `cmp`
   against its archived twin says the COPY is faithful, and
   `git -C <store> ls-files archive/<src> | wc -l` against
   `find <src> -type f | wc -l` says git HOLDS it. `cmp` reads the working
   copy, so it passes on a copy git ignored and never committed.

   Then `symbion commit`, and end with at least one `check` row, dirty tree
   or not.
