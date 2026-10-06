# symbion: catalogs, seeds, resolvers and reconcile

Read this before `arc seed`, `arc reconcile`, or editing `[catalogs]` or
`[resolvers]` in `symbion.toml`.

A catalog type (e.g. `file`) is a shell command under `[catalogs]` that prints
the type's legal names, one per line; `symbion schema` lists this store's. It
runs in the root of the current worktree. A name you type resolves against
it: exact, else unique substring, else the one name equal but for case, else
your typed string, stored with a note on stderr.

## Seed: add, unless there is a catalog

`arc seed` mints one bodiless task per name: it is for fanning out over a
catalog, where the name is the whole ticket. A checklist whose boxes need a
body is `add --arc-id`, as in SKILL.md.

Before seeding a catalog type for the first time, dry-run it and read the
names. The dry run needs no arc and writes nothing:

```bash
symbion arc seed --scope file --dry-run           # read these names
aid=$(symbion arc create --name "Review" --scope file)
symbion arc seed "$aid"                           # one task per name; prints the ids
```

A second `arc seed` skips names the arc already holds.

A catalog's output depends on the tool's version and the project's own config,
so what it printed in another project, or here last month, tells you nothing.
`pytest --collect-only -q` prints one test id per line, but where the
project's pytest config already puts `-q` in `addopts`, the flags stack to
`-qq` and it prints per-file counts (`tests/test_store.py: 39`). A seed on
that mints dozens of tasks against garbage targets.

## Resolvers: when substring is the wrong match

Two catalogs need a resolver:

- A **numeric** catalog fragments under the substring rule: `3.1416` is not a
  substring of `3.14159`, so it silently becomes a second target.
- A **store-derived** catalog (`symbion list … --type X | jq …`) is empty until
  its first `X` row exists, and that row cannot be written against an empty
  catalog: without a resolver the first `add` is refused.

Declare it beside the catalog:

```toml
[catalogs]
reading = "set -o pipefail; symbion list --all --json --type reading | jq -r '.[].target.name' | sort -u"
[resolvers]
reading = "python3 tools/resolve_reading.py"
```

The resolver reads the query on stdin line 1 and one catalog name per line
after it. Exit 0 and print the name to store, in the list or not: that is how
a first sighting mints its canonical form. Exit 2 and print the matches to
refuse as ambiguous. Anything else is an error and stores nothing; a resolver
never falls back to the substring rule. It runs under the store lock, so it
must not write to the store. `set -o pipefail` on a piped catalog is
load-bearing: without it a failed producer reads as an empty catalog.

Within one write (`add --from-json`, `arc seed --name …`), names resolved by
earlier rows are candidates for later ones, resolver or not.

## Reconcile

`arc reconcile <id>` checks each task's target against the live catalog:
`live`, `renamed`, `stale` or `uncheckable`. `--json` is a bare array of `id`,
`target_type`, `target_name`, `status`, `suggestion` and `needs_result`, as
found before any `--apply`. `--apply` moves each renamed item's old name onto
the live one across the whole store, as `rename` does. It does not close a
stale task: that takes `--resolve-stale`, so an unconfigured `[renames]`
cannot close real open work.
