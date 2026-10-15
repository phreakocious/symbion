# symbion: catalogs, seeds, resolvers and reconcile

Read before `arc seed`, `arc reconcile`, or an edit to `[catalogs]`,
`[resolvers]` or `[repos]` in `symbion.toml`.

A catalog type is a command under `[catalogs]` that prints the type's names,
one per line. It runs in `sh` (dash on Debian and Ubuntu; on Windows, the one
Git for Windows installs) in the root of the current worktree, with
`$SYMBION_DIR` set to the store. From a repo the store does not belong to (a
hub writing with `--dir ../<sat>-notes`), it runs in the store's own project,
and a note on stderr says so; check stamps come from there too. A typed name
matches as SKILL.md says, with one addition: a read (`list --target`, `context
--target`) first takes a name the store already holds, so entries on a deleted
file stay readable by its old name.

## Repos: one store over several repos

Repos that change together share one store:

```toml
[repos]
ansible = "../ansible"
net = "../terraform-net"
app = "../terraform-app"
```

Each value is the repo's main checkout, relative to the store (`~` expands).
Each name is a target type (`net:modules/vpc/main.tf`), and may not be
`commit`, `item`, `project`, `arc` or `file`. Its catalog is every file git
lists in that repo; a `[catalogs]` entry of the same name replaces it, and
runs in the repo's root. `[resolvers]` and `[renames]` entries of that name
work as for any type.

`file:PATH` is still taken, read from where you stand, and stored on the repo
that holds it, with a note naming the result. The first of these that answers
wins:

1. A path (`/`, `~`, `./`, `../`): the repo, or linked worktree, git finds it
   in. From `terraform-app`, `file:../terraform-net/main.tf` is `net:main.tf`.
2. The name from the cwd: in `terraform-net/environments/x/`, `file:main.tf`
   is `net:environments/x/main.tf`.
3. The repo you stand in, from its root, by the catalog rule.
4. The other repos, by the same rule: one that holds the name takes it;
   several refuse, naming each `REPO:NAME`.
5. A new file in the repo you stand in, from the cwd.

Outside every repo, only a path, or a name exactly one repo holds, answers. A
`[catalogs]` type that is no repo's runs in the repo you stand in, and is
refused outside every one. `[catalogs] file` beside `[repos]` makes every
command refuse, naming the repo to move it under. The summary's `repos in
<store>:` line gives each repo's path from the cwd.

A `check` is stamped in its target's repo (a repo's type, or the repo that
holds a commit), else the repo you stand in; outside every repo it needs
`--external`. Each repo names the store with its `.symbion` (`symbion --dir
../<store> init --yes` in it).

To convert a store with one repo: list the repo under the name its `file` entries
will take, move `[catalogs] file` and `[renames] file` under that name, then
run `symbion rename --type file --to-type NAME`. A store already shared by
several repos, with a catalog type per repo: name each `[repos]` key after
that type, and its entries carry over; its `[catalogs]` entry may go. A commit
stored short, because the repo you stood in lacked it, moves onto its full id
with `symbion rename SHORT SHORT --type commit`. A `file:` entry whose name one
repo holds moves with `rename --to-type`; a name in several repos, or in none,
is the user's to decide.

## Seed

`arc seed` mints one bodiless task per name: it fans out over a catalog, where
the name is the whole ticket. A checklist whose boxes need a body is `add
--arc-id`. `arc seed --scope TYPE --dry-run` prints the names, needs no arc and
writes nothing. A second seed skips the names the arc already holds.

A catalog's output depends on the tool's version and the project's config.
`pytest --collect-only -q` ends with a blank line and `2 tests collected in
0.01s`, which a seed takes as a name; with `-q` already in the project's
`addopts`, it prints per-file counts instead. This keeps only the ids, and
pytest's exit:

```toml
[catalogs]
test = 'out=$(pytest --collect-only -q) && printf "%s\n" "$out" | grep ::'
```

With per-file counts, it lists no id and the catalog fails.

## Resolvers

Two catalogs need a resolver:

- A **numeric** catalog fragments under the substring rule: `3.1416` is not a
  substring of `3.14159`, so it becomes a second target.
- A **store-derived** catalog is empty until its first entry exists, and that
  entry cannot be written against an empty catalog.

```toml
[catalogs]
reading = 'out=$(symbion list --all --json --type reading) && printf "%s\n" "$out" | jq -r "[.[].target.name] | unique | .[]"'
[resolvers]
reading = "python3 tools/resolve_reading.py"
```

The resolver reads the typed name on stdin line 1, and one catalog name per
line after it. Exit 0 and the first non-blank line printed is the name stored,
in the list or not, spaces included: that is how a first sighting mints its
canonical form. Exit 2 and the lines printed are the matches, refused as
ambiguous. Any other exit is an error and stores nothing. A resolver never
falls back to the built-in match. It runs in the root of the current worktree,
so `tools/` above is the project's, and under the store's lock: `symbion list`
works there, and a write waits on the lock.

A piped catalog must fail when its producer fails, or a failed producer reads
as an empty catalog, and with a resolver every name mints as new. dash refuses
`set -o pipefail`, so the examples check the producer with `out=$(…) &&`.
Within one write (`add --from-json`, `arc seed --name …`), names resolved by
earlier entries are candidates for later ones.

## Reconcile

`arc reconcile <id>` checks the target of each open entry in the arc against
the live catalog:

- `live`: the catalog holds the name.
- `renamed`: the catalog lacks it, and `[renames]` maps it to a live name,
  given as `suggestion`.
- `stale`: the catalog lacks it, and `[renames]` gives no new name.
- `uncheckable`: its type has no catalog (`item`).

`[renames] TYPE` is that type's rename evidence: `"git"` is git's own rename
detection, and a command prints one `OLD<TAB>NEW` pair per line. With no
entry, a renamed file reads `stale`. `--apply` moves each renamed target's old
name onto the live one across the whole store, as `rename` does. A stale entry
closes only with `--resolve-stale`, which skips a verdict kind (`needs_result`
in `--json`): closing one takes a result.
