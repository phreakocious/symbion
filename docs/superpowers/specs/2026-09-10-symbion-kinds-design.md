# Symbion kinds — labels are the project's, shapes are symbion's

**Date:** 2026-09-10
**Status:** Design approved (brainstorm) — ready for the implementation plan.
**Amends:** `2026-09-08-symbion-vocabulary-design.md` ("Seven kinds, fixed" becomes seven *default labels* on three fixed bits); `2026-09-04-symbion-design.md` §"Note schema" (`status` is determined by the kind's `status` bit, not by a hard-coded set); `2026-09-08-symbion-gui-design.md` (the home page's boards are derived from the table).

**Motivation:** Two ideas filed on 2026-09-10 by an adopter whose results are measurements asked for an eighth kind, `prediction`, and noted that `measurement` has no home among the seven. Both are one symptom: the seven labels are symbion's, and a project whose work is not software (the research notebook symbion was extracted from is one) has words of its own that are correct where they live. The 2026-09-08 spec fixed the labels for a software audience and was right to; what it also fixed, without needing to, was the *set*. This spec separates the two. Symbion owns the few behaviours a kind can have. The project owns the labels, and ships with the seven.

---

## Evidence

Measured 2026-09-10 by tracing every site in `src/symbion` where `kind` changes behaviour. A kind drives exactly three things:

1. **Lifecycle.** Whether the row carries `open`/`resolved`. `STATEFUL_KINDS = {bug, task, idea, question}` (`store.py`); `default_status`, `read_status`, `check_status_kind`, the GUI's resolve button.
2. **Visibility of the open state.** Whether an open row counts in arc progress and `arc todo` (`CHECKLIST_KINDS = STATEFUL_KINDS - {idea}`), in `summary` (`open_bugs`, `open_tasks`, `open_questions`, three named fields and a hard-coded hook line), and in the default `context` view (`kind != "idea"`). `idea` is the one exception, because parked means parked.
3. **Verdict.** Whether the row carries `checked`/`result`, gets a provenance stamp at write, and has `state` derived against HEAD. `check` only: `gitref.provenance_stamp` (`kind != "check"`), `cli._print_note`, `list`/`context` state derivation, the GUI form's field visibility and body-optional rule, the "recent checks" board.

Everything else that names a label is presentation or a default: the GUI chip colour map (`_KIND_CHIP`), the "open bugs" and "standalone tasks" home boards, `seed` minting `task`, the default `context` view including every `decision`, and the `resolve` help string. Fourteen code sites in `src/`, and two more in tests.

So seven labels sit on four shapes, and the shapes are combinations of three bits:

| label | `status` | `parked` | `verdict` |
|---|---|---|---|
| `note` | | | |
| `decision` | | | |
| `bug` | ✓ | | |
| `task` | ✓ | | |
| `question` | ✓ | | |
| `idea` | ✓ | ✓ | |
| `check` | | | ✓ |

One legal combination has no built-in: `status` + `verdict`. That is the pre-registration a research project asks for — a check written before the data exists, provenance stamped at commitment, open until the result lands. It falls out of the model; it is not a feature.

A store kept in the ancestor's vocabulary declares the labels it already uses under this spec, adds one, and keeps every word; its migration is the three non-kind substitutions from the vocabulary spec plus one catalog line per target type.

## The model

Three bits. Nothing else about a kind is configurable, and no toml adds a fourth.

- **`status`** — the row carries `open`/`resolved`, defaults to `open` at write, and `resolve` works on it. Open rows count in arc progress, `arc todo`, the session-start line and the default `context` view.
- **`parked`** — open rows are excluded from every open view named above. Requires `status`; `parked` without `status` is refused at config load.
- **`verdict`** — `--checked` and `--result` are accepted, rendered, and offered in the GUI; the body is optional (a check may be nothing but its verdict); provenance is stamped at write; `state` (`current`/`behind`/`diverged`/`unverifiable`) is derived against HEAD.

A kind with no bits is **plain**: a dated body on a target and nothing more.

`--checked` and `--result` are refused on a kind without `verdict`, on `add` and on `supersede`, the way `--status` is refused on a kind without `status` today. Both were silently accepted before; a verdict on a `decision` is a mis-filed row, not a feature. The reader stays tolerant (rows already carrying them load unchanged).

**`status` + `verdict`, the pre-registration.** One transition rule, shared by every write path: **a row of this shape cannot be moved to `resolved` without a non-empty `result`.** It lives in `store._supersede_unlocked`, beside `check_status_kind`, and in `add` for `--status resolved`, so the CLI's `resolve` (which gains `--result`), the GUI's resolve button, the GUI's arc-checklist tick and `arc reconcile` all share it — reconcile reaches the store through `_supersede_unlocked` directly, not through `api.supersede`, so a rule placed in the API would have missed it. The error names the flag. On that transition, `api.supersede` **re-stamps provenance**: the original row keeps the commitment sha (what HEAD was when the prediction was written, before the data), and the resolving row carries the sha the verdict was made at. An ordinary `supersede` on any row still inherits provenance, as today, because a correction is about the same run; reconcile never performs the transition (below), so it never re-stamps. The falsifier goes in the body. No `--falsifier` flag: a required flag enforces presence, not content, and the discipline that makes a pre-registration worth filing lives in the project's rules, not the schema.

## The table

```toml
[kinds]
note     = { when = "a durable fact that fits nothing else: a gotcha, a footgun, how a thing works" }
decision = { when = "an ADR: a judgment call not obvious from the diff" }
bug      = { status = true, when = "a known-broken thing to return to; resolve when fixed" }
task     = { status = true, when = "a concrete next action; with --arc-id, a checklist box in that arc" }
question = { status = true, when = "needs the owner's answer before work can proceed; resolve --body carries it" }
idea     = { status = true, parked = true, when = "a parked thought; resolve --add-tag adopted or retired, --body says why" }
check    = { verdict = true, when = "a dated verification: --checked what ran, --result what it said" }
```

- The seven defaults live **once, in code** (`store.DEFAULT_KINDS`, bits and `when` strings). `init` renders them into a new store's `symbion.toml` so the file reads as the project's vocabulary.
- **An absent `[kinds]` table means the defaults.** Every live store today has no table and needs no change.
- **A present table replaces the defaults entirely.** There is no merge, so a project can remove a kind it does not use. Two cases, and the difference is whether the label has rows:
  - **An unused label** is renamed or dropped by editing its line. `symbion schema` prints a row count per label so "unused" is read, not assumed.
  - **A used label** is renamed by editing its line **and** rewriting its rows, because the reader refuses an undeclared kind: rows left under the old label load as unreadable, `summary` reports `N rows unreadable, first: line N: unknown kind 'bug'`, and every open one is invisible and unresolvable until repaired. The rewrite is one anchored substitution on `notes.jsonl`, run once by hand, per the vocabulary spec's precedent — ids and `supersedes` links are separate fields and are untouched:

    ```sh
    # macOS: sed -i '' -e 's/"kind": "bug"/"kind": "anomaly"/g' notes.jsonl
    # GNU:   sed -i    -e 's/"kind": "bug"/"kind": "anomaly"/g' notes.jsonl
    ```

    No tool is shipped for it. Dropping a used label is the same rewrite onto whichever label the rows now belong to, or a decision to leave them unreadable on purpose, which the summary line will keep saying.
- Validation at config load, refused with the line that is wrong: a label matches `[a-z][a-z0-9_-]*`; every value is a table whose keys are a subset of `{status, parked, verdict, when}`; bits are booleans and `when` is a string; `parked` requires `status`; the table is non-empty.
- `init` on an existing store does not touch its toml (as today). A project that wants to edit the defaults copies the table `symbion schema` prints.

Target types are not in scope: `commit`, `item`, `project` and `arc` keep their names and their meanings, and catalog types are declared under `[catalogs]` exactly as now.

## Behaviour that changes

**`store.py`.** `NOTE_KINDS`, `STATEFUL_KINDS` and `CHECKLIST_KINDS` are deleted. A `Kinds` mapping (label → bits) replaces them and is threaded the way `target_types` already is: `note_from_dict(d, target_types=None, kinds=None)`, `add_many(store, rows, target_types=None, kinds=None)`, `default_status(kind, kinds)`, `read_status(note, kinds)`, `check_status_kind(kind, status, kinds)`, `arc_progress`/`arc_todo`/`arc_items` take `kinds`. `store.kinds(store_dir)` returns the table (the toml's `[kinds]`, else the defaults) and `load(store)` always passes it, so a row in an undeclared kind lands in `load_malformed` exactly as an unknown kind does today; `note_from_dict` with `kinds=None` uses the defaults (implemented 2026-09-10: a row always needs its bits, so "validate nothing" had no caller). `seed_arc` takes the kind to mint and refuses an ineligible one (rule under `cli.py`). `apply_reconciliation` with `resolve_stale` **skips** a stale `status` + `verdict` row instead of closing it — a pre-registration's verdict cannot be "the target disappeared" — and the reconcile report lists it as `stale (needs --result)`; `--apply` re-targeting is a status-preserving supersede and proceeds on any kind. (Implemented 2026-09-10 as `default_status_for(spec)`, `read_status(note)` reading `note.spec`, `check_fields(kind, spec, d)` and `kinds.read_kinds(store_dir)` in a new `kinds.py`: the bits ride on `Note.spec` at read time instead of a table threaded through every call, which is what lets every consumer drop the label.)

**`api.py`.** `_row_to_fields` validates against `ctx.cfg.kinds`. `supersede` re-stamps provenance on the open→resolved transition of a `status` + `verdict` row; the result requirement itself is the store's. `provenance_stamp(cfg, kind)` tests the `verdict` bit.

**`cli.py`.**
- `add --kind` and `list --kind` choices come from the table; an illegal value gets a `_catalog_choice`-style error naming `symbion.toml [kinds]`.
- `resolve` takes `--result`; its help reads "mark an open row resolved". On a `status` + `verdict` row, omitting it is refused by the store rule above, and the error names `--result`.
- `arc seed --kind LABEL`, default `task`. **Eligible kinds have `status`, and neither `parked` nor `verdict`**: a seeded row is a checklist box that is nothing but its name, and `seed_arc` never stamps provenance, so a seeded `verdict` row would be a prediction with no commitment sha and no falsifier. An ineligible label, or a default `task` that is undeclared or ineligible, is refused with the reason and the flag.
- `_print_note` renders `checked=… result=… state=…` on the `verdict` bit.
- `symbion schema` is new (below); the session hook runs it after `summary`.

**`summary.py`.** The three named counts become one field, `open`: an object keyed by label in declared order, one entry per non-parked `status` kind, counting open heads **outside arcs**. This is one simplification over today, where bugs inside arcs were counted and tasks were not: an arc's rows are reached through its own progress line, for every kind. `standalone_tasks` and per-label `open_notes` calls go; `open_notes(notes, kind)` stays as the generic helper. The rendered line is label-first so no label is pluralised (`anomalys`):

```
symbion: open outside arcs: bug 1, task 0, question 0
```

`empty_summary()` carries `open: {}`. The default `context` view (no `--target`, `--commit` or `--branch`) becomes: every open non-parked row, plus every head of a **plain** kind. Today it is open non-idea rows plus every `decision`; `note` heads join it, which is what a durable fact about the project is for.

**`gui/`.** The add form's kind select lists the table; `checked`/`result` visibility binds to the `verdict` bit; the body-required rule tests the same bit. `_KIND_CHIP` is keyed by bits, not label: `verdict` (with or without `status`) takes the check colour, `parked` none, `status` the task colour, plain none. One simplification: `decision` loses its own chip and reads like `note`; a per-kind `color` key is a later decision, not this spec's. The resolve button appears on open rows of any `status` kind; on a `status` + `verdict` row it — and the arc checklist's tick, which is the same transition — opens the edit dialog with `result` required instead of resolving in one click, so a pre-registration cannot be closed without its verdict (the store refuses it anyway; the dialog is so the refusal is never what the user sees). The arc page's "seed all" button seeds `task` and is rendered only when `task` is declared and eligible; a store without one seeds from the CLI with `--kind`. No kind selector in the GUI: no store has that shape yet. The home page renders one "open <label>" board per non-parked `status` kind (rows outside arcs, matching `summary`), the priority board, and one "recent <label>" board per `verdict` kind, 25 rows each. For a default store that is bug, task, question, priority, check — two boards more than today, both of which were reachable only by filter before.

**`gitref.py`.** `provenance_stamp` tests the bit. `check_state` is unchanged.

**The retired-kind hint** (`followup`, `anomaly`, `audit`) fires only when the label is *undeclared*, and its text gains "or declare it under [kinds] in symbion.toml". A store that declares `anomaly` has no retired kinds.

## `symbion schema`

One renderer, three consumers. Text by default, `--json` for agents.

```
kinds  (symbion.toml [kinds]; absent = these defaults)   rows
  note      plain            17  a durable fact that fits nothing else: a gotcha, a footgun, how a thing works
  decision  plain            31  an ADR: a judgment call not obvious from the diff
  bug       status           12  a known-broken thing to return to; resolve when fixed
  task      status           58  a concrete next action; with --arc-id, a checklist box in that arc
  question  status            9  needs the owner's answer before work can proceed; resolve --body carries it
  idea      status parked     6  a parked thought; resolve --add-tag adopted or retired, --body says why
  check     verdict          19  a dated verification: --checked what ran, --result what it said
targets
  commit item project arc   built in
  file                      catalog: git ls-files '*.py'
```

`--json`: `{"kinds": [{"label", "status", "parked", "verdict", "when", "rows"}, …], "targets": [{"type", "catalog": "<command>" | null}, …], "declared": true | false}`. `declared` says whether the table came from the toml or the defaults; the text form says it in the header line. `rows` counts every row ever written under the label, heads and superseded alike, because a rename has to rewrite all of them.

Consumers:

1. **The SessionStart hook** prints `symbion schema` after `symbion summary`, every session. The vocabulary is what an agent must know before its first write, the hook is the one read that is guaranteed, and it costs ten lines.
2. **SKILL.md** embeds `` !`symbion schema` `` where the "Picking a kind" table is today (Claude Code substitutes a skill's inline command output at invocation time; no frontmatter needed). The table is deleted from SKILL.md. What stays is fixed text: one paragraph per bit saying what it does mechanically, the `--body`-is-markdown note, and the write-only-use guard. The `symbion init` SKILL/hook drift test is unaffected: the embedded text is the command, not its output.
3. **A human** runs it to see the words before editing the toml.

## Compatibility

- The four live symbion-era stores: no table, no change. Their `summary --json` consumers are the hook and the tests, both updated here.
- A project whose results are measurements, if it adopts: the table below (the ancestor stamps provenance on every row through an override command; symbion stamps `verdict` kinds only, so a label that wants a sha per row declares `verdict`, which also admits `--checked`/`--result` on it — `measurement` below does, `anomaly` is the project's call), then the vocabulary spec's `sed` minus the three kind lines, then `git mv activities.jsonl arcs.jsonl`, then one catalog per target type. Its supersede links survive because nothing is imported through `--from-json`. Numeric resolution for a measured type is a separate spec (`[resolvers]`), not this one.

```toml
[kinds]
note        = { when = "a durable fact that fits nothing else" }
decision    = { when = "an ADR: a judgment call not obvious from the diff" }
anomaly     = { status = true, when = "an unexpected result; resolve when explained" }
followup    = { status = true, when = "a concrete next action" }
measurement = { verdict = true, when = "a dated result at a sha; the body carries the number and its interval" }
audit       = { verdict = true, when = "a dated verification" }
prediction  = { status = true, verdict = true, when = "a pre-registration: prediction, control and falsifier in the body, written before the data; resolve --result closes it" }
```

## Spec amendments

- Vocabulary spec §"The vocabulary": *"Seven kinds, fixed. `STATEFUL_KINDS = {bug, task, idea, question}`"* becomes *"Seven default labels on three fixed bits (amended 2026-09-10 — see `2026-09-10-symbion-kinds-design.md`); the bits are fixed, the labels are the project's."* The `idea` and `question` rows stand as history.
- v1 spec §"Note schema": *"status is determined by kind"* becomes *"status is determined by the kind's `status` bit (amended 2026-09-10)"*.
- v1 spec "Non-goals": *"The five note kinds are fixed"* gains a second amendment note pointing here.
- GUI spec: the home page's board list is derived from the kinds table (amended 2026-09-10).

## File layout after

```
../<repo>-notes/
  notes.jsonl        # unchanged shape; kind ∈ the declared labels (or the seven defaults)
  arcs.jsonl         # unchanged
  symbion.toml       # + [kinds], written by init on a NEW store; absent on existing stores = defaults
```

No new file. No migration.

## Testing

Both directions, per the project's rules.

- A declared label is accepted by `add`, `add --from-json`, `list --kind` and the reader; an undeclared one is refused by each, and the reader files it under `load_malformed` with the label in the error.
- `parked` without `status` is refused at config load with the offending label; `parked` with `status` loads. An unknown key in a kind entry is refused.
- A toml with no `[kinds]` yields the seven defaults; a toml with a one-line table yields exactly that one kind and none of the defaults.
- `--checked`/`--result` are refused on a plain kind and on a `status`-only kind, and accepted on a `verdict` kind, on both `add` and `supersede`.
- On a `status` + `verdict` row: `resolve --result` produces a resolved row whose provenance sha differs from the original's when HEAD has moved (the fixture commits between the two); a plain `supersede --body` on the same row inherits the original's provenance. And on a `verdict`-only row, `supersede --result` inherits (the correction rule is untouched).
- `summary --json` on a store with `anomaly` declared as a `status` kind reports `open.anomaly`, has no `open_bugs` key, and the rendered line reads `open outside arcs: anomaly N`. A bug inside an arc is not counted; the same bug outside one is (both directions of the outside-arcs rule).
- `summary` still prints a zero count for a declared `status` kind with no open rows (the check that is normally true).
- `context` with no selector includes an open `bug`, a `note` head and a `decision` head, and excludes an open `idea` and a `check` (both directions on one fixture).
- `arc seed` mints the `--kind` given; with no flag on a table lacking `task`, it errors naming `--kind`; with no flag on the defaults, it mints `task`.
- `schema` prints every declared label with its bits and `when`; `schema --json` on a table-less store has `declared: false` and seven kinds; on a declared store, `declared: true` and exactly the declared set. Targets list built-ins plus each catalog with its command.
- The GUI: the kind select lists exactly the declared labels; `checked`/`result` fields are visible for a `verdict` kind and absent for a non-verdict kind; the resolve button on a `status` + `verdict` row opens the dialog with `result` required and does not resolve on click; on a `status`-only row it resolves in one click. The home page shows an "open anomaly" board on a store declaring `anomaly` and no "open bug" board.
- The retired-kind hint appears for an undeclared `followup` and not for a declared one, and not for `sausage`.
- **The transition rule, on every path.** On an open `status` + `verdict` row: CLI `resolve` without `--result` is refused naming the flag, and with it succeeds; `add --status resolved` without `result` is refused; the GUI resolve button and the arc-checklist tick open the dialog and do not resolve on click, and the dialog refuses an empty result; `arc reconcile --resolve-stale` leaves the row open and reports it as `stale (needs --result)` while closing a stale `task` in the same run (both directions in one fixture). On a `status`-only row every one of those paths resolves as today.
- **Seed eligibility, both directions.** `arc seed --kind` mints a `status`-only kind; refuses a plain kind, a `parked` kind and a `status` + `verdict` kind, each error naming the bit; with a table lacking `task`, the GUI arc page renders no seed button and the CLI default errors naming `--kind`.
- **Renaming a used label.** With `bug` renamed to `anomaly` in the toml and the rows not rewritten, `summary` reports the rows unreadable naming `bug`, and `list --status open` omits them; after the `sed`, the same rows list under `anomaly` with their ids and `supersedes` links intact. `schema` reports `rows` per label, and a label with zero rows reports `0`.
- `symbion init` on a new store writes a `[kinds]` table that round-trips to `DEFAULT_KINDS`; on an existing store it leaves the toml byte-identical.
- SKILL.md contains the literal `` !`symbion schema` `` and no longer contains the "Picking a kind" table; the drift test comparing the installed copy with package data still passes.

## Open questions

1. **`note` heads in the default `context` view.** Included by the plain-kind rule. If a store's note count makes `context` noisy, the rule narrows to "plain kinds tagged for context" or reverts to decisions only; decide after using it. **Decided 2026-09-22, after use: the rule stays; the exit was missing.** Measured on symbion's own store: 60 rows in the default view, 24 of them note heads. Read one by one, 11 were durable gotchas (what the rule is for) and 13 were "built at" markers, progress logs, review records and wishes: finished work written as a note, which a plain kind could never leave, since it has no status to resolve and `supersede` cannot change a kind. Decisions-only would have dropped the 11 with the 13; an include-tag needs remembering on every future gotcha. So the tag runs the other way: a plain head tagged `retired` (`supersede --add-tag retired`) leaves the default view and stays on its object and in `list`. Scoped to plain kinds: an open row tagged `retired` is still open work and stays in. The 13 were retired and the two GUI wishes became tasks; the view read 48 rows after, 11 of them notes.
2. **The schema at every session start.** Ten lines, every session, on every store. If it reads as noise on default stores, print it only when `declared` is true and leave the SKILL embed as the always-on surface.

## Out of scope

- `[resolvers]` for per-type name canonicalization (the second of the two ideas above). Next spec.
- Renaming built-in target types, statuses (`open`/`resolved`), or the `arc` registry.
- A fourth bit, a per-kind colour, or per-kind fields beyond `checked`/`result`. `measurements` and `evidence` stay frozen and writer-less (decided 2026-09-04).
