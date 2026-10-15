# Adopting symbion in an existing project

A bootstrap reads this in full before it writes its first entry. `SKILL.md` covers
everything after; nothing here is needed again once the sources are retired.

The store starts empty and the project does not. A bootstrap fills it with a
working set, not a ledger: what a later session needs to not redo or misread
the work. Several hundred lines of scratch and a few hundred commits make two
or three dozen entries, and some scratch items turn out to be closed already.

**One store per repo, unless the repos change together.** A `check` stamps one
repo's HEAD, and a `commit` target lives in one repo. The hub the user works
from, and each satellite the work is about, get a store each when the work
stays in one repo at a time; the hub's store keeps only what is about the hub.
A project `SessionStart` hook in the hub running `symbion --dir ../<sat>-notes
summary` shows the satellite's store there, and the same `--dir` writes to it.
Repos whose changes go together (one change starts in one repo and ends in
another) share one store that lists them under `[repos]` (`catalogs.md`).

1. **A `file` catalog first.** A bootstrap enables one, so entries can attach to
   files, one not yet committed included: under `[catalogs]` in the store's
   `symbion.toml`,
   `file = "git ls-files --cached --others --exclude-standard"`, with a
   pathspec when entries will attach only to code. `symbion arc seed --scope file
   --dry-run` lists its names. Without it, every entry is `item` or `project`. A
   store with `[repos]` has a type per repo instead. Other types come at step
   5, once the candidates show them.

2. **Sources.** A project's open threads and lessons sit wherever its
   sessions left them, and each project leaves them somewhere new. A
   bootstrap sweeps for all of them. Places they have been:
   - gitignored scratch (`TODO.md`, `IDEAS.md`, `NEXT_SESSION.md`,
     `.superpowers/`), often the only copy;
   - tracked docs (`AGENTS.md`, `CLAUDE.md`, `NOTES.md`, `docs/audits/`);
   - Claude Code memory;
   - retros and handoffs;
   - another store on the machine, where a session elsewhere wrote about this
     project before it had a store of its own;
   - a folder beside the repo that belongs to the project but sits outside
     it, which a tracked file comes from with nothing in the repo saying so.

   What a source is decides what it gives. The only copy migrates, and step 7
   retires it. A tracked file gets a pointer, never a copy. Memory gives what
   no tracked file says. A retro or a handoff gives its open items, and any
   lesson that names a file and that no tracked file states. An agent
   instruction file that is gitignored, or whose every change the user
   reviews (a hook gates it, or the user said so), is the user's: its
   candidate rules go in as one arc, one `question` per rule.

   Two sweeps found sources nothing else did. The first covers the stores on
   the machine (`../*-notes`, and any a `.symbion` file names), for the
   project's name and the names it goes by (a host, a domain). Each line names
   what it counted; a zero means only that the pattern matched nothing.

   ```bash
   for s in ../*-notes; do
     [ -f "$s/notes.jsonl" ] || { printf '%s: not a store\n' "$s"; continue; }
     n=$(symbion --dir "$s" list --all --grep '<name>|<host>' --json | jq length)
     printf '%s: %s\n' "$s" "${n:-failed, see above}"
   done
   ```

   The bootstrap reads each store that answers non-zero without `--json`: a
   entry there that belongs here comes over with that entry as its source. The
   second lists the sibling paths tracked files name, absolute and relative,
   and the siblings that share the repo's name. A `../` path is relative to
   its file.

   ```bash
   r=$(git rev-parse --show-toplevel)
   d=$(dirname "$r" | sed 's/[][\.*^$(){}?+|]/\\&/g')
   git grep -h -o -I -E "$d/[^/\"' )\`]+" -- :/ | grep -v -x -F "$r" | sort | uniq -c
   git grep -n -I -F '../' -- :/
   ls -d "$r"*
   ```

   A tracked file generated from a folder there gets a `note` naming its
   source and the command that regenerates it.

3. **The git log since the last handoff strikes; it does not mint.** A
   bootstrap checks each open scratch item against HEAD (greps the function,
   runs the command it names) before it becomes an entry. A closed item gets no
   entry unless its body carries a lesson the commit does not. A commit body can
   carry an open item nothing else does (an assertion added and never run):
   the bootstrap mints that one.

4. **Kinds.** The schema table's descriptions map most items. Three do not map
   by their look:
   - A verification not run today is a `note` on its commit, pointing at the
     report. symbion stamps a `check` when it is written, so a late one reads
     `current` against today's HEAD.
   - What needs the user's call, not work, is a `question`; `resolve <id>
     --body "decided: …"` closes it with the answer.
   - A bootstrap reads a `task` from a scratch file against the rest of that
     file: a ruling in its preamble goes into the task as the
     counter-position, or the bootstrap does not mint the task.

   The defaults fit most projects. A project mints a kind of its own when a
   class of entries recurs that no default's description fits. Stores in use added
   `anomaly` (a reading nobody can explain yet, resolved by the explanation;
   `status`); `measurement` began the same way, in several stores, before it
   became a default. A kind adds only a name and its description: the bits are
   all symbion acts on. The name pays when someone filters by it (`list
   --kind`), or when it gives a class its own place at session start, which
   deals one open entry of each kind in turn. A topic is a tag, not a kind.
   `[kinds]` replaces the defaults, so a new kind goes in beside the defaults
   the project keeps, with a `when` line: `symbion schema` prints it to every
   writer choosing a kind. A label renamed once entries use it needs the one-time
   `sed` the starter `symbion.toml` gives.

5. **Target types.** `item:` takes any name, and a near spelling of one is a
   second item. When several candidates are members of one kind of object (a
   host, a test, a metric, a board), that kind earns a catalog type of its
   own. Its names then match one list, a misspelling gets a `did you mean`,
   `arc seed` makes one task per member, and `arc reconcile` notices a member
   that left the list.

   The list usually exists already, somewhere the project keeps it. Catalogs
   in use print:
   - the hosts in an inventory file (`grep -E '^[a-z]' inventory.txt`);
   - the keys of a JSON file the project generates (`jq -r '.runs | keys[]'
     out/runs.json`);
   - a registry in the project's code (`python3 -c 'import m;
     print(*m.REGISTRY, sep="\n")'`);
   - the directories that hold a marker file (`git ls-files '*/agent.yaml' |
     sed 's#/agent.yaml##'`);
   - test ids (`catalogs.md`, "Seed");
   - a fixed list typed in place, for hardware serials (`printf '%s\n' A1 B2`);
   - a narrower or wider `file`: code only (`… '*.py' '*.sh'`), or a
     gitignored file entries attach to (`… --others --ignored --exclude-standard
     -- CLAUDE.md`).

   A resolver comes with a catalog whose names the built-in match gets wrong:
   - numbers, where a near value becomes a second target (`3.1416` is not a
     substring of `3.14159`);
   - names with aliases or formats (a domain with and without `www.`, a
     version as `v1.2` or `1.2.0`), which the resolver reduces to one form;
   - a set no list holds yet (people, places): a catalog over the names entries
     already use, whose resolver mints the canonical form at the first
     sighting.

   When the list's names change by plan (a migration renames every metric),
   `[renames] TYPE` maps old to new, and reconcile follows them.

   A kind with a handful of members and no list stays `item`, and so does one
   whose list no command can print yet. A bootstrap runs `arc seed --scope TYPE
   --dry-run` on a new type before its first entry: the names it prints are the
   names entries will match. A long command lives in the store's `tools/`, run as
   `"$SYMBION_DIR"/tools/x.sh`. A pipe checks its producer with `out=$(…) &&`:
   `sh` is dash on Debian and Ubuntu, which refuses `set -o pipefail`.
   `catalogs.md` has the rules for catalogs, resolvers and renames.

6. **The candidate table.** A bootstrap shows the user a table of the
   candidates (kind, target, one line, source) and waits for their go. An
   autonomous run with no user to ask writes, and puts the table in its final
   report. The bootstrap writes the entries as one `add --from-json -` (`add -h`
   lists the keys), and symbion writes none unless every line passes. Once
   committed, entries are append-only.

7. **Retiring the sources.** A bootstrap retires every source that migrated,
   in the same session, so no list survives in two places. A file to delete
   goes on a list the user runs at the end of this step. An edit in place (a
   `MEMORY.md` line, a shared file's section) the bootstrap makes itself.
   - **Scratch.** Untracked: a verbatim copy goes under `<store>/archive/`
     with `git add -f` (a scratch directory can carry its own `.gitignore` of
     `*`), and the file goes on the delete list. Tracked: the delete list;
     git keeps it. First, the bootstrap fixes the tree's references to it,
     and carries its preamble (what the file was for) into the project
     `decision` that records the migration. A scratch file shared with other
     projects is still theirs: after the archive copy, the bootstrap replaces
     only this project's section, with a pointer naming each new entry.
   - **The handoff file.** Each item no entry, instruction or memory holds
     becomes an entry: a trap is its file's `gotcha`, and a practice that names
     the failure behind it is a `task` tagged `practice` on the instruction
     file it belongs in. The bootstrap copies the file to `<store>/archive/`
     and puts it on the delete list now, by hand: a tool that rewrites it at
     session end keeps the migrated threads in it. From then on the wrap's
     commit message is the handoff (`wrap.md`). With continuity 0.21.0 or
     later, sessions end with `/wrap -j`, which writes no handoff file. With an
     older continuity the file stays, its open-threads section replaced by one
     naming this store and the commands that read it.
   - **Claude Code auto-memory** (`~/.claude/projects/<slug>/memory/`), where
     the project uses it. The bootstrap copies migrated topic files to
     `<store>/archive/claude-memory/`, puts them on the delete list, and
     removes their `MEMORY.md` lines. `MEMORY.md` then states the split: memory
     keeps how to work with the user and context no object holds; the rest is a
     entry. Auto-memory stays on. A memory dir copied when this repo was carved
     from another holds that project's files too, and a pair may have drifted:
     the bootstrap compares each pair with `cmp`, archives the superset, and
     lists only the subset copies for deletion.
   - **Entries in another store.** Each entry that moved gets `symbion --dir <store>
     resolve <id> --body "moved: <new id>"`, then `symbion --dir <store>
     commit`. A plain entry has no status: it takes `supersede <id> --add-tag
     retired --append --body …`. When both projects touch the object (a shared
     host), the migration's `decision` names the store that owns it.

   **The deletes are the user's to run.** An agent's own `rm` over migrated
   sources can be refused as irreversible, an `echo rm` dry run included, and
   one refusal forbids the delete by every other route. So the bootstrap runs
   `symbion commit` first: it names any path under `archive/` that git ignored,
   and the `git add -f` that takes it. Then it makes two checks for each copy:
   `cmp` against its archived twin says the copy is faithful, and `git -C
   <store> ls-tree -r --name-only HEAD archive/<src> | wc -l` against `find
   <src> -type f | wc -l` says the commit holds it. `cmp` alone passes on a
   copy git ignored, and `git ls-files` on one staged and never committed. The
   bootstrap ends the step on the list and one line to run, `rm -r -- <paths>`,
   with the word that the copies are archived and committed.

   Last, it writes at least one `check --external` on the migration: what the
   check covers is the store, not the repo.
