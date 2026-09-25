# Symbion — a git-keyed project-management layer for small projects

**Date:** 2026-09-04
**Status:** Design approved (brainstorm); spec under review before the implementation plan.

**Motivation:** Professional software teams tie work together with tooling that lives *outside* the git repo — ticket systems, epics, decision records — because branches and commits cannot express "why", cannot be edited after the fact, and cannot carry state across the moment they were written. Small projects get none of that, and fall back on `CLAUDE.md`, scratch markdown files, and chat. All three drift append-only and none is queryable.

We already built the hard part twice. Symbion's ancestor, the research notebook it is extracted from — a retractable lab notebook of append-only JSONL with supersede chains, plus a first-class activity registry — is running in two research projects, vendored byte-identically with a per-project domain module as the seam and `diff` as the drift check. Symbion extracts that core into a real package, replaces the project-specific seam with configuration, and adds the one target type a software project actually has and a research corpus does not: **the commit**.

---

## Problem

A conclusion reached while working — "this is what broke the parser", "kept the naive scan because the profiler said it wasn't hot", "audited the suite on `d36bc6e`, 412 passed" — has nowhere durable to live. The commit message is the closest thing, and it structurally cannot hold any of it: it is written before the consequence is known, it cannot be edited, and it cannot be retracted. `CLAUDE.md` and memory files grow append-only, which is the systematizer-crank tell: **retraction is the weak leg**. Chat evaporates.

There is also no way to run a unit of work *across* many objects to completion. "Add a real assertion to every test that currently has none" is a campaign over a set, with per-item state, and nothing in a small project's toolchain tracks that.

## Goal & non-goals

**Goal:** A per-project, private, git-versioned, queryable JSON notebook. Each note is typed, dated, attributed, retractable, and attached to a stable target — a commit, a declared catalog object, or the project globally. On top of notes sits an **activity** registry: named tickets/epics that render as per-object checklists, seedable across a catalog defined by a shell command. Both a human and Claude read and write the same store through one CLI, and a SessionStart hook surfaces the summary so the store is never write-only.

**Non-goals (YAGNI):** auth, multi-user, sync/remote, full-text search, a link-graph view, evidence staging, a plugin mechanism, activity *deletion* (archive instead), auto-commit, and any built-in `file`/`path` target type. (A project may *declare* a catalog named `file`, as the examples below do — the non-goal is symbion shipping one and treating paths as special.) The five note kinds are fixed. (Amended 2026-09-08: seven kinds, and the vocabulary is replaced — see `2026-09-08-symbion-vocabulary-design.md`.) (Amended again 2026-09-10: the bits are fixed, the labels are the project's.) Migrating those two projects onto the package is explicitly deferred (see **Migration**). (The GUI non-goal was reversed on 2026-09-08 — see
`2026-09-08-symbion-gui-design.md`. Every other non-goal here stands.)

---

## Load-bearing rationale

Three decisions future readers will want the reasoning for.

### 1. The store is a sibling repo, never a directory in the project

The ancestor's reason was a trust boundary. Symbion's is different and stronger: **if the notes live in the project repo, checking out an old branch time-travels your ticket system.** A ticket that disappears on `git checkout` is not a ticket system. A sibling repo makes the notebook branch-independent, and makes a note's reference to a commit sha a one-way pointer into a repo that the notebook never has to be consistent with.

Default location is `<project-root>/../<project-name>-notes`, where **project root is the main checkout, not the current worktree**. `git rev-parse --show-toplevel` is wrong here: inside a worktree it returns the worktree path, so `project-wt1/` would get its own `project-wt1-notes` store and the notebook would fragment one-per-worktree — exactly backwards, since worktrees are parallel lines of work on the *same* project and must share one ticket system. Derivation is **the first `worktree` record of `git worktree list --porcelain`**, which names the main worktree directly (verified 2026-09-04). `dirname(--git-common-dir)` is *not* used: it holds for conventional repos and linked worktrees but is wrong for submodules, where the common dir is `<parent>/.git/modules/<sub>`, and for repositories created with `--separate-git-dir`. `worktree list` reports the checkout path in every one of those layouts, and needs no `--path-format` handling. `SYMBION_DIR` overrides. `git init`'d locally, no remote.

**`project_root` names where the store lives. It must not also be used to resolve HEAD, branches, or dirtiness — those are per-worktree by definition, and a Critical defect found by functional probing after the first fix wave was exactly this conflation.** `Config` therefore carries a second path, `work_root`, and the two are used for disjoint purposes:

| root | answers | derivation | used by |
|---|---|---|---|
| `project_root` | where is the store? | first record of `git worktree list --porcelain` (the MAIN worktree) | `config.store_dir` only |
| `work_root` | what tree is the caller actually in? | `git rev-parse --show-toplevel` (the CURRENT worktree — the one place this call is correct) | `gitref._git` (so `canonical_commit`, `provenance_stamp`, `audit_state`, `branch_commits`, `subjects`) and `catalog.run_configured` (so every catalog, rename, and provenance-override command) |

Before this fix, `gitref._git` ran every git operation with `-C project_root`, so **every ref, state, and configured-command resolution from a linked worktree silently ran against the main checkout instead.** Measured 2026-09-04 from this repo's own linked implementation worktree (HEAD `0f3bf221c34a…` on branch `worktree-symbion-impl`; main checkout at `4585fd7f9ea0…`):

```
symbion add --kind audit --type commit --name HEAD
  -> stored target.name = 4585fd7f9ea0…   (the MAIN checkout's HEAD)
     actual HEAD here   = 0f3bf221c34a…
```

The blast radius was every git-derived read or write that isn't the store path itself: `canonical_commit` froze `HEAD`/branch/tag targets onto the wrong commit; `provenance_stamp` recorded the wrong sha *and* the wrong `dirty` flag — an audit could read `dirty: false` while the actual tree the caller was standing in was dirty, which is the single worst outcome this field exists to prevent; `audit_state` compared against the wrong HEAD, so `current`/`behind`/`diverged` were computed against the wrong line of history; `branch_commits` resolved `<base>..<ref>` in the wrong checkout; and a configured catalog command (`git ls-files '*.py'`) enumerated the wrong tree — silently, since a non-empty file list from the wrong repo is indistinguishable from a correct one. `gitref.uncommitted(store)` was never affected: it takes the store path directly, not a `Config`.

This is why the existing suite (130 tests) passed while the bug was present: every fixture builds a plain repo with no linked worktree, where `project_root` and the correct ref/state root are the same path by construction, so nothing in it could tell the two roots apart. The regression tests added with this fix create an actual linked worktree with a commit and a dirty file the main checkout does not have, and assert from "inside" it.

**Config precedence.** `Config.work_root` defaults to `Config.project_root` when not given — this keeps every hand-built `Config(project_root=...)` in the test suite, and every plain non-worktree repo, behaving exactly as before, since the two roots coincide there. `config.load()` takes an optional `work_root` parameter (the CLI passes `config.work_root()`, i.e., the actual current worktree) for the general case. An explicit `project_root =` in `symbion.toml` **also pins `work_root` to the same value**, unless `work_root =` is *also* named explicitly in the file (which always wins): hand-pinning the root is read as "operate on this tree," full stop, not "only change where the store lives." One consequence this decision forces on `_init`: the generated `symbion.toml` must NOT write an uncommented, literal `project_root = "<value>"` by default — doing so would silently pin `work_root` to the main worktree for every project from the moment it runs `symbion init`, reintroducing this exact defect through configuration instead of code. Both `project_root` and `work_root` are written commented-out, documenting the default and the pin, and require deliberate action to enable.

### 2. JSON-in-git, not SQLite

Inherited verbatim from the ancestor's design and still correct: SQLite is a binary blob whose git diff is useless, and the retraction discipline depends on visible, dated history. JSONL gives a clean append-diff per note and `git log -p notes.jsonl` reads as a real timeline. Queryability is recovered by loading the file and filtering in Python; at hundreds-to-low-thousands of notes that is free.

### 3. A catalog is a command, not a registry

The obvious design is a hand-declared registry of components, created the same way activities are. It is wrong, and the reason is worth recording because it is not obvious.

The ancestor's catalog has three properties that make fan-out seeding worth its machinery:

1. **Externally authoritative** — the project's own pipeline produces it. You re-read it; you do not maintain it. That is what gives `reconcile` something to check *against*.
2. **Large enough that hand-enumeration hurts** — dozens of members.
3. **Homogeneous** — every member is the same kind of thing, so a predicate like "all produce float64" applies uniformly.

A hand-declared component list has none of them. It would hold a dozen heterogeneous entries, so the fan-out saves nothing a hand-written list wouldn't; and because you maintain it yourself it **cannot be wrong**, which means reconcile has nothing to detect. That design keeps the machinery and discards the thing that justified it.

A catalog type is therefore **a shell command that emits one name per line**, and the protocol is strict: blank lines are stripped, every other line is a name verbatim, and nothing else may reach stdout. `git ls-files '*.py'`. `git ls-files 'tests/test_*.py'`. `grep -rl 'ponytail:' -- .`. Re-run it and you have truth; diff it against the frozen followup targets and drift falls out, which is `reconcile_activity` working as designed instead of against a list that cannot disagree with you. A hand-declared list survives as a special case — `cat components.txt` is a valid command — it just does not get a bespoke subsystem.

**A catalog command's output format is not knowable from documentation — only by running it here, at this version, today.** The worked example is `pytest --collect-only -q`, which was cited in an earlier draft of this spec as emitting per-file counts (`tests/test_store.py: 39`) rather than names. That was measured, and it does not generalize: pytest **9.0.2** emits per-file counts for those flags, while **9.1.1** emits one full node id per line and moves the counts to `-qq` (both verified 2026-09-04). The format changed between two *patch* releases of the same tool. So the failure mode this protocol invites is not one bad command it is worth memorising — it is trusting any remembered format at all. `symbion activity seed --dry-run` exists for exactly this: it resolves and prints the names and creates nothing, so a catalog is verified in the project and version that will use it, before it mints followups against garbage. *Amended 2026-09-25:* the version was not the cause. The two measurements came from two repos as well as two versions; in one repo, pytest 9.0.2 and 9.1.1 both print node ids for `--collect-only -q`, and both print per-file counts once the pytest config adds `-q` to `addopts`, so the flags stack to `-qq`. The protocol stands, and the mistake is its own example: what a command printed in another project says nothing about what it prints here.

**Every configured command runs through one bounded helper** — catalogs, renames, and the provenance override alike: `cwd=work_root` (the CURRENT worktree — see §1's `project_root`/`work_root` distinction; this is deliberately not `project_root`, which names the main worktree), `stdin=DEVNULL`, and a `command_timeout` (default 30s, configurable, because listing files in a monorepo and in a five-file repo differ by orders of magnitude). stdin is closed so a command that prompts — a credential prompt, say — fails instead of hanging; the timeout bounds everything else. These commands are arbitrary user shell run by an unattended agent, so an unbounded hang is an operational defect, not a hypothetical. Without this, `git ls-files '*.py'` invoked from anywhere else silently enumerates the wrong repository (or the notes store itself) and returns a plausible non-empty list, which is the worst failure shape available: a wrong answer that reads as a right one. And run from the *wrong worktree* — the main checkout instead of a linked one with a feature branch's added files — it is exactly that failure shape: a plausible, non-empty, wrong list.

Commands come from a config file in your own store, run by you, against your own repo: the same trust level as a Makefile. This is not a sandbox boundary and is not treated as one.

---

## Architecture

### Store

Three files in the store directory, auto-created on first write (`git init` + empty files + `.gitignore` for `.lock` and `.tmp.*`):

- **`notes.jsonl`** — append-only, one note per line, supersede chains.
- **`activities.jsonl`** — the activity registry, rewritten in place (atomic: temp + fsync + rename).
- **`symbion.toml`** — configuration. Optional; every value has a derived or literal default.

### Note schema

Field names are **identical to what the reference already writes**, including the reader accepting the legacy `atlas` key as a synonym for `provenance`. The only schema change is `target.type` accepting `commit`.

```json
{"id": "20260904-141502-004311-a3f", "kind": "audit",
 "target": {"type": "commit", "name": "d36bc6e91f2a…"},
 "created_at": "2026-09-04T14:15:02", "author": "phreakocious",
 "body": "full suite after the parser rewrite",
 "checked": "pytest -q", "result": "412 passed, 0 failed",
 "measurements": {"passed": 412, "failed": 0, "seconds": 3.2},
 "provenance": {"sha": "d36bc6e…", "dirty": false},
 "status": null, "activity_id": null, "supersedes": null,
 "tags": ["suite"], "refs": [], "evidence": []}
```

- **`kind` ∈ {`note`, `anomaly`, `decision`, `followup`, `audit`}** — unchanged, and they transfer to software without strain: `decision` is an ADR, `anomaly` a known-broken, `followup` a ticket item, `audit` a dated verification, `note` the catch-all. **(Vocabulary replaced 2026-09-08: `followup`→`task`, `anomaly`→`bug`, `audit`→`check`, plus `idea` and `question`; `activity_id`→`arc_id`, `global`→`project`. See `2026-09-08-symbion-vocabulary-design.md`.)**
- **`target` is `{type, name}`.** Built-in types: `commit` (name = full sha), `global` (name = null), `activity` (name = the activity id), and **`item`** (name = a freeform string). Every other type comes from a `[catalogs]` key.
- **`item` is the no-catalog target type.** Its names are stored NFC-normalized exactly as typed, never canonicalized, and it is always `uncheckable` in reconcile. It exists because the seed path constrains `--scope` to the catalog types: with no `[catalogs]` declared that set is empty, and `activity seed <id> --name X` would have no legal `--type` to attach the followup to. `item` is that type, so a plain checklist needs no catalog and no configuration.
- **`status` is determined by `kind`, not by the caller.** (Amended 2026-09-10: by the kind's `status` bit — see `2026-09-10-symbion-kinds-design.md`.) `add` defaults `status="open"` for `anomaly` and `followup`, and rejects `--status` for `note`, `decision`, and `audit`, which store `null`. The reference defaults `--status` to `None` for every kind, so `add --kind anomaly` without an explicit flag stores `status: null` and is then invisible to `query(status="open")` and to every open-items view — an anomaly that silently never surfaces. Enforced on write; **tolerated on read**, where an `anomaly`/`followup` with `status: null` is read as **open**. Null-as-open is the fail-safe direction: an unfinished item wrongly shown costs a glance, one wrongly hidden costs the whole point of the store. Read-side tolerance is also what keeps the ancestor's existing stores loadable. **Amended 2026-09-11: the read-side tolerance is gone.** Measured across the four symbion stores then in use: zero rows relied on it, and the only store holding null-status rows was a new one, built that day by a hand transform. The ancestor is not evidence either way -- its store is not a symbion store, and of its rows symbion loads a small share, none on a status kind, where the guess inflated the session-start headline ~4x (48 open rows where 11 were open and 38 had no status). `read_status` now returns what was written; a null status on a status kind is a defect in the FILE, and `summary` reports the count on every run while any remain, so such a row is still named without being guessed at. The fail-safe argument holds at small N and inverts at large N: 38 rows wrongly shown is not a glance, it is a headline that stops meaning anything. **The guard applies on `supersede` too, not just `add`.** `resolve` is `supersede(status="resolved")` unconditionally, and plain `supersede --status` merges its fields onto the inherited row with no kind check of its own — either path could hand a `note`/`decision`/`audit` head a status `add` itself would have refused. A correction never changes a note's `kind` (there is no `--kind` on `supersede`), so this is not "corrections may carry a status the original could not" — it is the same invariant, closed at the one shared write path (`_check_status_kind`, called from both `add` and `_supersede_unlocked`) instead of only the one a reviewer happened to look at.
- **`author`** resolves through a fixed chain: `--author` → `SYMBION_AUTHOR` → `"claude"` if `CLAUDECODE` is set in the environment → git `user.name` → `"user"`. Both a human and Claude drive the same CLI, so the invocation itself carries no signal. A SessionStart hook *cannot* supply it — each agent shell is initialized fresh from the user's profile, so an `export` in a hook process never reaches a later CLI process. `CLAUDECODE=1` is set in the agent's shell environment (verified 2026-09-04), which makes the attribution self-configuring and impossible to forget; `.claude/settings.json` `env` can set `SYMBION_AUTHOR` explicitly where that is preferred. **Known misattribution:** a human running `! symbion add …` from inside a Claude Code session is in that same environment and will be recorded as `claude` unless they pass `--author` or export `SYMBION_AUTHOR` in their profile. Detection is a default, never an override.
- **`author` describes who wrote *that row*, and is never inherited.** `supersede` refreshes `id` and `created_at`; it must refresh `author` too. Copying it forward — which the reference does — attributes a correction to the person being corrected, and attributes every `resolve` to whoever wrote the note rather than whoever closed it. A row with a fresh id, a fresh timestamp and a stale author is incoherent, and "the timeline is the history" only holds if each row says who wrote it. The store never guesses: a direct API caller that supplies nothing gets `"unknown"`, not `"claude"` and not the inherited value, because an obviously-absent attribution beats a plausible false one. Identity resolution lives only in the CLI, which passes `_author_default()` at every write site — including the `supersede` calls made internally by `apply_reconciliation` and `rename_target`.
- **Corrections append, never mutate.** A retraction is a new row with `supersedes: <old-id>`; the old row stays. Ticking a checkbox is exactly this.
- **A correction targets the chain tip, not the literal id given.** `supersede(old_id)` walks the supersede links from `old_id` to the live head before building its row, inside the lock. Without this the transaction lock is not enough: two serialized calls naming the same `old_id` each append a row pointing at it, so `heads()` returns two heads where the model allows one. It also fixes the single-threaded case, which is the common one — correcting a note from an id copied out of an earlier listing must extend the chain, not fork it. The cost is that a correction inherits the *current* head's fields rather than those of the row the caller named; that is the intended reading of "correct this note".
- **`measurements`** (flat, finite numbers and strings) and **`evidence`** (store-relative paths) are retained from the ancestor's schema. Evidence *staging* is not ported. Copy a file into the store yourself if you want one.

### Activity registry

Unchanged from the reference: slug `id` stable across rename, `target_scope` advisory, archive-never-delete, membership = a head `followup` carrying the `activity_id`, state = one head per `(activity_id, target_type, target_name)`, progress = resolved/total over those heads. **(`activity` is `arc` as of 2026-09-08, and an `idea` is never a checklist item. See `2026-09-08-symbion-vocabulary-design.md`.)**

An activity is a ticket or an epic. Seeding it across a catalog produces one open followup per catalog member. **Activities work with no catalog at all**: `activity seed <id> --scope item --name X --name Y` attaches explicit targets to the built-in `item` type. This requires widening `--scope` from the reference's `choices=sorted(CATALOG_TYPES)` to the catalog types *plus* `item` — with no catalogs declared the reference's choice set is empty and the subcommand is unusable. So a small heterogeneous project uses activities as plain checklists and never declares a catalog, while a project with 200 test files declares one command and gets the ancestor's behaviour. Same code at both ends.

### Configuration

```toml
# project_root = "/path/to/project"                 # default: first worktree of `git worktree list --porcelain`
#                                                    # (the store's location -- see §1). Setting this also pins
#                                                    # work_root to it, unless work_root is set below too.
# work_root = "/path/to/project"                    # default: the CURRENT worktree (`git rev-parse
#                                                    # --show-toplevel`) -- where HEAD/branch/dirty state and
#                                                    # every configured command (catalogs, renames, the
#                                                    # provenance override) resolve. Never project_root.
git_name  = "symbion-notes"
git_email = "symbion-notes@local"
stale_target_noun = "tracked file"
default_branch = "main"                            # for the derived branch view

[catalogs]
file = "git ls-files '*.py'"
test = "git ls-files 'tests/test_*.py'"
debt = "grep -rl 'ponytail:' -- ."

[renames]
file = "git"                                       # built-in NUL-safe rename detection

[provenance]                                       # optional override; default is built in
command = "python -c 'import json,sys; json.dump({\"schema\": 7}, sys.stdout)'"
render  = "@schema{schema}"                        # format string over the emitted object
```

The whole of the ancestor's domain-module contract maps onto this table or onto a built-in. There is **no plugin mechanism** — no entry points, no module discovery, no same-named-file discipline. The seam existed because there was no package to depend on; once there is one, it has no job left, and a hook with zero implementations at v1 is an abstraction we would be maintaining for nobody.

### Name resolution

**Catalog types** resolve through one generic resolver: exact match, else unique substring match, else fall back to the NFC-normalized input. Ambiguity returns candidates and **refuses** — never a nearest guess. A miss is not an error: a note on a departed or never-cataloged target is valid, and `supersede`/`resolve` inherit the stored target rather than re-resolving, so a departed target stays editable.

**`commit` gets one built-in exception**, and it is not fuzzy matching: canonicalization is `git rev-parse --verify --end-of-options '<input>^{commit}'`, expanding a short sha to a full object id. This earns the special case because short shas are how both a human and Claude refer to commits, and storing `a3f9c1` unexpanded silently breaks every equality match against the same commit written long. On a rev-parse miss (rebased away, shallow clone) it falls back to the typed string. This also means **symbolic refs are accepted and frozen at write time**: `--target commit:HEAD` and `commit:main` both resolve to a full object id before storage (verified 2026-09-04), so a note can never drift onto a different commit as a ref advances. The `^{commit}` peel is what makes this a *commit* target rather than an object target: plain `--verify` happily accepts `HEAD^{tree}` and returns a tree id (verified), which would be stored as a commit and never match anything. Peeling rejects it — `<tree>^{commit}` fails with `Needed a single revision`. "Full object id", not "40 characters": SHA-256 repositories use 64.

`commit` is a target type but **not** a catalog type — you would never seed an activity across `git log`. Being immutable, it needs no reconcile, no rename map, and can never go stale.

### Branch is a derived view, never a target

A commit sha is immutable, permanent, globally unique, never renames. A branch name is mutable, deleted on merge, and freely reused — a note on `feature/parser-rewrite` is orphaned the week it merges, and if the name is later reused the note silently re-attaches to unrelated work. That is the `stale`/`renamed` problem in its worst form, with no rename map to recover it.

So nothing branch-shaped is ever stored. `symbion context --branch REF` computes notes on any commit in `<default_branch>..REF` at read time (`--since REF` overrides the base). Zero reconcile, and it still answers "what did we decide while working on this?"

### Provenance and staleness

The ancestor stamps audits with its catalog's sizes and build time so the read side can flag an audit as stale against a rebuilt catalog. The software analogue is exact and is **built in, not configured**: on `kind == "audit"`, stamp `{"sha": <HEAD at write time>, "dirty": <working tree dirty>}`.

Two things fall out for free:

- **`dirty: true` means the audit is not reproducible.** It was verified against a tree state that was never committed and no longer exists. Nothing else in the toolchain records that.
- **Generic staleness with no domain knowledge.** In the ancestor this check needed the built catalog; here git answers it. A boolean `merge-base --is-ancestor` is not enough, because the store is branch-independent *by design* — reading notes written on another branch is the normal case, not an edge case, so a bare "not an ancestor" would report most healthy audits as stale. Four states:

| state | condition | read as |
|---|---|---|
| `current` | audit sha == HEAD **and** `dirty` false | verified against exactly this tree |
| `behind` | ancestor of HEAD, distance N | `behind 37 commits` |
| `diverged` | commit exists, not an ancestor of HEAD | **unverifiable** — audited on another line of development |
| `unavailable` | `rev-parse` fails | **unverifiable** — rebased away, squashed, or shallow clone |

**`dirty` is a separate dimension and takes precedence over all four.** An audit stamped `dirty: true` always renders `unverifiable (dirty tree)` regardless of its sha relationship to HEAD — including when its sha *is* HEAD, which is the common case, since the stamp records HEAD at write time. Without that precedence the state table would contradict the very thing the dirty flag exists to record: a `current` reading on an audit run against a tree state that was never committed.

`diverged` and `unavailable` likewise report as **unverifiable, never as current**. The distinction matters because of squash and rebase merges: an audit run on a topic-branch commit that was squashed into `main` is describing work that *is* in the tree, but its sha is no longer reachable — reading that as "fine, different branch" would silently vouch for a verification nobody can re-check. Symbion says it cannot tell, which is the honest and the fail-safe answer. Changing the note's *target* does not help — provenance stamps HEAD regardless of what the note is attached to — so the only thing that survives a squash is **re-stamping**: a superseding audit written after the merge, whose provenance records the squashed commit that is actually in `main`.

`symbion list --kind audit` renders the state, and the distance whenever `audit_state` gives one (`current` is distance 0, `behind` is distance N — only `unverifiable`/`diverged` render no distance); `--json` carries both as `state`/`distance` fields.

`[provenance] command` + `render` remain as the override for a project wanting something else, but the default needs no configuration to be useful.

---

## CLI surface

A console-script entry point: `symbion …`, from any cwd. This deletes the entire footgun category the ancestor's skill has to document — no `-m`, no repo-root cwd requirement, no `sys.path` games, no venv path, and no argparse-position trap for `--dir` since the store is resolved before subcommand parsing.

The reference commands carry over unchanged: `add`, `list`, `resolve`, `supersede`, `commit`, `rename`, `tags`, and `activity {create,list,rename,archive,seed,todo,reconcile}`.

Five additions:

- **`--json` on every row-producing command** (`list`, `context`, `summary`, `activity todo`, `activity list`). This is the change that most directly serves "easy for Claude to use": it removes the need to parse `type:name  note-id` by treating the id as the last whitespace token.
- **`symbion summary`** — what the SessionStart hook runs, and what a human can run. Sections: open `anomaly` count, open standalone `followup` count, active activities one line each (`id  done/total  name`), open notes tagged `priority` in full, and the **uncommitted count**. Bounded by **explicit caps, not by expectation**: at most 10 activities (most open items first) and at most 10 priority notes, each body **flattened** (newlines, tabs, and other control characters replaced with a single space) and *then* truncated to 200 characters, with every elided section printing `+N more`. Flattening before truncating is what makes the cap real: a body is freeform markdown, so 200 characters can be 200 newlines, and an activity name can contain one too. Every field interpolated into the summary is flattened, not just bodies. Ceiling is roughly 30 lines regardless of store size. `--full` lifts the caps for a human reading it directly. Counts are always exact even when the listing is truncated — the cap hides rows, never changes a number.
- **`symbion context`** — pull-based detail, composed from existing `list`/`heads_for` calls with no new logic. `--target TYPE:NAME`, `--commit SHA`, `--branch REF [--since REF]`, or bare for project-wide open items and decisions. Uncapped in every mode, deliberately: `summary` is the bounded command and the hook runs that one, so capping a single `context` branch would be the inconsistency.
- **`activity seed --dry-run`** — prints the resolved names and the count, creates nothing. A malformed catalog command is otherwise discovered *after* it has minted 75 followups, one of which is a summary line. The skill directs a dry run the first time any catalog type is used.
- **`--add-tag` / `--rm-tag` on `supersede`** — the existing `--tag` *replaces* inherited tags, a footgun the ancestor's skill has to warn about. These add and remove alongside it, and give `priority` its mechanism independent of any GUI (and, since 2026-09-08, shared with one: the star button and the flag are two front-ends over `api.retag`): `supersede <id> --add-tag priority` is the star, and `summary` surfaces it.

`symbion init` writes an annotated starter `symbion.toml`; nothing requires it. It also installs `.claude/skills/symbion/SKILL.md` and `hooks/session_start.sh` from package data (`src/symbion/data/`), and writes `.claude/settings.json` registering the hook when that file is absent, printing the block when it exists rather than merging JSON. Re-running `init` keeps the toml and refreshes the two files, reporting each one it changed. Decided 2026-09-04: the earlier `cp` at adoption drifted from source within the hour in an adopting project. The repo's own copies are written the same way, and a test fails when they drift from the data files. With no config at all, the store path, project root, and identity all derive, and no catalogs means no fan-out seeding — the correct behaviour for a project that has not earned one.

**Committing stays manual.** A git failure must never block a write. The uncommitted count in `summary` is the nudge that replaces auto-commit.

**Counting uncommitted rows.** `git status --porcelain` cannot do it: 200 appended notes are still one changed path. Because `notes.jsonl` is append-only, added lines *are* new notes, so the count is `git diff --numstat -- notes.jsonl` (added field), plus `git diff --cached --numstat` for anything staged, and the file's full line count when it is untracked. `activities.jsonl` is rewritten in place, so a line count there is meaningless — it is reported as a boolean (`registry modified`), never as a number. Output reads `7 notes uncommitted, registry modified`, and is exact or absent, never approximate.

### The read side

A store Claude writes to and never reads is a pacifier. The ancestor avoids that because its main diagnostic command ends with a block of the notes on what it probed — the notes surface where the next investigation starts. A generic project has no such tool, so the equivalent is a **SessionStart hook running `symbion summary`**, whose output is bounded by design and whose counts are what tell Claude whether pulling detail with `symbion context` is worth it.

Recent `decision` heads are deliberately **not** in the summary. They are the highest-value thing not to re-litigate, but also the fastest-growing set and the one most likely to turn the injection into wallpaper. The user can say "go pull from there."

---

## Data flow

```
add note        → resolve target (catalog resolver, or git rev-parse for commit)
                → stamp provenance if kind == audit
                → append one notes.jsonl line
add --from-json → one row per line in the `list --json` shape; the flag form is
                  the one-row case of the same path
                → every row built and validated (kind, type, status/kind, keys
                  outside the input shape refused by line) BEFORE any append,
                  under one lock, ids minted unique within the batch
                → append N notes.jsonl lines, print N ids in input order.
                  Added 2026-09-05: two bootstraps wrote dozens of rows as one
                  process each, ~5 s and a shell-quoted markdown body per row.

activity create → activities.jsonl (one record)
activity seed   → scope == item ? require explicit --name (no catalog is run)
                : run the catalog command → refuse if empty or nonzero exit
                → one open followup per name with no head followup yet (idempotent)
tick a box      → supersede the followup with status=resolved (target inherited)
reconcile       → re-run the catalog command → classify each open followup
                  live / renamed (via [renames]) / stale / uncheckable
                → --apply re-targets renamed only (evidence-backed, reversible)
                → --resolve-stale additionally closes stale (destructive, opt-in)

session start   → symbion summary (counts + activity progress + priority items + uncommitted)
on demand       → symbion context --branch REF  → git rev-list base..REF → notes on those commits
                → symbion context --target T:N  → heads_for()
```

## Error handling & edge cases

- **A catalog command returning zero names is an error, not an empty catalog.** `git ls-files '*.py'` in a repo with no Python exits 0 with empty output; seeding over it would create 0 followups and report success, which is indistinguishable from having worked. Symbion refuses, exits nonzero, and names the command that produced nothing. A nonzero exit from the command itself is likewise an error, never an empty list.
- **`--apply` never closes a stale item.** The reference's `apply_reconciliation` resolves `stale` followups with a body asserting the target was "retired/renamed". With `[renames]` unconfigured — the shipping default — *every* rename classifies as stale, so `--apply` would silently close real open work and report the activity as further along than it is. `--apply` therefore re-targets `renamed` only, which is evidence-backed; closing stale items requires the separate `--resolve-stale`, and its body says "disappeared from the catalog; no rename evidence" rather than asserting a retirement it cannot know. Conservative *reporting* was already right; conservative *acting* is what was missing.
- **`[renames]` takes the literal `"git"` or a command.** `"git"` selects a built-in provider that runs `git log -z -M --diff-filter=R --name-status --format=` and parses NUL-delimited records in Python. `--format=` is load-bearing — without it commit headers interleave with the status records — and `-M` is stated explicitly rather than relying on `diff.renames` defaulting on. The provider **resolves chains transitively to a fixed point**, so `A→B→C` maps `A` to `C` and not to a `B` that no longer exists, with cycle detection bounding the walk. **Ambiguous histories are dropped, not guessed**: if `A` maps to both `B` and `C` on different lines of development, no entry is emitted, the followup classifies as `stale`, and since `--apply` no longer closes stale items the cost is a line of noise rather than a wrong re-target. The shell one-liner this replaces (`… --name-status | awk '{print $2"\t"$3}'`) breaks on any path containing a space, and shipping it as the documented example would have made path renames — the single most common case — silently unparseable. Arbitrary commands remain supported for non-path catalogs, with the same contract: `old<TAB>new` per line.
- **`rename` exits 1 when it re-targets 0 notes.** Matching nothing means the old name was wrong; the reference prints `re-targeted 0 note(s)` and exits 0, which reads as done.
  *Amended 2026-09-24:* `rename` also re-points every `--ref` to the old name, and prints both counts (`re-targeted 1 note(s), re-pointed 1 ref(s)`). It exits 1 only when both are 0: a name that only refs carry is a real object with nothing filed on it. Measured in an adopting store: a ref stayed on the old name, and `context --target` on the new name dropped it. `arc reconcile --apply` runs the same sweep for each `renamed` item, over the whole store.
- **Malformed line:** `load` skips it and collects it for a surfaced warning; one bad hand-edit cannot take down a read.
- **Ambiguous name:** refused with candidates, never resolved to the nearest.
- **Departed target:** notes attach by name regardless of current catalog membership. `resolve`/`supersede` inherit the stored target, so a departed target stays tickable.
- **Missing commit** (rebased away, shallow clone): `rev-parse` miss falls back to the typed string; `list` resolves a commit target to its subject line via one batched `gitref.subjects` call per listing, degrading to the bare sha instead when the sha doesn't resolve. The batched subject lookup must pass `--ignore-missing`, and that flag is required rather than defensive: without it a single absent sha makes `git log --no-walk` exit 128 with **empty** output (measured), so one stale commit blanks the subject of every valid commit alongside it — the degradation stops being per-commit and becomes total.
- **Concurrent writers: the lock spans the whole transaction, not just the write.** The reference locks `_append_note` but loads *outside* the lock, so every read-modify-write is racy: two concurrent `supersede` calls on the same id both read the same head and append, forking the chain so `heads()` returns both; two `seed_activity` calls compute the same membership set and duplicate every checklist item; two `create_activity` calls read the same registry and the last rewrite silently drops the other — on the one file with no supersede history to reconstruct it from. This is not theoretical here: the harness issues parallel tool calls, so several `symbion` processes in one message is the normal case, not an edge case. Every *public* mutating entry point therefore takes one outer flock spanning load → validate → mint ids → write, and the internal helpers (`_append_note`, `_write_activities`, `_supersede_unlocked`) get unlocked variants. `_supersede_unlocked` carries `supersede`'s fast-forward and cycle detection but takes an already-loaded `notes` list instead of loading it, so a multi-row aggregate (`rename_target`, `apply_reconciliation`) can hold ONE outer lock spanning its whole sweep and call it once per row, rather than nesting a fresh `_lock` per row (which would deadlock — flock is per open-file-description, not per call) or reloading the whole file once per row (O(n²) on a sweep of n, which the reference did). The lock is acquired only at top-level entry points and after `ensure_store`; `_lock` must never nest, since flock is per open-file-description and a second `LOCK_EX` from the same process would deadlock. Every public mutating entry point (`add`, `supersede`, `rename_target`, `commit`, `create_activity`, `rename_activity`, `archive_activity`, `seed_activity`, `apply_reconciliation`) now holds exactly one `_lock` for its whole transaction; none call another locked entry point internally.
- **Seed idempotency:** re-seeding skips targets that already have a head followup.

## File layout

```
pyproject.toml                        # console_scripts: symbion = symbion.cli:main
src/symbion/store.py                  # lifted notebook core: Note, Activity, append/supersede/heads/query, activities
src/symbion/config.py                 # symbion.toml load, defaults, path derivation
src/symbion/catalog.py                # catalog-as-command, rename map, generic resolver
src/symbion/gitref.py                 # commit peeling, provenance stamp, audit states, branch view, uncommitted count
src/symbion/summary.py                # summary + context composition, flattening and caps
src/symbion/cli.py                    # argparse + --json rendering
tests/
docs/superpowers/specs/2026-09-04-symbion-design.md
.claude/skills/symbion/SKILL.md       # the agent-facing surface, modeled on the ancestor's skill
hooks/session_start.sh                # runs `symbion summary`; exits 0 always, stderr suppressed
```

## Testing

pytest. Round-trip, filter-by-every-field, malformed-line tolerance, legacy `atlas`-key acceptance, and lock/atomic-rewrite tests are table stakes. Nine need deliberate aiming, because they otherwise pass on broken code.

- **The SessionStart hook never fails a session.** It is `symbion summary 2>/dev/null || true` — an absent store or a broken config prints nothing and exits 0, and so does an uninstalled `symbion` in a project with no store. A hook that errors on a missing optional tool breaks session init for every project that does not use symbion, which is most of them. **One exception, added 2026-09-04:** when `symbion` is off PATH but this project's store already exists (`$SYMBION_DIR`, else `../<root>-notes/notes.jsonl`, found with bash builtins only since PATH may be empty), the hook prints one line naming the problem and still exits 0. Measured: the console script was installed editable into `.venv` only, so this repo's own read loop was silently dead for a session — and silence there is indistinguishable from a project that never adopted symbion. Both directions are tested: quiet with no store, loud with one. `symbion summary` itself, not just the hook wrapper, must tell an absent store (`store.exists`, i.e. `notes.jsonl` was never created) from an empty-but-initialized one: only the former prints nothing. Tested by pointing the hook at a nonexistent store and asserting exit 0 with **empty stdout and stderr** — asserting only empty stderr is not enough to catch `summary` printing its zero-counts line for a store that was never created, which is the defect this bullet exists to prevent. A second test, against a store that exists but is empty, asserts the zero-counts line **does** print, so the absent-store test can't pass vacuously by never reaching `summary`'s real code path. This mirrors the ancestor's "absent store = silent" discipline. **`--json` is the one exception to "prints nothing":** a programmatic consumer needs valid JSON even against an absent store, so `summary --json` there emits the zero-valued shape (`open_anomalies: 0`, empty `activities`/`priority`, etc. — see `summary.empty_summary()`) instead of an empty string. Only the TEXT path stays silent; `--json`'s contract is "always valid JSON," never "sometimes nothing." **Amended 2026-09-20: the text path is no longer silent on an absent store.** `symbion init` now creates the store and installs the hook in one step (f21508f), so a hook running against an absent store is never a project's ordinary first session: it is a `.symbion` pointer to nowhere, a renamed repo whose store sits beside the old name, or a wrong `SYMBION_DIR` — and printing nothing there read as "no store yet" twice in the field (2026-09-11, 2026-09-20) while the next `add` forked a second store. `symbion summary` prints `symbion: no store at <path>; run \`symbion init\`` and still exits 0, so the hook never fails a session; `--json` is unchanged. The hook's own quiet direction (`symbion` off PATH and no store) still prints nothing.
- **The transaction lock is asserted under real concurrency.** Two processes each `supersede` the same note id; assert exactly one head afterwards, not two. Two processes each `create_activity`; assert both survive the registry rewrite. Both fail against the reference's load-outside-the-lock behaviour, which is what makes them worth writing.
- **A dirty audit never reads `current`.** Stamp an audit with `dirty: true` at HEAD — the case that satisfies `sha == HEAD` — and assert the rendered state is `unverifiable`. Paired with a clean audit at HEAD asserting `current`, so neither direction can rot.
- **Seed idempotency, both directions.** The natural test — seed twice, assert the second created nothing — passes on a `seed_activity` that always creates nothing. The test asserts the first seed creates exactly N for an N-name catalog *and* the second creates 0. The N>0 assertion carries the weight.
- **Reconcile, all four classifications from one fixture.** Asserting a departed target reports `stale` passes on code that always says stale. `live`, `renamed`, `stale`, `uncheckable` are all asserted; `live` is the direction that must not be allowed to rot.
- **Id uniqueness with a scripted id source.** Seeding 200 items and asserting distinct ids cannot fail — microsecond stamps make collision essentially impossible, so the assertion sits nowhere near its threshold and proves nothing about `_mint_unique`. Pinning `_clock` and `_rand` to constants does not work either, in two ways: `_mint_unique` calls `new_id()` with no arguments, so those injectables are unreachable from it at all; and if they *were* reachable, the retry loop would spin forever on the second insertion. So `_mint_unique` takes an injectable `_gen` defaulting to `new_id`, and the test passes a scripted source that returns a duplicate on the first attempt of each of the first 5 items and a fresh id afterwards. Assertions: 200 distinct ids, **and** the source was called 205 times. The call-count assertion is the one that carries the test — without it, the test passes on an implementation with no retry loop at all.
- **Uncommitted count computed outside the write path, and asserted exactly.** If `summary` counts uncommitted rows only after a write in the same process, a store left uncommitted for a week reports zero — the state goes unreported because nothing changed. It is computed unconditionally, and asserted both ways with exact values: append exactly 3 notes, commit nothing, then in a *fresh* process assert the count is `3` (not `> 0`, which would pass on a stuck-at-one implementation); commit and assert it is `0`. A third case covers the untracked store, where the whole file counts.
- **`--apply` leaves stale items open.** Reconcile a fixture with one renamed and one genuinely deleted target, run `--apply`, and assert the renamed item was re-targeted *and* the stale item is still `open`. Then `--resolve-stale` and assert it closes. Without the first assertion, the safe default is untested and a regression to the reference behaviour would pass silently.
- **Staleness distance asserted at exactly 1.** One audit, exactly one subsequent commit, `distance == 1` — not `> 0` after fifty commits, which would pass on any non-zero implementation and catch no off-by-one.

## Migration (deferred — REPEALED 2026-09-08, see `2026-09-08-symbion-vocabulary-design.md` §No backward compatibility; kept as the record of the intent and of the `atlas` synonym's justification)

Symbion is intended to become the shared component that the two research projects import, retiring the byte-identical vendoring and its `diff` drift check — drift becomes impossible rather than merely detectable. That migration is **not v1**, and neither project is expected to change much in the meantime.

The only constraint this places on the design is that on-disk field names stay identical to what those stores already contain, including the legacy `atlas` provenance key, so an existing store loads unchanged whenever migration happens. That costs nothing now; it is just declining to rename things gratuitously.

The ancestor reads its catalog in-process rather than shelling out, so migrating it means either a small wrapper command or a narrow escape hatch added *then*, with a real implementation in hand — not speculated now.

## Open questions

1. **The `[renames]` command for file renames.** The sketched `git log --diff-filter=R` pipeline is untested and may need `-M` tuning or a follow-based alternative. Fine to ship with `[renames]` empty: reconcile then reports `stale` instead of `renamed`, and because `--apply` no longer closes stale items, the cost of that misclassification is a line of noise rather than silently-closed work.
2. **`context --branch` base.** Defaults to `default_branch`; merge-base may be the better default for long-lived branches. Decide after using it.
3. **Recovering audits lost to squash merges.** `git cherry` or patch-id matching could map a squashed topic-branch commit onto its `main` counterpart and promote `unavailable` back to `behind`. That is real machinery for a case the honest `unverifiable` answer already handles safely; build it only if squash-merge workflows make it common.
4. **Recent `decision` heads in `summary`.** Excluded for v1 by argument, not by measurement. Revisit if "go pull from there" turns out not to happen in practice.
