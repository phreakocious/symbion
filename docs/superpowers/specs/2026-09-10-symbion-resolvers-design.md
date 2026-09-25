# Symbion resolvers — the catalog names, the project matches

**Date:** 2026-09-10
**Status:** Implemented. A design record: the reasoning still holds, and the details are as of this date. What symbion does now is in the code, the tests, README.md and SKILL.md. Dated notes mark the decisions reversed since.
**Amends:** `2026-09-04-symbion-design.md` §"Catalogs" (a catalog type's match rule is the built-in substring rule *unless the project declares a resolver command*); `2026-09-10-symbion-kinds-design.md` §"Out of scope" (this is the "next spec" it named).

**Motivation:** An idea filed on 2026-09-10 by an adopter whose target names are measured values. `catalog.resolve` is "exact, else unique substring, else the input; ambiguity refuses". The refusal half is what such a project depends on and is kept. The matching half fragments a numeric catalog silently: `3.1416` is not a substring of `3.14159`, so it falls through to "the input" and mints a second target. Nothing raises, and the investigation then has two heads — the outcome arcs exist to prevent. Measured quantities vary in their trailing digits by construction, so for a numeric type this is the normal case, not an edge. What such a project wants instead is a numeric rule: match inside a window, return the canonical form on a miss, and refuse on more than one hit. This spec lets a project declare that rule, as a command, beside the catalog it applies to. Symbion still knows nothing about what the names measure.

---

## Evidence

Measured 2026-09-10 in `src/symbion`.

- `catalog.resolve(query, candidates)` has two callers: `catalog.canonical` (every write and read that names a target — `add`, `list --name`, `context --target`, refs) and `api.seed_names` (`arc seed` with explicit names, and a sweep with none, where every pool name resolves to itself by exact match).
- Every configured command — catalogs, renames, the provenance override — runs through `catalog.run_configured`: cwd `work_root`, `stdin=DEVNULL` so a prompting command fails instead of hanging, one timeout. A resolver needs input on stdin, so the runner gains an `input=` argument; every other caller keeps stdin closed.
- A target type exists only if it is built in (`commit`, `project`, `arc`, `item`) or is a `[catalogs]` key (`api.resolve`: `target_types`, `arc_scopes`, `seed_scopes`). The type is declared by its catalog.
- `catalog.names` refuses empty output ("produced no names") because a seed sweep of zero names would create nothing and report success. `canonical` calls `names`, so an empty catalog is an error on `add` today.
- **Every write canonicalizes before it locks.** `api.add_many` and the CLI's `add` build every row's fields, catalog run included, and only then does `store.add_many` take `_lock` and append; `seed_names` resolves before `seed_arc` locks; `supersede` canonicalizes refs before `store.supersede` locks. So a batch holding `40.40` and `40.45` resolves both against the same store and mints two targets where sequential adds mint one (reproduced by the 2026-09-10 review with a temporary adapter), and two concurrent adds have the same window. The catalog also runs once per row, not once per write.
- `run_configured` is `/bin/sh -c`, and a pipeline's status is its last command's: a `symbion` that fails at the head of `| jq | sort -u` exits 0 with empty output. Today the empty guard catches that; the guard this spec lifts for resolver types would not.
- A numeric type's catalog is naturally the store itself: every row (not heads), the type's distinct names. Under symbion that is `symbion list --all --json --type reading` piped to the names. `store.load` takes no lock and `list --type` without `--name` runs no catalog, so the nested call cannot deadlock or recurse.

## The protocol

A resolver is a shell command, declared per target type, that replaces the built-in match rule for that type and nothing else. Symbion runs the catalog first, exactly as today, then the resolver:

- **cwd, timeout, environment:** as every configured command (`run_configured`).
- **stdin:** line 1 is the query, NFC-normalized; every following line is one catalog name, verbatim as the catalog emitted it. A catalog with no names (a store-derived catalog before its first row) gives a stdin of one line.
- **exit 0:** the first non-blank stdout line, verbatim, is the name to store or query; further lines are ignored. It may be outside the candidate list: that is how a first sighting mints its canonical form (`40` → `40.00`), and it is the only way a miss is expressed. A resolver that wants the input stored echoes it. Empty stdout is an error, not a miss.
- **exit 2:** ambiguous. stdout is the matches, one per line, blank lines dropped; symbion raises `catalog.AmbiguousName(query, matches)` — the existing error, so the CLI's `error:` line and the GUI dialog need no change. Exit 2 with no matches is an error: an ambiguity with no candidates is a resolver bug.
- **anything else** — another exit status, a timeout, exit 0 with empty stdout — is `CatalogError` naming the command and the first 400 bytes of stderr. **A resolver failure never falls through** to the built-in match or to the input. The failure this spec exists for was silent; a broken resolver that silently minted duplicates would be the same failure with more moving parts.

No miss code (a resolver decides what a miss stores), no type on stdin (the command is per type; a shared script takes the type as an argument), no candidates on argv (a name is data, not a shell word).

## The table

```toml
[catalogs]
# set -o pipefail is load-bearing: /bin/sh reports the LAST command's status,
# so without it a failed `symbion` reads as an empty catalog and the resolver
# mints a fresh name. A shell without pipefail fails the line loudly instead.
reading = "set -o pipefail; symbion list --all --json --type reading | jq -r '.[].target.name' | sort -u"

# A resolver replaces the built-in match rule (exact, else unique substring,
# else the input) for ONE catalog type. stdin: the query on line 1, then one
# catalog name per line. Exit 0: the first non-blank stdout line is the name
# to store, in or out of the list. Exit 2: stdout lists the matches, one per
# line; symbion refuses as ambiguous. Anything else is an error and stores nothing.
# A NUMERIC catalog without a resolver fragments: 3.1416 is not a
# substring of 3.14159, so it silently becomes a second target.
[resolvers]
# reading = "python3 tools/resolve_reading.py"
```

A `[resolvers]` key with no matching `[catalogs]` key is refused at config load, naming the key and the toml path. The catalog declares the type; a resolver alone is a missing line or a typo, never a type.

## Behaviour that changes

**`catalog.py`.** `run_configured(cfg, cmd, input=None)`: `input` given means `stdin=PIPE` with that text, else `DEVNULL` as today. `names(cfg, type, *, allow_empty=False)`: the empty guard stays the default; a non-zero exit is an error whatever stdout holds, before the emptiness check, as today. Two new functions. `resolve_with(cfg, target_type, query, candidates)` runs the configured resolver and applies the exit contract above. `match(cfg, target_type, query, candidates)` is the one picker: the resolver when `target_type` is in `cfg.resolvers`, else `resolve` as today. `pool(cfg, target_type)` is `names(cfg, type, allow_empty=type in cfg.resolvers)`, and only a caller that holds a query to resolve may use it — a sweep has no query and calls strict `names`, so an empty sweep still fails rather than seeding nothing with success. `canonical(cfg, type, name, pending=())` becomes: no catalog → `nfc`; else `match(cfg, type, q, candidates)` where `candidates` is the order-preserving union of the catalog's names and `pending` (`dict.fromkeys`), never a concatenation: an answer that was already in the catalog would otherwise appear twice, and a second identical query would then hit two candidates and be refused as ambiguous, with the built-in rule and with any resolver that counts lines. `resolve` itself is untouched.

**`config.py`.** `Config.resolvers: dict`, loaded from `[resolvers]`; the load-time check that every key is in `catalogs`.

**Write-time resolution, under the lock.** Every write canonicalizes inside the store lock, in row order, with the names this write has already resolved visible to the rows after them. `store.add_many`, `store.seed_arc` and `store.supersede` gain a `canonicalize=` callback, called inside `_lock` over every row before any is appended; a raise there writes nothing, which is the validation-before-write promise those functions already make. `api.canonicalize_rows(ctx, rows)` is the callback, passed by `api.add_many`, the CLI's `add`, `api.seed` and `api.supersede`: it runs each catalog once per write (a cache keyed by type for the call), and for every target or ref of a catalog type matches against the catalog's names plus a `pending` list of the names this write has already resolved for that type, appending each answer. A batch of two neighbouring readings resolves the second against the first; two concurrent adds serialize on the lock, and the second's catalog run sees the first's row on disk. The callback is the existing `gitref.canon_name` rule moved under the lock, plus `pending`: a `commit` is still peeled to its full object id there, a built-in non-canonical type is still NFC only, and only a catalog type matches. `fields_from_row` stops canonicalizing and keeps names raw (NFC only); validation of kind, type and shape stays where it is, outside the lock. `api.canon_refs` splits: `check_refs` (ref shape, ref type, NFC — unlocked, the error a caller can report before locking) and resolution in the callback. **The CLI's `supersede --ref` passes raw refs**: today it canonicalizes them before `api.supersede` for its own error message, and under this design that early resolution would hand the lock an already-canonical name, which resolves by exact match even when the original query has become ambiguous since (another writer added a neighbour in between). The CLI keeps `check_refs` for the message and nothing more. `api.supersede` resolves only the refs the caller supplied; the inherited target and, when no `--ref` is given, the inherited refs are never re-resolved, as its docstring already promises. Read paths — `list --name`, `context --target` — stay unlocked: nothing is minted there.

A configured command must not write to the store. It would wait on the lock its caller holds until the timeout kills it: bounded and loud, but pointless. The starter toml says so.

**`api.py`, seeding.** The sweep distinction has to survive into the write, and today it does not: `seed_names` returns a plain list either way and the CLI and the GUI's seed-all both hand it to `api.seed`, so an unconditional callback would resolve sweep names too — a numeric resolver could even refuse a catalog name as ambiguous against its own neighbours. So `api.seed(ctx, arc_id, scope, names=None)` takes the raw input. `names` empty or `None` is a sweep: strict `catalog.names`, run unlocked as today, passed to `seed_arc` verbatim with no callback, resolver run zero times (each pool name resolving against the pool by exact match was a no-op; this is the same result). `names` given is explicit: NFC-raw into `seed_arc`, resolved under the lock through the callback, each against the pool plus the ones before it. The CLI passes `args.names`; the GUI's seed-all passes `None`. `seed_names` stays as the unlocked preview behind `arc seed --dry-run` (it writes nothing, so there is no race to close) and prints what a real seed would store.

**`summary.py` / `schema`.** The targets list gains `resolver` beside `catalog` for a type that has one (`null` otherwise), so the hook and `symbion schema` show which types match by command.

**Docs.** The starter toml gets the block above (noted 2026-09-25: commented out, as an example to uncomment). `src/symbion/data/SKILL.md` gets a "Resolvers" paragraph beside the catalogs one, then `symbion init` here. README's config section names the table.

**Cost.** A canonicalization on a resolver type is the catalog once per write plus the resolver once per name, instead of the catalog once per row. For a store-derived catalog the catalog is a `symbion` start, a few hundred milliseconds. The lock is held across every resolution in the write, so the worst case is rows × command_timeout (45 rows at the 30 s default is over twenty minutes); readers never wait, writers wait for the whole write. A per-row release would let a neighbour mint between rows, so the ceiling stays.

## Errors

| condition | raised where | error |
|---|---|---|
| resolver key, no catalog key | `config.load` | `ValueError` naming the key and the toml path |
| exit 0, empty stdout | `resolve_with` | `CatalogError` naming the command |
| exit 2, matches | `resolve_with` | `AmbiguousName(query, matches)` |
| exit 2, no matches | `resolve_with` | `CatalogError` naming the command |
| other exit, timeout | `resolve_with` | `CatalogError` naming the command and stderr |
| empty catalog, resolver declared, a query in hand | — | not an error; the resolver gets zero candidates |
| empty catalog, no resolver | `names` | `CatalogError` "produced no names", as today (amended 2026-09-20: `pool` raises its own error, naming the deadlock: a store-derived catalog with no resolver can never mint its first row) |
| empty catalog on a seed sweep, resolver or not | `names` | `CatalogError` "produced no names": a sweep has no query |
| catalog exits non-zero, empty stdout, resolver declared | `names` | `CatalogError` naming the command and stderr; the exit is checked before emptiness |

## File layout after

```
../<repo>-notes/
  symbion.toml       # + [resolvers], commented in the starter; absent = built-in match everywhere
```

No new file, no migration, no change to `notes.jsonl`.

## Testing

Both directions: each test is shown to fail on the bug it guards. Resolvers are shell one-liners or two-line scripts written to `tmp_path`, the way `test_catalog.py` already drives catalog commands.

- **Exit 0 stores the printed name**, including one outside the candidate list; the stored row's `target.name` is the printed string, not the query.
- **Exit 2 raises `AmbiguousName`** whose `candidates` are the printed lines, in order; the `add` stores nothing. Exit 2 with nothing printed raises `CatalogError`.
- **Never fall through.** Exit 1, a timeout, and exit 0 with empty stdout each raise `CatalogError`, and the store afterwards has no row whose name is the query and none whose name is the built-in match. This is the row's failure mode, so the assertion is on the absence.
- **stdin shape.** A resolver that prints line 2 stores the first catalog name; one that prints line 1 stores the query. Names with a trailing space arrive intact.
- **Empty catalog.** With a resolver declared, a catalog printing nothing resolves through the resolver with a one-line stdin; without one, `add` still raises "produced no names". An `arc seed --scope <type>` sweep on that empty catalog raises "produced no names" and seeds nothing, resolver declared or not.
- **A failing catalog never reads as empty.** With a resolver declared, a catalog of `false | cat` resolves (the shell reports `cat`'s status: this pins the trap), and `set -o pipefail; false | cat` raises `CatalogError` and stores nothing — on a shell without pipefail the line itself fails, which is the same assertion.
- **Pending names.** One `add --from-json` carrying `40.40` then `40.45`, under a test resolver that matches inside 1.0, stores one target; the reverse order stores the other name for both (row order is the rule). `arc seed --name a --name a2` under a prefix-matching resolver seeds one row. Explicit refs in the same write resolve against pending too. The catalog runs once per write, checked by a catalog that appends to a counter file.
- **Pending never duplicates.** Two rows in one `add --from-json` both naming `parser` against a catalog of `src/parser.py`, with NO resolver, both store `src/parser.py`; the same under a resolver that prints the count of matching lines stores it twice, not `2`.
- **The original query reaches the lock.** `supersede --ref reading:41.25` under a resolver that writes its stdin to a file: the file's first line is `41.25`, not a name resolved earlier. A `supersede --body` with no `--ref` on a row carrying refs runs the resolver zero times and keeps the refs verbatim.
- **Sweeps bypass the resolver.** With a resolver declared that appends to a counter file: `arc seed --scope <type>` from the CLI and the GUI's seed-all button each seed every catalog name and leave the counter empty; `arc seed --name x` increments it once.
- **Under the lock.** The callback runs with the store lock held: a test callback attempts a non-blocking `flock` on the lock file and must fail. And a second add started while another thread holds `_lock` resolves against the row that thread appends before releasing (a store-derived test catalog that reads `notes.jsonl`); the assertion is one target, and its falsifier — canonicalizing before the lock — is the code as it stands today.
- **Config.** A `[resolvers]` key absent from `[catalogs]` fails load naming the key; a matching key loads; no table means `resolvers == {}` and the built-in rule on every type (a substring still expands, an ambiguity still refuses, a miss still stores the input).
- **Every caller.** `add`, `add --from-json`, `list --name`, `arc seed --name` and `supersede --ref` each store or match the resolver's answer, one CLI test each. `arc seed --scope <type>` with no names runs the resolver zero times, checked by a resolver that appends to a counter file; `--dry-run` prints the catalog verbatim.
- **Surfaces.** `schema --json` carries `resolver` for the declared type and `null` for the other; the starter toml written by `init` contains `[resolvers]`; `tests/test_init.py::test_this_repo_copies_match_package_data` still passes after the SKILL.md edit.

## Producer → consumer

This work produces the protocol, the table, and the docs. Its consumer is a project's resolver script (stdin per the protocol, a match window, one canonical precision, exit 2 on more than one hit) and its store-derived catalog line (with `set -o pipefail`, as above). Neither half is done until both are: a protocol with no script is unexercised, and the script cannot exist without the protocol.

## Out of scope

- Resolvers on built-in types. `commit` keeps its peel to a full object id; `item`, `project` and `arc` stay uncanonicalized. The idea's mention of a git ref was an illustration of what a command can do, not a request.
- A miss exit code, a numeric `tolerance` shorthand under `[catalogs]`, candidates on argv, the type on stdin.
- The reconcile path. `reconcile_arc` compares stored names against the live catalog by set membership and is untouched; a store-derived catalog makes every stored name live by construction.
- An adopting project's migration itself, and any of its types that has no catalog — a type is declared by a catalog, so the migration gives such a type one or maps it to `item`.
