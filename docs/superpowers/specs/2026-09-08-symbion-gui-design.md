# Symbion GUI — a human browse-and-write surface over the same store

**Date:** 2026-09-08
**Status:** Implemented. A design record: the reasoning still holds, and the details are as of this date. What symbion does now is in the code, the tests, README.md and SKILL.md. Dated notes mark the decisions reversed since.
**Changed since:** the vocabulary spec (2026-09-08) renamed activity→arc throughout, routes and api functions included; the kinds spec (2026-09-10) made the home boards one per kind; the resolvers spec (2026-09-10) split `canon_refs` into `check_refs` and `canonicalize_rows`.
**Amends:** `2026-09-04-symbion-design.md` §Goal & non-goals, §CLI surface.

**Motivation:** Symbion's v1 spec lists **a GUI** as its first non-goal, and designs `supersede --add-tag priority` explicitly so that `priority` "gets its mechanism *without a GUI*" (`2026-09-04-symbion-design.md:207`). That was right for v1: the store had to prove it was worth reading before anything was built to read it prettily. It is now being reversed deliberately, for one reason the CLI cannot address — **browsing**. A tag is a link; a target is a link; a supersede chain is a thread. In a terminal each of those is a fresh command you have to compose from memory. On a page it is a click.

The port is cheap because the front-end already exists. The research notebook symbion was extracted from has a NiceGUI front-end over the direct ancestor of `symbion/store.py`, which renamed none of its functions. Its notes/activities surface and their tests port against symbion's store with import swaps and a handful of edits.

---

## Problem

Three things the CLI structurally cannot do well:

1. **Follow a thread.** `list --tag priority`, then read an id off the output, then `context --id`, then notice a ref and compose a third command. Every hop is retyped. The store is a graph and the CLI renders it one flat slice at a time.
2. **Tick a checklist.** `activity todo <id>` prints the work list; acting on it is `resolve <id>` per row, with the id copied by hand. The loop is the single most mechanical thing symbion asks a human to do.
3. **Show what a note actually says.** Bodies are markdown. `measurements` is a table. `provenance` is a git relationship — current, behind N, diverged, unverifiable — that `_print_note` (`cli.py:158`) renders as a raw `prov={...}` dict beside `state=`/`distance=`, correct and unreadable.

None of these is an agent's problem. An agent composes commands cheaply and reads JSON natively. This is the human half of "a human and a coding agent through one CLI", and it is the half that has had no attention.

## Goal & non-goals

**Goal:** An optional, local, single-user web front-end — `symbion serve` — that browses the store by filter, renders every field symbion stores, and performs the writes a human makes *while reading*: add, supersede, resolve, tag, tick a checklist item, seed a scope, create/rename/archive an activity, and commit the store. It shares one write path with the CLI so the two cannot drift.

`init`, `rename` (a catalog migration) and `activity reconcile` (a maintenance sweep) stay CLI-only. Both are rare, both are destructive-adjacent, and neither is something you reach for mid-browse.

**Non-goals (still):** auth, multi-user, sync/remote, full-text search (reversed 2026-09-29, see below), a link-graph view, evidence *serving*, activity deletion, auto-commit. No new note kinds (superseded 2026-09-08 by `2026-09-08-symbion-vocabulary-design.md`: `idea` and `question`; the GUI relabels wholesale and gains nothing else). The GUI adds no capability the CLI lacks — it is a second surface on one model, never a superset.

*2026-09-29:* full-text search is no longer a non-goal. The CLI gained it first (`list --grep`, 3e3af17, 2026-09-24); the GUI's header box reads the same fields through the same `store.query(grep=...)`, from a `q` URL param, so Decision 3 holds. It differs in one way, on purpose: it is literal and needs every word, where `--grep` is a regex, because a person types words and pastes `$HOME`.

**Non-goal explicitly reversed:** "a GUI" moves out of the v1 non-goals list. The v1 line stands as history; the list itself is amended (see **Spec amendments**).

---

## Load-bearing rationale

Four decisions future readers will want the reasoning for.

### 1. `api.py` exists because a second write client is a drift engine

`store.add_many` validates kind, status and target type. It does **not** canonicalize names, canonicalize refs, stamp provenance, or resolve an author. All four live in `cli.py`:

| concern | today | consequence of a GUI skipping it |
|---|---|---|
| `_canon` → `gitref.canon_name` | `cli.py:49` | GUI writes an uncanonical target name; `list --name` cannot find it |
| `_canon_refs` | `cli.py:200` | a ref of an unreadable type, the exact bug `f441519` fixed |
| `gitref.provenance_stamp` | `cli.py:278` | every GUI audit reads as unverifiable to `audit_state` |
| `_author_default` | `cli.py:25` | see §2 |

This is not hypothetical. The ancestor GUI has a module that exists for precisely one reason, stated in its own docstring: a GUI write site forgot `author=` and stored `author="claude"` for human work. One forgotten keyword, permanently on disk in an append-only file. Symbion gives a forgetful caller four ways to do that instead of one.

The fix is a module in `summary.py`'s slot — composing `store` + `gitref` + `catalog` + `config`, imported by `cli` and `gui`, importing neither. `summary.py` is the precedent that this layer already exists in the design; `api.py` is its write-side twin.

**The extraction is not speculative.** `cli.py` is converted to call `api.py` in the same step that creates it. An interface whose only consumer is a not-yet-written GUI is an interface nothing consumes.

### 2. A GUI write is the human's, never the agent's

`_author_default()` returns `"claude"` when `CLAUDECODE` is set. That variable is set in the agent's shell environment — which is the shell you launch `symbion serve` from. Unchanged, every button a human clicks in a session-launched GUI is attributed to claude, permanently, in an append-only file.

The GUI therefore resolves its own author and never consults `CLAUDECODE`:

```
api.gui_author():  SYMBION_AUTHOR  >  git user.name  >  "user"
```

This **extends** the ancestor GUI's `gui_author()`, which is `git user.name > "user"` with no env override; the reasoning transfers exactly: the explorer *is* the human's interface, so the identity is a property of the surface, not of the environment. `serve --author NAME` overrides. The resolved name renders in the top bar on every page, so a misattribution is visible before it is permanent rather than discovered in a `list` months later.

*2026-10-01:* the name sits at the foot of a sidebar on a wide window, and in the top bar while the sidebar hides behind its menu button (900px and narrower). It is still on every page, before every write.

Both author rules live in `api.py` side by side — `author_default()` for the CLI, `gui_author()` for the GUI — so the difference is one file's worth of reading, not an undocumented divergence.

### 3. The URL is already `store.query()`

```python
def query(notes, *, target_type=None, target_name=None, kind=None,
          status=None, activity_id=None, tag=None, structured=None)
```

Seven keyword filters, every one a scalar. That is a query string. So the browse view is not a page with a search feature — it is `store.query(**parsed_url_params)`, and every chip rendered anywhere in the UI is an `<a>` that adds one parameter. Clicking `#priority` on a note goes to `/notes?tag=priority`; clicking it again from a filtered view narrows further. Back button, bookmarks and shareable URLs all fall out for free, and the filter code is a dict comprehension rather than a state machine.

`query()` gains one kwarg, `author=`, symmetric with the six it has. `list --author` falls out of the same line.

The one read that is *not* a filter is `heads_for(store, type, name)` (`store.py:426`), which also surfaces notes that **ref** the object — a reverse edge `query()` cannot express. That earns its own route, `/object`.

### 4. The core stays dependency-free

README:19 is "Python 3.11+ and git. No dependencies." NiceGUI 3.12.1's transitive closure is **50 packages**, four of which are C-extension builds: `aiohttp`, `lxml`, `orjson`, `pydantic-core`. (`lxml>=6.1.0` and `lxml-html-clean>=0.4.4` are core requirements of NiceGUI 3.x, not extras-gated — a 2.x reading of the metadata says otherwise and is out of date.)

So the GUI is `[project.optional-dependencies] gui`, and every nicegui-dependent test module opens with `pytest.importorskip("nicegui")` — the pattern the ancestor GUI's own conftest already uses, so the core suite runs green in a bare checkout. README:19 is amended to say the *core* has no dependencies.

(Noted 2026-09-29: the core is no longer dependency-free. `rich`, pure Python, four
packages with what it pulls in, became a core dependency so that a plain install gets the
terminal view of `list`, `show` and `context`. `rich-argparse`, pure Python and needing
only rich, joined it the same day so that `--help` prints in that view's colours. The
reason above, 50 packages with C builds, still keeps the GUI an extra.)

A separate `symbion-gui` distribution would keep the core cleaner still, at the cost of two repos to version in lockstep and an `api.py` that becomes a published interface. For a project this size that is the worse trade, and the v1 non-goals already rule out a plugin mechanism.

**A framework was not assumed; it was chosen against the alternative.** Decision 3 describes stateless server-rendered pages — URL params to `store.query()`, every chip an `<a>`, no snapshot held, the store re-read per request. That is reachable with `http.server` + `parse_qs` + `html.escape` + f-strings, at zero dependencies, leaving README:19 true unqualified. Measured before deciding:

| | NiceGUI | stdlib |
|---|---|---|
| dependencies | 50, 4 C builds | 0 |
| ported code kept | 430 lines | 264 (38% of it is framework-bound) |
| ported tests kept | 834 lines | ~0 — they are interaction tests |
| new framework layer | 0 | ~300 lines |
| `serve.py` reload re-exec | 118 lines | not needed |

NiceGUI is chosen for the 430 + 834 lines and for interaction that stdlib makes clumsy: a checklist tick is an instant toggle rather than a form post and redirect, and edit is a dialog rather than its own route. The costs are real and are recorded here so this is not re-litigated: a websocket runtime is being paid for to render pages that hold no state, and `serve.py`'s 118-line re-exec is **a workaround for NiceGUI's spawn reloader, not an asset** — the module table below lists it as ported code, which it is, but its existence is a framework cost and it would be deleted, not rewritten, under stdlib.

If NiceGUI later proves the wrong bet, step 1 is renderer-agnostic and survives it. That is why it ships first and alone.

---

## Architecture

### `api.py`

```python
@dataclass(frozen=True)
class Ctx:
    store_dir: Path
    cfg: Config
    target_types: frozenset
    activity_scopes: frozenset
    seed_scopes: frozenset

def resolve(dir_value=None, cwd=None) -> Ctx
def author_default() -> str          # CLI rule, CLAUDECODE-aware
def gui_author() -> str              # human rule, never "claude"
def canon(cfg, target_type, name)
def canon_refs(cfg, target_types, refs)
def fields_from_row(ctx, row, author) -> dict
def seed_names(ctx, scope, names) -> list[str]

def add(ctx, row, *, author) -> Note
def add_many(ctx, rows, *, author) -> list[Note]
def supersede(ctx, note_id, *, author, **fields) -> Note
def retag(ctx, note_id, *, add=(), rm=(), author) -> Note
def seed(ctx, activity_id, target_type, names, *, author) -> list[Note]
def create_activity(ctx, name, description, scope, *, author) -> Activity
def rename_activity(ctx, activity_id, new_name) -> Activity
def archive_activity(ctx, activity_id) -> Activity
def commit(ctx, message) -> bool
def why_unverifiable(prov) -> str
```

`author` is keyword-only with **no default** on every write. A write site that forgets it raises `TypeError` at the call, rather than writing a plausible wrong row. This is the structural version of what the ancestor GUI's write module achieved by convention.

`resolve()` is a lift of `cli.main()`'s bootstrap (`cli.py:730-737`) — `project_root`, `work_root`, `store_dir`, `config.load`, and the three derived type sets — with no change in behavior. `_extract_dir` stays in `cli.py`: it parses argv, which is a CLI concern.

`seed_names` is **adapted, not lifted**: `cli.py:57` takes an argparse `args`; the api form takes `scope` and `names`.

`retag` is **new code, not a lift**, and it is the one place §1's argument was previously unsupported. `--add-tag` / `--rm-tag` are not a function today: they are five inline lines in `_dispatch` (`cli.py:546-551`) that scan a full `store.load()` to find the old row's tags and merge. There is nothing for a GUI star button to call. `api.retag` becomes that function and `cli.py` is converted to it in step 1, by this spec's own rule.

`rename_activity`, `archive_activity` and `commit` are thin pass-throughs that exist so the allowlist in §Testing can be absolute. A wrapper that only forwards looks like ceremony; it is what makes "gui/ never writes through store" a checkable property rather than a habit.

### `gui/` package

| module | lines | origin |
|---|---|---|
| `serve.py` | ~120 | the ancestor GUI's launcher — free-port picking, `--reload` re-exec through a non-`__main__` module |
| `theme.py` | ~140 | the ancestor GUI's theme, minus its chart constants |
| `chrome.py` | ~90 | header, breadcrumbs, author badge, commit button |
| `filters.py` | ~70 | new — URL params ⇄ `query()` kwargs, chip-link construction |
| `notes.py` | ~230 | the ancestor GUI's notes panel |
| `activities.py` | ~190 | the ancestor GUI's activities page and checklist widget |
| `pages.py` | ~120 | the six routes |

≈950 lines: ~500 ported, ~250 adapted, ~200 new.

### Routes

| route | read call |
|---|---|
| `/` | boards: open anomalies, standalone followups, `#priority`, recent audits — via the shared predicates below (Amended 2026-09-10: one board per non-parked status kind and one per verdict kind, from the kinds table.) |
| `/notes?tag=&kind=&status=&type=&name=&activity=&author=&structured=` | `store.query()` over `heads()` |
| `/object?type=&name=` | `heads_for()` — includes notes that **ref** the object |
| `/tags` | `tag_counts()`, each entry a link to `/notes?tag=` |
| `/activities` | `load_activities()` + progress + new-activity form |
| `/activity?id=` | checklist, rename/archive/seed, and notes *about* the campaign |

Every chip is a link: kind, status, author and each tag → `/notes?…`; target and each ref → `/object?…`; activity → `/activity?id=`.

### The boards page and `summary()` must not disagree

The `/` page and `summary.summary()` select the same three sets, and if they diverge the page and the SessionStart hook report different open work — worse than either being wrong alone, because each corroborates the other by existing.

They cannot simply share a return value. `summary(full=True)` gives `open_anomalies` / `open_followups` as **counts**, not rows, and its `priority` rows carry bodies already flattened and truncated to 200 chars for a terminal line — correct there, wrong for a page that renders markdown.

What is shared is the **selection**, extracted into named predicates in `summary.py` that both callers use:

- `open_notes(notes, kind)` — the existing `_open`, made public.
- `standalone_followups(notes)` — open followups with `activity_id is None`.
- `starred(notes)` — `"priority" in tags and read_status(n) != "resolved"`. Deliberately **not** `status == "open"`: `read_status` is `None` for note/decision/audit, so an `== "open"` test would make a starred decision unstarrable, and `SKILL.md` presents `--add-tag priority` as how *any* note gets starred. `summary.py:48-52` records this; a GUI re-deriving the rule would silently get it wrong in exactly that case.

The GUI renders full `Note` objects through those predicates. `summary()` keeps its own counting and truncation.

### Rendering symbion's model

The ancestor GUI predates half of symbion's note schema and renders none of it. The GUI adds:

- **`refs`** — chips linking to `/object`, the forward direction of the reverse edge `heads_for` already walks.
- **`provenance`** — on audits, a badge from `gitref.audit_state(cfg, prov)`. The vocabulary is exactly four states: `current`, `behind N`, `diverged`, `unverifiable`. **`dirty` is not one of them** — `gitref.py:82-88` collapses *no stamp*, *dirty tree*, and *a sha git no longer has* into `("unverifiable", None)`, and only `_why_unverifiable` (`cli.py:148`) tells them apart. That distinction is the decision-relevant one: a dirty stamp means the audit's own claim was made against a tree nobody can reconstruct, which is a different problem from a squashed sha. So `api.py` lifts `why_unverifiable(prov)` onto its surface and the badge carries the reason — otherwise the badge this paragraph is about cannot be drawn. Same argument as §1: a private in `cli.py` that a second client needs.
- **`measurements`** — a flat key/value table.
- **`evidence`** — rendered as text. Store-relative paths are **not** served over HTTP: it buys nothing a file manager doesn't, and turns the store into a static file host for anything a path traversal can reach.
- **commit** — a button running `store.commit(store_dir, msg, cfg)` with a live `gitref.uncommitted(store_dir)` count beside it. A GUI that can write but not commit only grows the number the SessionStart hook nags about.

---

## Error handling & edge cases

- **No store.** `serve` against a project with no store prints the `symbion init` hint and exits nonzero. It does not create one — creation is `init`'s job and a side effect of "show me my notes" is a bad trade.
- **Not a git repo.** `resolve()` raises exactly as `main()` does today; `serve` reports it and exits 1.
- **Malformed rows.** `load_malformed()` already reports skipped lines; the boards page renders the count and first line number, as the ancestor GUI's notes page does.
- **Concurrent writes.** The store's `flock` covers the GUI for free — `supersede` fast-forwards past a chain tip that moved while it waited (`store.py:341`), so a CLI write during a GUI session extends the chain rather than forking it.
- **Stale page.** The GUI holds no snapshot; every request re-reads the store. A page rendered before a CLI write is stale in the browser, not in memory, and any navigation corrects it.
- **Ambiguous name in the add form.** `catalog.resolve` refuses on ambiguity rather than guessing; the form surfaces the candidate list and does not submit.
- **Departed target.** `supersede` inherits `target` and never re-resolves it (`store.py:352`), so a note on a departed catalog entry stays editable in the GUI exactly as on the CLI.
- **Bind address.** localhost only, no auth. Single-user local tool; this is stated, not defended.

## Testing

- **`test_api.py`** — canonicalization on both write paths, ref canonicalization, provenance stamped on audits and only on audits, and both author rules including the one that matters: `gui_author()` returns the git user with `CLAUDECODE=1` set in the environment.
- **The existing suite is the extraction's regression net.** `test_cli.py` is 778 lines against behavior that must not change. It passing unmodified after `cli.py` is converted to `api.py` is the evidence the lift was faithful.
- **`test_gui_write_seam.py`** — a source audit over `symbion/gui/`, as an **allowlist**, never a denylist. `gui/` may reference `store` only for a named read set: `load`, `load_malformed`, `load_activities`, `heads`, `heads_for`, `query`, `tag_counts`, `activity_items`, `activity_progress`, `read_status`, `exists`. Any other `store.<attr>` in `gui/` fails.

  A denylist fails open. `store.py` has **eleven** writers — `ensure_store`, `add`, `add_many`, `supersede`, `rename_target`, `commit`, `create_activity`, `rename_activity`, `archive_activity`, `seed_activity`, `apply_reconciliation` — so any enumeration is a snapshot that silently permits the twelfth. It also permits what nobody thought to enumerate: an earlier draft of this spec named five, and `rename_activity`, `archive_activity` and `commit` were all in the GUI's own feature list, all missing from the list, and all missing from `api.py`'s surface. Three advertised features routed straight past the layer §1 exists to create, and the test would have said "clean".

  Asserted in **both directions** — fires on a planted violation, silent on the clean tree — so it cannot pass by matching nothing. (The ancestor GUI's theme-audit test is the model: the same trick keeps every hex inside `theme.py`.)
- **`test_filters.py`** — URL params ⇄ `query()` kwargs round trip, and an unknown param is ignored rather than raising.
- **Ported GUI tests** — ~834 lines of the ancestor GUI's notes-panel, activities, notes-page and author tests, each opening with `pytest.importorskip("nicegui")`.

## File layout

```
src/symbion/
  api.py                  NEW — the shared write path
  gui/
    __init__.py
    serve.py  theme.py  chrome.py  filters.py  notes.py  activities.py  pages.py
tests/
  test_api.py  test_filters.py  test_gui_write_seam.py  test_gui_*.py
pyproject.toml            [project.optional-dependencies] gui = ["nicegui"]
                          [project.scripts] unchanged; `serve` is a subcommand
```

## Implementation order

Each step names what it **produces** and what **consumes** it, so no step ships an interface nothing calls.

1. **`api.py`**, in four parts, all consumed by `cli.py` in this same step:
   - the **lift** — `resolve()`, `author_default()`, `canon`, `canon_refs`, `fields_from_row`, `seed_names`, `why_unverifiable`. No behavior change; `test_cli.py`'s 778 lines pass unmodified, which is the evidence the lift was faithful.
   - the **new function** — `retag()`, replacing the inline merge at `cli.py:546-551`. `supersede --add-tag` is converted to call it.
   - the **pass-throughs** — `rename_activity`, `archive_activity`, `commit`, so §Testing's allowlist can be absolute.
   - `query(author=)` in `store.py`, and `summary.py`'s three selection predicates made public with `summary()` converted to them.

   Independently shippable and independently reviewable. Symbion becomes embeddable whether or not step 2 ever lands — and since the renderer is a bet (see §4), this is the step that survives losing it.
2. **`[gui]` extra, `serve.py`, `theme.py`, `chrome.py`.** Produces the shell and the author badge; consumed by step 3.
3. **`/`, `/notes`, `/tags`, chip links** (`filters.py`, `notes.py`). The browse surface — the reason for the project.
4. **`/object`** + refs / provenance / measurements / evidence rendering.
5. **`/activities`, `/activity`** — checklist, tick, seed, rename, archive.
6. **Write surfaces** — add form, edit/supersede, resolve, star, commit. Consumes `api.add`, `api.supersede`, `api.retag` and `api.commit`, all first written in step 1. The star button is the consumer that makes `retag` more than a refactor.

## Spec amendments

`2026-09-04-symbion-design.md` needs two edits, or it contradicts its own repo:

- **§Goal & non-goals (line 22)** — remove "a GUI" from the non-goals list; add a pointer to this document. The remaining non-goals stand.
- **§CLI surface (line 207)** — `--add-tag` / `--rm-tag` are still the right mechanism and the reasoning is unchanged; only the clause "without a GUI" is now false. Note what this does **not** claim: the flags are not today a shared implementation the GUI can reuse, because they are not a function at all (`cli.py:546-551`, inline in `_dispatch`). After `api.retag` exists and `cli.py` is converted to it, the flag and the star button are two front-ends over one function — which is the arrangement this amendment asserts, and it is created by step 1, not found there.

## Settled late, recorded for the test it implies

**`serve` is a subcommand, not a separate console script.** One binary, one `--dir` convention. The cost is an `ImportError` path inside `main()` for a checkout without the extra; it must print `install symbion[gui]` rather than a traceback, and that failure mode is worth an explicit test — a bare `ModuleNotFoundError: nicegui` from a tool that advertises `serve` in `--help` reads as a broken install, not a missing option.

## Open questions

- **Does the boards page need pagination?** The ancestor GUI caps recent audits at 25 and nothing else. Deferred until a store exists that makes it hurt.
  *Answered 2026-09-29 for `/notes`:* a 312-row store took 3.7s and a 1 MB page there. `/notes` renders the newest 100, names the cut in its count line, and ends with a show-all link. The boards stay as they were: open rows, and the newest 25 checks.
