# symbion reference

How symbion behaves. To set it up, start with the [guide](guide.md).

## Entries, kinds and arcs

An entry is dated, attributed, retractable, and attached to something stable: a
commit, a file, a free-form item, an arc, or the whole project. Nine kinds by
default: `check` (a dated verification), `measurement` (a number and what
produced it, compared over time), `prediction` (a pre-registration,
written before the data), `decision` (an ADR), `bug`, `task`,
`question` (needs the user's answer; `--option` offers up to three, the default
first), `idea` (parked) and `note`. A kind is a label on
three bits (`status`, `parked`, `verdict`); declare your own under `[kinds]` in
`symbion.toml`, each with a `when` that says what it is for, and `symbion schema`
prints the table; the web page shows each `when` beside its kind. An **arc** is a named
line of work (an epic), shown as a checklist of entries across objects. The store is
a separate git repo of JSONL beside the project, so checking out an old branch
never time-travels your tickets.

## Catalogs, when a homogeneous set exists

A catalog is a shell command that prints one name per line. Declare it in the
store's `symbion.toml`:

```toml
[catalogs]
file = "git ls-files --cached --others --exclude-standard"
test = "git ls-files --cached --others --exclude-standard 'tests/test_*.py'"

[renames]
file = "git"     # so reconcile can follow renamed files
```

Each key becomes a target type (`--target file:src/x.py`). An arc seeded over
it gets one task per member, and `arc reconcile` reports which are live,
renamed or stale as the tree changes.

A line may add a tab and a description: `web1<TAB>the frontend box`. The GUI's
pickers show it under the name, and typing there matches it. Nothing else reads
it: a name resolves on the name, and the CLI prints no description. `commit`,
which has no catalog, shows each commit's subject.

One catalog type is built in: `doc`, the files under the store's own `docs/`,
each named by its path within it (`--ref doc:recipe.md`, or a unique part of
it; `doc:docs/recipe.md` finds it too). An entry's long detail goes in such a file, and the entry
names it. Its catalog runs at the store's root, so a `doc` name resolves from
any directory. A `doc` key under `[catalogs]` replaces it.

Several repos that change together share one store: `[repos]` lists them,
and each repo's name becomes a target type whose catalog runs in that repo
([catalogs.md](../src/symbion/data/skill/catalogs.md#repos-one-store-over-several-repos)).

Scope the command to where notes attach: no pathspec when docs and results
matter, a glob when only code does. A broad catalog costs only a refusal on an
ambiguous substring. Keep `--others --exclude-standard`: without it, a note on
a file made this session warns `taken as typed` on a correct name, and that
warning is the only thing that catches a typo. A gitignored file (a local
`CLAUDE.md`) still misses, and its note adds `it is a gitignored file in this
worktree`.

**Dry-run the first seed of every catalog type**
([catalogs.md](../src/symbion/data/skill/catalogs.md#seed) says why):

```bash
symbion arc seed --scope file --dry-run     # no arc needed; writes nothing
```

Names resolve exact, then unique substring, then the one name equal but for
case, else as typed. `parser.py` matches both `src/parser.py` and
`tests/test_parser.py`, so it is refused: use the full path. A read first takes
a name the store already holds as itself, so a deleted file's entries stay
readable by its old name. A closed catalog (`[resolvers] TYPE = "closed"`)
refuses the last step: a name it does not list is never stored
([catalogs.md](../src/symbion/data/skill/catalogs.md#resolvers)). Nor is a
name with a `.` or `..` segment that the catalog does not list: a `doc:` name
is its path within `docs/`, and a `file:` one is read as a path.

A catalog of **measured** names (a reading, a serial) needs a resolver, and
so does every **store-derived** one (`symbion list … --type X | jq …`).
[catalogs.md](../src/symbion/data/skill/catalogs.md#resolvers) says why, and
gives the protocol.

Within one write (`add --from-json`, `arc seed --name …`), names resolved by
earlier entries are candidates for later ones. A batch naming `src/parser.py` and
then `p`, against a catalog holding `src/pipe.py`, is refused as ambiguous
where two separate adds would store both.

## Conventions

- **How entries are kept and shown:** versions and which edits keep one, undo,
  two copies of a store, the session-start summary line by line, what a write
  prints, terminal against pipe, and store commits are in the skill's
  [mechanics.md](../src/symbion/data/skill/mechanics.md).
- **Parked means parked.** An `idea` never prints in `arc todo` or `context`,
  and prints in `summary` only when its `--due` date is near or past, or when
  someone other than the reader wrote it (`from <author>`). Close one with
  `resolve <id> --add-tag adopted --body "why"` or `--add-tag retired`.
  `list --kind idea` is the shelf.
- **Kinds are the project's.** Add or rename labels under `[kinds]` in
  `symbion.toml`; renaming one that has entries also takes a one-line `sed` over
  `notes.jsonl`, which `init` writes into the toml. A kind with both `status`
  and `verdict` is a pre-registration, closed by `resolve --result`: the
  default `prediction` is one, and a store whose table predates it adds
  `prediction = { status = true, verdict = true }`. A kind
  can pick its colour by a palette name, `color = "teal"`, in the terminal
  and in `serve`; a declared kind without one shares lavender, and a name
  outside the palette is refused with the list. symbion 0.3.0 and older
  refuse the key, so every machine that reads the store needs a newer one.
- **`priority` stars an entry** for the next session's summary:
  `supersede <id> --add-tag priority`, until the entry is resolved or
  `--rm-tag priority` takes the star off. From a day on, the summary prints
  its age.
- **Due dates.** `--due 2026-10-01` (or an ISO datetime) on any status entry. An
  open entry past due or due within 7 days leads the session-start summary
  (`overdue 2d`, `due in 3d`), and a parked one from its due date on. `list --overdue` lists those past due, `list
  --due-by DATE` those due by then, past due included, and `supersede <id>
  --due ''` clears one.
- **Untouched entries.** An open entry no one has edited through 5 active days,
  days on which anyone wrote an entry in the store, prints in the summary, up to
  3, oldest first. Idle days do not count, and an edit restarts the count. An
  entry with a due date, a pre-registration, a starred entry, one waiting on
  the reader and an arc's box are left out.
- **Time is UTC.** Entries are stamped in UTC, and symbion prints times and reads
  a bare date in UTC. Set `TZ` to print and read them in that zone instead.
  `SYMBION_NOW=<ISO time with an offset>` stops symbion's clock at that time, for
  a demo or a test: every age and due date reads against it, and a write is
  stamped with it, its id too.
- **Record finished work as a resolved task**:
  `add task --target … --status resolved --body "done: …"` for this
  session's work that had no ticket, when a later session would otherwise
  redo or misread it. A bootstrap mints no entry for work git history already
  shows closed (`adoption.md`, step 3).

## Where things live

- **Store:** `../<repo>-notes`, beside the *main* worktree; linked worktrees
  share it. `SYMBION_DIR` or `--dir PATH` (anywhere in the argv) override. A
  store named from another repo, or a command run inside a store (to edit
  `symbion.toml`, say), resolves in the store's own repo. A write or a catalog
  run says so on stderr.
  A named store that no project claims resolves where you stand, and a check
  or a prediction written to it says whose HEAD it took.
  `init` inside a store refuses.
- **A store not named after its repo:** put its path in a `.symbion` file in
  the repo root (first non-blank line; relative to the main checkout,
  absolute, or under `~/`; a file, not a link to the store). A linked worktree's pointer
  counts while the main checkout has none, as on a branch that adopts symbion.
  Outside any git repo, the nearest `.symbion` at or above the cwd names the
  store, as `--dir` would.
  `symbion --dir ../other-notes init --yes` writes it. If the repo already
  reads a store that exists, or git tracks a pointer whose store is missing
  here, init creates the new store and leaves the pointer alone; `--repoint`
  changes it. Commit it; a relative path
  survives a clone. It cannot live in `symbion.toml`, which is inside the store
  it has to find. Set it whenever the names differ: after a repo directory is
  renamed, every command, writes included, reports
  `no store at ../<new-name>-notes` until a `.symbion` names the old store.
  A note under that error names any store beside the repo that no project
  claims, by its name or by a pointer.
- **A store over several repos:** `[repos]` in its `symbion.toml` lists them,
  and each repo's `.symbion` names the store. From a listed repo, or a linked
  worktree of one, symbion stands in that repo; from anywhere else, in none,
  and a check on no repo's object, or a catalog type that is no repo's, is
  refused there. The
  summary's `repos in <store>:` line names the store, marks where you stand,
  and gives each other repo's path from the cwd. `file:PATH` is read from
  where you stand; `REPO:PATH` from that repo's root.
- **Files:** `notes.jsonl` (the entries and their versions), `arcs.jsonl`,
  `symbion.toml`, and the
  `README.md` and `.gitignore` that `init` writes. `archive/` holds files
  copied in to keep (`adoption.md`); `commit` names any path there git ignores.
  An edit keeps a field this symbion does not know, such as a newer release's,
  on the entry or arc as written.
- **Settings:** besides the tables above, `symbion.toml` takes
  `default_branch`, `local_only = true` (no remote on purpose: `commit` stops
  saying "no remote"), `shared = true` (experimental: two people's agents on one store; see
  [Shared stores](#shared-stores)), `command_timeout` (the seconds a configured
  command may run, default 30; raise it when a slow catalog is killed), `git_name`
  and `git_email` (the author of the store's own commits, default
  `symbion-notes` and `symbion-notes@local`; set your own where a remote
  refuses a made-up author), `stale_target_noun` (what `arc reconcile` says a
  stale target is missing, default `catalog entry`; name your catalog's
  members), and `[provenance] command`, which stamps a
  verdict entry with the JSON object it prints instead of HEAD. That command
  must exit 0; an object with a `sha` reads `behind N` as a HEAD stamp does.
  With `"dirty": true`, a `dirty_count` and a `dirty_paths` list let the write
  name the uncommitted paths, as the built-in stamp does. With `author_date`
  (git's `%ad --date=raw`), `subject` and `tree`, a stamp whose commit git
  has pruned still follows a rewrite. Any other key reads as unset, and each
  command names it on stderr with its near miss
  (`local_onyl (local_only?)`); the session-start hook puts the note in its text.
- **Trust:** the commands in `[catalogs]`, `[resolvers]`, `[renames]` and
  `[provenance]` run as shell at every write and many reads, and the
  session-start hook runs `summary` in every project with a store (and writes
  the gotcha hook's list, `~/.cache/symbion/gotchas/<session>`). A store
  cloned beside a repo runs its author's commands from then on: read its
  `symbion.toml` as you would code you clone.
- **Secrets:** a store is a git repo, so any pre-commit hook guards it;
  symbion ships none. [gitleaks](https://github.com/gitleaks/gitleaks), for
  one, scans only what a commit adds, from the store's `.git/hooks/pre-commit`:

  ```sh
  exec gitleaks git --pre-commit --staged --redact --no-banner --no-color -v -l warn .
  ```

  A refusal names a line of `notes.jsonl`, and `symbion commit` commits
  nothing. If the secret is in your last write, `symbion undo <id>` takes it
  back. Otherwise, edit the secret out of `notes.jsonl` by hand: an edit keeps
  each earlier version on its own line, so check every line that holds it.

  A committed secret stays in the store's history. Rotate it first: a
  rewrite does not reach a copy someone already has. Then
  [git filter-repo](https://github.com/newren/git-filter-repo)
  `--replace-text` replaces it with `***REMOVED***` in every commit. The entry
  keeps its id, so each cite of it still holds. If the store has a remote,
  force-push it and re-clone each other copy. Last, add a `decision` entry that
  cites the entry and says who removed what, when and why, without the secret.
  `show` on the entry lists that decision, so a reader who finds `***REMOVED***`
  finds the reason too.
- **Reading:** `symbion summary` (what the hook prints),
  `symbion context --target TYPE:NAME`, `symbion context --branch REF`,
  `symbion list --json`.
  Every command that prints entries takes `--json`.

## Shared stores

`shared = true` in `symbion.toml` lets two people, each with their own agents,
keep one store: a clone for each person, synced through the store's git remote.
It is experimental: a later release may change it, or remove it.
The [guide](guide.md#share-a-store) sets one up. With the key absent or false,
nothing here runs, and text output does not change. The one exception is
`summary --json`, which carries the keys `sync` and `others` (empty lists) and
`others_elided` (0) in every store. `shared = true` with `local_only = true` is
refused, and a shared store with no `origin` says `no remote`.

- **Upgrade both sides before the switch.** An older symbion writes an edit with
  the `side` of the version it replaces, and commits as `git_name`. A pull that
  brings such a commit says `a clone runs an older symbion`. An older symbion
  reads `shared` as an unknown key, warns, and syncs nothing.
- **A side** is the clone's `git config user.name`: each person's own, the same
  for all their agents. A write with none is refused, and the refusal names the
  `git config` command that sets it. Each new version of an entry or an arc carries
  the `side` that wrote it. An entry belongs to the side of its first version, and
  one with no side (written before the switch) reads in every side's session
  start.
- **The store's commits** are authored by the clone's git identity, so the store's
  `git log` shows each side. A `GIT_AUTHOR_NAME` or `GIT_COMMITTER_NAME` in the
  environment authors the commit instead, as it does for any git commit. The
  `Side:` trailer on a wrap still comes from the clone's `user.name`.
- **`symbion pull`** fetches `origin` and merges its branch into the store around
  your pending entries, which stay pending, each its session's. It says what it
  did, one line each: both ids of each entry that both sides edited, each arc
  value and each line both sides rewrote by hand that it dropped, the commits
  pulled (or `up to date`), a changed `symbion.toml`, and a clone that runs an
  older symbion. At session start you see those last two lines too, not only the
  agent. The merge driver handles `notes.jsonl` and `arcs.jsonl`. Git handles every other file, and it can fail on one:
  `symbion.toml`, or a file under `docs/` or `archive/`. Then `pull` changes
  nothing. It names the files, prints the git commands that finish the merge,
  and exits 1. A store that is not shared refuses `pull`.
- **The merge driver** is a hidden verb that git runs, `symbion merge-driver`. The
  first `pull` in a clone registers it in that clone's `.git/config` and
  `.git/info/attributes`. Nothing is committed, so a clone without symbion merges
  as git does. [mechanics.md](../src/symbion/data/skill/mechanics.md) says how it
  merges.
- **When sync runs.** Session start pulls, with a fetch cut off at 4 seconds.
  `commit` pulls, then pushes, and a push past 30 seconds is stopped; so do the
  GUI's commit and push buttons. A fetch that fails changes nothing: session
  start says so in one line, and a commit also says its push waits. Session
  start's pull waits at most 2 seconds for a store another symbion command
  holds, then says `pull: skipped` and reads the store as it is. `add`, `resolve` and
  `supersede` say `origin has N new store commits: symbion pull` when origin
  holds some. They fetch at most every 10 minutes, and a fetch that fails there
  says nothing. Every fetch runs with no terminal, so an unknown ssh host key or
  a passphrase with no agent fails rather than asking.
- **A pull cut off** (the process killed) leaves a copy of the pending entries it
  set aside in the clone's git dir, `symbion-pull`. The next `pull` puts them back
  first, and says so, and names a `symbion.toml` the cut-off pull's merge changed.
  A copy that is not whole is never applied: the pull stops, names the copy, and
  leaves it for you to compare with `notes.jsonl` and `arcs.jsonl` and remove.
- **Reading.** Every query shows both sides, and an entry the other side raised
  names its side: `claude (NAME)` as an author, `side=NAME` on a pipe's line, and
  `side NAME` on a terminal's short `list` line, which names no author.
  `list --side NAME` keeps the entries raised on side NAME, so an entry with no
  side is in no side's list, and a store that is not shared refuses it.
  `list --author` matches the label, so `--author 'claude (B)'` finds the other
  side's agent. `--json` keeps the stored author, with `side` in its own key.
  Session start reads your side's entries only
  ([mechanics.md](../src/symbion/data/skill/mechanics.md#the-session-start-summary)).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test,gui]'
.venv/bin/python -m pytest -q -n auto
```

On Windows, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test,gui]'
.\.venv\Scripts\python.exe -m pytest -q
```

The suite includes pyflakes (`tests/test_lint.py`): an unused import or
variable, or an undefined name, fails it. `.venv/bin/python -m ruff check src
tests` runs the same check, and `--fix` clears what it can. ruff has no OpenBSD
wheel, so `[test]` leaves it out there and the test skips.

To make the clone the `symbion` on PATH, link the venv's script into any
directory on PATH, `ln -s "$PWD/.venv/bin/symbion" ~/.local/bin/symbion`, or
install it with `pipx install -e /path/to/symbion` (or `uv tool install -e`).

The specs in `docs/superpowers/specs/` are dated design records: why each
decision was made. `SKILL.md` (in `src/symbion/data/skill/`, beside
`adoption.md` and the hook) is what an agent reads; keep it short and its
footgun list honest. If this clone is your installed symbion (an editable
install, then `symbion init --yes`), `~/.claude/skills/symbion` and, with
`--agent codex`, `~/.agents/skills/symbion` or, with `--agent hermes`,
`~/.hermes/skills/symbion` link to that directory, so an edit there reaches every session on the machine at once, as
an edit to `src/` reaches every `symbion` call.
