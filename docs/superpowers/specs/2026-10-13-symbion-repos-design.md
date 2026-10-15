# Symbion repos: one store over several repos that change together

**Date:** 2026-10-13
**Status:** Approved 2026-10-13. Revised three times that day: twice after
the owner's reviews (`file:` finds its repo, then `file:` is read from where
you stand), then after a second look by a fresh reviewer (the conversion is
one bulk rename; outside every repo, a write refuses as today). 2026-10-14:
the second look's smaller findings (a shared store's owner, a fork's
commits, the read forms, `[repos]` paths). Built 2026-10-14.
**Relates to:** the base design's §1 (`project_root` and `work_root`) and its
"Provenance and staleness"; adoption.md's "One store per repo".

## The need

Some work spans repos that change together, such as one Ansible repo and two
Terraform repos. A change often starts in one Terraform repo, changes the
other's inputs, and ends in the Ansible inventory. That work needs one place: one
arc, one open list, and one summary at session start in any of the three. It
also needs `check` rows ("plan clean", "lint passed") that read correctly from
any of the three.

symbion binds a store to one repo. adoption.md says "one store per repo" and
sends a hub to its satellites' stores with a SessionStart hook. That fits repos
that are loosely related. It does not fit these: a change that spans the three
has no one store, and an arc cannot span stores.

Such a store can be built today. Each of its repos has a `.symbion` that names
it, and one catalog type per repo (`git -C <repo> ls-files`) keeps same-named
files apart. It still has gaps: stamps and commit targets that know one repo,
catalog types that lose `commits_since` and `[renames]`, and no owner.

## What breaks today

Measured 2026-10-13 on three scratch sibling repos (`ansible`, `tf-net`,
`tf-app`) and one store named for the first, with a `.symbion` in the other two
that names it:

- The default `file` catalog lists the repo you stand in. `file:main.tf`
  written from `tf-net` and from `tf-app` is one target, and both rows list
  under it from every repo. Every Terraform repo has `main.tf`, `variables.tf`
  and `outputs.tf`.
- A catalog that puts each repo's name before its paths (`for r in …; do git -C
  ../$r ls-files | sed "s#^#$r/#"; done`) keeps them apart, and a bare
  `main.tf` is refused (`matches 3`). But `gitref._file` looks for
  `tf-net/main.tf` under the current repo's root, so these rows get no
  `target_blob` and no `commits_since`. The same holds for any catalog type
  that lists another repo with `git -C`.
- A check reads `current` only from the repo it was stamped in. From the other
  two it reads `unverifiable (commit unavailable)`: `check_state` runs
  `rev-list` in the current worktree.
- `--ref commit:<a sha of another repo>` is stored as typed, with `git does not
  know commit … here`.
- A store named for one of its repos belongs to that repo
  (`config.store_owner`). From a directory in no repo, `--dir` stands in it, so
  a check is stamped with that repo's HEAD whatever its target. A store named
  for none of its repos, with a pointer in several, has no owner. From a
  directory in no repo, `--dir` then refuses every write on a catalog type (`a
  configured command runs in the project's worktree`), even when the command
  reads no repo (`printf 'h1\nh2\n'`).

## The design

### `[repos]`

```toml
[repos]
ansible = "../ansible"
net = "../terraform-net"
app = "../terraform-app"
```

Each key names a repo. Each value is its main checkout, relative to the store
directory (`~` expands). Paths compare resolved, so a symlink names its
target, and two names on one path do not load: one repo is one type. Each
name is a target type. Its catalog is the default
file catalog (`git ls-files --cached --others --exclude-standard`), run in that
repo. A `[catalogs]` entry of the same name replaces the command (`net = "git
ls-files '*.tf'"`), and a `[resolvers]` or `[renames]` entry of that name works
as for any catalog type. Rows read `net:modules/vpc/main.tf`, `app:main.tf`,
`ansible:site.yml`.

A repo's name may not be a built-in type (`commit`, `item`, `project`, `arc`)
or `file`.

A type per repo, not a repo prefix inside a `file` name: the type is already
how symbion picks a catalog. `list --type net`, `arc seed --scope net` and the
GUI's type list then work unchanged, and no name has to be split into a repo
and a path.

Each repo still finds the store as today: a `.symbion` that names it, or the
store is `<repo>-notes` beside one of them.

### `file` in a store with repos

A store with `[repos]` declares no `file` type, but `file:NAME` is still
taken as input. NAME is a path as seen from where you stand, as a shell reads
one. It is stored on the repo that holds it, relative to that repo's root.
The first of these that answers wins:

1. **A path** (NAME is absolute, or starts with `~`, `./`, `../`, `.\` or
   `..\`) resolves from the cwd. Git says which listed repo, or linked
   worktree of one, holds it: from `tf-app`, `file:../terraform-net/main.tf`
   is `net:main.tf`. A path in no
   listed repo is refused, naming the repos.
2. **The name from the cwd.** Standing in a listed repo, NAME is joined to the
   cwd's directory in it: from `terraform-net/environments/x/`,
   `file:main.tf` is `net:environments/x/main.tf` when that repo's catalog
   holds it.
3. **That repo's catalog, from its root,** by the built-in rule (exact, else a
   unique substring, else equal but for case). Agents write a path from the
   repo's root, so `file:modules/vpc/main.tf` typed in a subdirectory still
   finds its file. Several matches are refused, naming each.
4. **The other repos' catalogs,** by the same rule run over all of them at
   once, each name kept with its repo. One repo holds it: stored there, and
   a note says so (`note: file:site.yml is ansible:site.yml, the one repo
   that holds it`). Several do: refused, naming each (`file:main.tf matches
   net:main.tf and app:main.tf; write one of them, or its path`).
5. **A new file,** in the repo you stand in, at the name from the cwd, with a
   note.

Outside every listed repo, only a path or a name exactly one repo holds
answers. Any other name is refused: `file:new.tf matches nothing in any repo;
give its path, or write REPO:new.tf`. A note names the stored form of every
`file:` that resolves, since its type always changes.

What a typed name resolves to can change as the repos do: once `net` holds
`site.yml`, `file:site.yml` typed in `net` means net's. A stored row does not
change: it holds `ansible:site.yml`.

`REPO:NAME` is the stored form, and is always read from the repo's root.

A read takes the same rule: `context --target file:X`, `list --target
file:X`, `list --type file --name X`, and `list --name file:X`. `list` splits
a `TYPE:` prefix off `--name` only for a declared type (`cli.py`, the `list`
handler), so without this, that last form reads a silent 0. `list --type
file` with no name reads every repo type. A read adds the names the store
holds on each repo type to that repo's catalog, so a departed file's rows
still answer.

On a write, the rule sits in `api._canonicalizer`. Every write's names meet
the catalog there, under the lock (`add`, each ref flag, `--from-json`,
`supersede`), so no write input skips it. The type checks that run before it
(`api.check_refs`, and the target check beside it) accept `file` in a store
with repos. After it, `store.add_many` refuses a target of a type the store
lacks (`store.note_from_dict`), and `supersede` checks no type. The plan
extends the check to every name a write resolves: an add's refs, a rename's
new name, and a supersede's new refs (`api.supersede` refuses a new
target). Refs a `supersede` inherits are not checked, so a row written before
the conversion stays editable. On a read, the rule sits in the two read
callers of `gitref.canon_name` (`list`, `context --target`), and a test per
read form above guards it, since a read that misses it prints a
silent 0. An arc's `--scope` and `rename --to-type` take a repo's name, and
`file` there is refused with the list. A store with `[catalogs] file` beside
`[repos]` does not load, and the message says to move that command under the
repo's name.

*2026-10-13, the owner's review:* the first draft refused every `file:`,
because each input that takes a type would need its own translation, and one
missed would store a wrong row. The write inputs meet in one call, and the
read forms are three.

*2026-10-13, the owner's second review:* `file:` is read from where you
stand, which reverses "the repo you stand in breaks no tie" (see "Not
chosen"). Finding the repo is deterministic work, and it belongs to symbion,
not the agent: an agent gives the path it already holds. The case that rule
guarded, a session in `net` that read `../terraform-app/main.tf` and types
`file:main.tf`, now stores net's, with the note. The path it read names app's.
The cross-repo rule of the first revision is step 4, and only answers what
the repo you stand in does not hold.

### Paths in a store without repos

A `file:` name that is a path (as in step 1 above) resolves from the cwd in
any store, and is stored from the repo's root: from `src/`, `file:./x.py` is
`file:src/x.py`. Measured 2026-10-13, both that and an absolute path were
stored as typed, as a second target beside the file's own, at exit 0. A path
outside the project is refused. The rule is the `file` type's: another
catalog type keeps a name as its catalog has it, since that name need not be
a path (a host list's names may start with `/`).

A bare name keeps today's rule, from the root. In a store without repos,
`file:NAME` is also the form `list` prints. Read from the cwd, a pasted name
would mean a different file in every subdirectory where the same name
recurs, as `main.tf` does at each level of a Terraform layout. A path never
has the printed form, so reading it from the cwd changes no pasted name.

### Where you stand

The cwd is in a repo when its main worktree (`config.project_root()`) is one of
the listed paths. For that repo, symbion reads the cwd's worktree, as
`work_root` does today: a linked worktree on a feature branch answers for its
repo. Every other repo answers from its main checkout.

What is about no row's object (a catalog type that is not a repo,
`context --branch`) runs in the repo you stand in. Outside every listed repo
it is refused by name, as a configured command and a stamp are refused today
outside a repo. The provenance command runs in the stamped repo ("Stamps"),
and the last wrap's count reads every repo ("Session start"). The GUI stands
where `serve` started.

*2026-10-13, the second look:* the first draft ran these in the first listed
repo when the cwd was in none, and listed the provenance command and the
wrap count here, against "Stamps" and "Session start". A fallback keyed on
the order of `[repos]` prints nothing when that order changes, and a
non-repo catalog that reads the repo would list a different set from each
one, the same-name defect this design opens with. The owner chose the
refusal.

### The repo of an object

Every git question about a row's object goes to the object's repo:

- A target or ref of a repo type: that repo. Catalog runs, name resolution,
  `target_blob`, `commits_since`, `only_on_default_branch` and `ignored_here`
  run there.
- A `commit` target or ref: the repo that holds the commit. On a write, the
  repo you stand in peels the name first. If it knows the name, that is the
  commit: `HEAD` and a branch name mean that repo's. Else symbion asks each
  other repo. If one knows it, the name is stored as its full sha. If several
  peel it to different commits (a branch name typed from outside every repo),
  it is refused, naming them. Several that peel it to one sha (a fork, a
  subtree) hold one commit. If none does, it is stored as typed, with the
  note, as today. On a read, `context --commit` peels as a write does, and
  `subjects`, `unmerged` and the GUI's commit lines ask each repo for its own
  shas.

### Stamps

A verdict row's stamp gains `repo`:

```json
{"sha": "…", "dirty": false, "repo": "net"}
```

The stamped repo is the target's repo when the target has one (a repo type or a
commit), else the repo you stand in. With neither, the row is refused, as a
stamp is refused today with no repo: run it from one of the repos, or add it
with `--external`. A provenance command runs in the stamped
repo, and symbion adds `repo` to the object it prints.

`check_state` reads a stamp in its repo: in the cwd's worktree if you stand in
that repo, else in its main checkout. A check stamped in `net` reads `behind 1`
after a commit in `net`, from any of the three, and does not move on a commit
in `app`.

A stamp with no `repo`, or with a name the store no longer lists, belongs to
the listed repo that holds its sha. Every stamp written before the store had
repos has no `repo`. In a shared store, those came from whichever repo each
session stood in, so no one repo is right for all of them; the sha is. When
several hold it (a fork), the repo you stand in comes first, then the order
of `[repos]`. The answer is kept per process, since a commit's repo does not
change. If no listed repo holds the sha, the stamp reads `unverifiable`, as
a stamp on a departed commit reads today.

A store without `[repos]` writes no `repo`, and nothing about its stamps
changes.

A caller that passes `head` for a page of rows passes the HEAD of the repo it
stands in. gitref uses it for that repo and reads each other repo's HEAD once
per call.

### Session start

The summary prints one line when the store has repos:

```
repos in work-notes: ansible ../ansible, net (here), app ../terraform-app; file:PATH is read from where you stand, REPO:PATH from that repo's root
```

The line names the store. The header names a store only when it is not the
one the cwd's tree names, and a `.symbion` pointer counts as naming it, so
confirming that every repo reads one store otherwise takes `summary --json |
jq .store`. The line also gives each other repo's path from the cwd: a row
on `app:main.tf` is `../terraform-app/main.tf` from here, and an agent need
not keep the map itself.

`summary --json` gains `repos`: `[{"name", "path", "from_cwd", "here"}]`.
When the cwd's repo names the store but is not listed, the line says so and
gives the `[repos]` line to add, and `unlisted_repo` holds its name and path.

The last wrap's `N commits since` counts every repo and names each repo that
has commits: `3 commits since (net 2, app 1)`.

`schema` lists each repo type with its path and its catalog command.

### Gotchas

Unchanged. `_list_gotchas` skips only the built-in types, and the hook matches
a row's name anywhere in a tool call. A Read of `/…/terraform-net/main.tf`
matches a row on `net:main.tf` from any repo. It also matches a row on
`app:main.tf`, as a short name in one repo already matches each directory's
file of that name.

### Owner

A store with `[repos]` is claimed by its repos, and `orphan_stores` never lists
it. `api.resolve` does not follow one owner for it: `--dir` from a cwd in one of
its repos stands in that repo, and from anywhere else stands in none.

## Converting a store

### A store with one repo

List the store's repo under the name its `file` rows will take. Move
`[catalogs] file` to that name. Then move the rows:

```bash
symbion rename --type file --to-type ansible
```

With no names, `rename` moves every row and ref of `--type` to `--to-type`,
each name kept as typed, and runs no catalog. `rename --type` takes only a
declared type today, and the store no longer declares `file`. So `rename
--type` also takes a type the store holds rows of: that is how rows leave a
type the store dropped. `--to-type` still takes only a declared type. The
store's stamps carry no `repo`, and their shas find it.

*2026-10-13, the second look:* the first draft looped `rename "$n" "$n"
--type file --to-type NAME` over the store's `file` names, with a bulk form
as the upgrade if the loop was slow. `rename` resolves the new name through
the catalog, as `add` does, so a row on a departed path that is a unique
substring of a live one moved onto the live file, with a note, at exit 0.
Measured in a scratch store: a row on `a/b.txt`, the file moved to
`legacy/a/b.txt`, and the same-name rename stored `file:legacy/a/b.txt`.
The bulk form takes no name, so it has nothing to resolve.

### A store already shared by several repos

- Name each `[repos]` key after the catalog type that already lists that repo.
  Its rows carry over unchanged, since their names are from the repo's root.
  Its `[catalogs]` entry may go: the repo's default catalog runs in the repo's
  tree, and in your linked worktree when you stand in one, which a `git -C`
  to a fixed path does not. A command kept there also runs in the repo's
  tree, so it needs no `git -C`, and a script kept in the store is
  `"$SYMBION_DIR"/tools/x.sh`, since `SYMBION_DIR` names the store in every
  configured command.
- Old stamps find their repo by sha (above). A check written with `--external`
  to get around the old behavior stays external: its row holds no commit to
  recover.
- A commit stored as typed because the cwd's repo lacked it (a short sha beside
  its full id): `symbion rename SHORT SHORT --type commit` peels the new name
  in whichever repo holds it and moves the rows onto the full id. A loop over
  the store's commit names that are not a full sha (40 hex digits, 64 in a
  SHA-256 repo) does all of them.
- `file:` rows from before the per-repo types: a name that exists in one repo
  moves there with `rename --to-type`. A name in several repos or in none is
  listed for the person to decide.

Merging several stores into one is not in this design.

## Not chosen

- **`commit:REPO@SHA`.** One commit would have two spellings, `commit:SHA` and
  `commit:net@SHA`. Two spellings of one commit are the duplicate target a
  shared store already shows. A full sha needs no qualifier, and the lookup
  finds its repo.
- **A repo known by its path or its origin URL.** A path changes with a
  re-clone or another machine's layout, and a URL changes when the host moves.
  The `[repos]` name changes only when the person changes it, and the sha
  lookup covers that too. The name is written anyway: repos that share history
  (a fork, a subtree) hold the same commits, and the name is what a reader of
  the row sees.
- **A one-off migration that fills `repo` in old stamps from the `.symbion`
  pointers.** A pointer says which repos use the store, not which repo a stamp
  came from. The sha says that.
- **`file:` as the repo you stand in, or the cwd's repo breaking a tie.** This
  design is for sessions that read across repos. A session in `net` that writes
  about `../terraform-app/main.tf` would store a row on `net:main.tf`, with no
  word. A refusal names both targets.
  *Reversed 2026-10-13 by the owner:* `file:` is read from where you stand
  (see "`file` in a store with repos"). The session above stores net's, with
  a note naming it, and the path it read names app's.
- **`file:` read from the cwd in a store without repos.** There `file:NAME`
  is also the printed form, and a pasted name would mean another file in a
  subdirectory that holds the same name. Only a path is read from the cwd.

## Limits

- One repo per stamp. A check that reads two repos (an inventory against
  Terraform outputs) is stamped with one: its target's, or the one you stand
  in. A stamp of several repos needs a rule for its state, and one that moves
  on any repo's commit reads `behind` after unrelated work. Deferred until a
  store shows the need.
- `default_branch` stays one value per store. In a repo whose default branch
  differs, `only_on_default_branch` finds nothing, and `context --branch` needs
  `--since`.
- `context --branch REF` reads the repo you stand in.
- A `[catalogs]` type that is not a repo runs in the repo you stand in, and
  from outside every listed repo a write on it is refused by name, as today,
  even when its command reads no repo. One whose command reads the repo
  (`git ls-files 'tests/*'`) lists another set from each repo, so such a
  command belongs under that repo's name; a non-repo type is for a list that
  is the same from every repo (a file in the store, a host list).
- `init` does not write `[repos]`: it is written by hand, and `init --dir STORE`
  in each repo writes its `.symbion`, as today.
- A listed repo missing on this machine: a write on its type is refused, naming
  the path. Its rows read as a departed object's do: `unverifiable`, no
  `commits_since`. `file:` skips it, and its note says so.
- A listed repo whose own tree names another store (its `.symbion`, or a
  `<repo>-notes` beside it) reads that store, and nothing in this one says
  so. With no store there, a write from that repo is refused, naming the
  path it looked for. The upgrade, if a store shows the need: the repos line
  marks each repo whose tree names another store.
- `file:` runs the built-in rule over each repo's catalog, and skips a repo
  type's `[resolvers]` entry: a resolver may return a name outside its
  catalog, so it cannot say whether its repo holds a name. Such a repo is
  reached by its own type.
- A path given as `file:` is stored as git places it, whether or not the
  repo's catalog lists it. A `[catalogs]` entry that filters a repo
  (`git ls-files '*.tf'`) still takes a path to its `README.md`: a catalog
  disambiguates, it does not whitelist.

## Docs

- adoption.md: "One store per repo" becomes one store per repo unless the repos
  change together, and then one store with `[repos]`. The hub's hook stays for
  repos that do not.
- SKILL.md: one sentence. In a store with repos, `file:PATH` is a path from
  where you stand, the one you read, and symbion stores it on its repo, as
  `REPO:PATH` from that repo's root. In any store, a `file:` path from the cwd
  (`./`, `../`, absolute) is stored from the repo's root.
- catalogs.md, reference.md, guide.md: `[repos]`, and a `[catalogs]` entry that
  replaces a repo's command.

## Testing

A fixture of three sibling repos and one store with `[repos]`. Each test fails
with its guard broken:

- `main.tf` in two repos is two targets.
- Each step of the `file:` rule, typed in `terraform-net/environments/x/`: a
  path into another repo (relative and absolute), a path outside every repo
  (refused), `file:main.tf` (`net:environments/x/main.tf`, though net's root
  holds `main.tf` too), `file:modules/vpc/main.tf` (from net's root),
  `file:site.yml` (the one other repo), and `file:new.tf` (a new file in
  `net:environments/x/`). From a repo that lacks `main.tf`, `file:main.tf` is
  refused, naming net's and app's. From outside every repo, `file:site.yml`
  is stored and `file:main.tf` and `file:new.tf` are refused.
- A resolving `file:` and a refused one through each write input: `--target`,
  `--ref`, `--from-json` and `supersede --add-ref`.
- In a store without repos, from a subdirectory: `file:./x.py` and its
  absolute path are stored from the root; a path outside the project is
  refused; a bare name resolves as today.
- The reads: `context --target file:site.yml`, `list --target file:site.yml`,
  `list --type file --name site.yml` and `list --name file:site.yml` list the
  `ansible:site.yml` rows; `list --type file` lists every repo's.
- `[repos]` with two names on one path does not load.
- A ref of type `file` that a write resolves, with the rule patched out of the
  canonicalizer, is refused by the store. A `supersede` of a row with an
  inherited `file` ref is accepted.
- From a directory in no repo, `--dir` writes a row on `item` and on a repo
  type, and refuses one on a catalog type that is not a repo, naming the
  repos.
- A check on `net:main.tf` reads `current` from each repo and from outside. It
  reads `behind 1` after a commit in `net`, and does not change after a commit
  in `app`.
- A check on an `item` stamps the repo you stand in. Outside every repo, it is
  refused. A check on `net:main.tf` written from outside every repo stamps
  `net`.
- A stamp with no `repo`, and one whose `repo` is no longer listed, read
  against the repo that holds the sha. A sha no listed repo holds reads
  `unverifiable`.
- `commit:` peels in the right repo. A sha of another repo is stored in full. A
  branch name typed from outside every repo is refused as ambiguous.
- From a linked worktree of `net`, that worktree answers for `net`.
- `commits_since` and `target_blob` on `net:` rows.
- The summary line, `summary --json`'s `repos`, and the last wrap's per-repo
  count.
- A `[catalogs]` entry for a repo type runs in that repo's tree, with
  `SYMBION_DIR` set. `[renames]` for a repo type runs there too.
- From a repo that lacks a commit, `rename SHORT SHORT --type commit` moves its
  rows onto the full id.
- `orphan_stores` does not list a store with repos.
- The bulk `rename --type file --to-type NAME` moves targets and refs as
  typed, a departed path that is a substring of a live one included;
  `rename --type file` is accepted while rows of it remain; and the old
  stamps read `current`.
- A single-repo store: stamps, the summary and every path above are unchanged
  (the existing suite).
