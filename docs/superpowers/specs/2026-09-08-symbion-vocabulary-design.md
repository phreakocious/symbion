# Symbion vocabulary — the nouns a stranger meets

**Date:** 2026-09-08
**Status:** Implemented. A design record: the reasoning still holds, and the details are as of this date. What symbion does now is in the code, the tests, README.md and SKILL.md. Dated notes mark the decisions reversed since.
**Changed since:** the kinds spec (2026-09-10) replaced the fixed seven with labels on three bits, so the summary counts, the hook line and `resolve`'s help are per label, and `idea` is the default `parked` label.
**Amends:** `2026-09-04-symbion-design.md` (the v1 spec) and `2026-09-08-symbion-gui-design.md`.

**Motivation:** Symbion's nouns came over from the research notebook it was extracted from, where an *activity* and an *anomaly* were words of the research itself. Symbion's audience is someone dropping it into an existing git repo. The v1 spec asserted the five kinds "transfer to software without strain"; four adopted stores and two bootstrap runs by fresh agents say otherwise. This spec replaces the vocabulary, adds the two kinds the data shows are missing, and does it with no backward compatibility: the old names are migrated out of the four live stores once and never accepted again.

---

## Evidence

Measured 2026-09-08 over every head row in the four symbion-era stores plus a read of every `note`-kind body.

1. **The docs gloss three names at every use.** `activity` is rendered as "campaign", "epic", "ticket", "checklist" and "board" across README, SKILL and the GUI; `followup` as "ticket" or "ticket item"; `anomaly` as "known broken". `decision` is never glossed. A term that needs a gloss wherever it appears is the wrong term.
2. **`note` names both the row and a kind.** "A note of kind note" is the first thing a newcomer trips on. The catch-all is 20–25% of heads in the three adopted repos (symbion's own store, where the schema is itself the subject matter, is 11.8%), and it is not miscellaneous: the bodies are parked ideas (tagged `idea` in all four stores; the README documents the tag as a convention, and ideas get "PARKED" / "RETIRED" written in prose, which is a status encoded as text) and durable facts about the code (tagged `gotcha`, `footgun`, `process`, `orientation`).
3. **Two stores invented the same missing kind independently.** symbion's own store carries 9 rows tagged `ruling`: a followup that needs the owner's call, closed with `decided:` / `declined:` in the body. Another store's fresh agent, never shown that convention, minted a tag of its own and used it 15 times for the same thing. That is a *question*: open, answered by a human, resolving into a decision.
4. **`audit` is the kind adoption skips.** Two adopted stores hold 0 rows and 1 row, though the README insists the first note is an audit. To a software reader "audit" means security or compliance, not "the test run I did today at this sha" — a false friend rather than an unfamiliar word: it is already in the reader's vocabulary, attached to something else, so it is not learned, it is mistranslated.
5. **`activity` is the expensive rename** — 162 source lines, 252 test lines, 203 doc lines, plus the `activity_id` field, the `activity` target type and the `activities.jsonl` file — which is why it is done now, while there are no external consumers, and not later.

## The vocabulary

| v1 | now | stateful | the moment |
|---|---|---|---|
| `followup` | **`task`** | yes | a concrete next action; the checklist item |
| `anomaly` | **`bug`** | yes | a known-broken thing to return to (code, doc, or spec) |
| `audit` | **`check`** | no | a dated verification: `--checked` what ran, `--result` what it said; `state` is derived from HEAD |
| `decision` | `decision` | no | an ADR: a judgment call not obvious from the diff |
| `note` | `note` | no | a durable fact about the project that fits nothing else: a gotcha, a footgun, how a thing works |
| — | **`idea`** | yes | a parked thought; open means parked, resolved means adopted or retired, and the resolving row is **tagged** `adopted` or `retired` |
| — | **`question`** | yes | needs a human's answer; the resolving body carries it |
| `activity` | **`arc`** | | a named line of work with a beginning, an end, and its own record |
| `global` (target type) | **`project`** | | a note on the project as a whole |

Seven kinds, fixed. `STATEFUL_KINDS = {bug, task, idea, question}`; `status` stays determined by kind and refused on the rest, exactly as v1 §"Note schema" states it. (Amended 2026-09-10: seven default labels on three fixed bits, declared per project — see `2026-09-10-symbion-kinds-design.md`.)

**`note` keeps its name.** With ideas and questions split out, what remains in the catch-all is a durable fact, and no word beats `note` for that. The row/kind collision shrinks to one sentence in SKILL.md: every kind is a note; `note` is the one with no extra structure.

**`arc`, not `epic`, `campaign`, `checklist` or `milestone`.** An arc is what the thing is: a named line of work with a start, an end, and a story in between, whose progress is measured over the tasks inside it and whose decisions and notes are filed under it as its history. `epic` is Jira's word. `checklist` names the rendering and undersells the scope, the record, the archive state and reconcile. `campaign` fits the seeded fan-out and not the hand-picked list, and carries a flavor. `milestone` is GitHub's word and implies a date or a version.

**`bug`, not `issue`.** `issue` is GitHub's row type and would read as "the thing symbion is a substitute for". `bug` says known-broken, and a row on a measurably wrong value in a spec is a doc bug, not a stretch.

**Renames everywhere the old word appears, no exceptions:**

| surface | v1 | now |
|---|---|---|
| CLI subcommand | `symbion activity create/seed/list/todo/rename/archive/reconcile` | `symbion arc …` |
| `add` / `supersede` flag | `--activity-id ID` | `--arc-id ID` |
| `list` filter | `--activity ID` | `--arc ID` |
| target types | `--type activity`, `--type global` | `--type arc`, `--type project` |
| JSON field | `activity_id` | `arc_id` |
| store file | `activities.jsonl` | `arcs.jsonl` |
| `summary` JSON | `open_anomalies`, `open_followups` | `open_bugs`, `open_tasks`, `open_questions` |
| `summary` JSON | `activities`, `activities_elided`, `ACTIVITY_CAP` | `arcs`, `arcs_elided`, `ARC_CAP` (also in `empty_summary()`) |
| GUI routes | `/activities`, `/activity?id=` | `/arcs`, `/arc?id=` |
| GUI test marks | `activity-card`, `new-activity-{name,scope,create}` | `arc-card`, `new-arc-{name,scope,create}` |
| hook line | `N open anomalies, M open standalone followup(s)` | `N open bugs, M open standalone tasks, K open questions` |
| `resolve` help | "mark an anomaly/followup resolved" | "mark a bug/task/idea/question resolved" |

`--checked`, `--result`, `state`, `supersede`, `resolve`, `item`, `commit`, and every catalog-derived type keep their names.

## No backward compatibility

**v1 §Migration is repealed.** That section reserved the on-disk field names for a future in which the two research projects import this package instead of vendoring the notebook, and called that "the only constraint this places on the design". Decided 2026-09-08: they don't, and they won't. The research notebook and its GUI are the **ancestor** this project was extracted from, not a consumer of it — a separate lineage on a vocabulary that is correct where it lives, since there an activity and an anomaly really are the research's own objects. Symbion could be deleted tomorrow and that work would go on unchanged. So the constraint is gone, and with it the last reason for a legacy map: SKILL.md teaching two vocabularies is the defect being fixed.

**The `atlas` → `provenance` read-side synonym goes with it.** `LEGACY_PROVENANCE_KEY` justifies itself in its own comment with "an append-only file is never rewritten" — this spec rewrites four of them — and the rows carrying that key are all in the ancestor's store, now out of scope. Measured 2026-09-08: **0** such rows across the four live stores. Deleted, not migrated.

- `note_from_dict` refuses `followup`, `anomaly` and `audit` the way it refuses any unknown kind today, and `activity` and `global` the way it refuses any unknown target type when handed the type set, as the CLI does. Two things it does **not** catch: `refs[].type` is never validated (`note_from_dict` builds ref targets unchecked), and an unknown key such as a leftover `activity_id` is dropped silently. Both are covered by the migration below rather than by the reader. No alias, no warning path, no `--legacy`.
- **But the reader does not refuse a row; it drops it.** Found 2026-09-08 while planning: `store._read_notes` catches every exception per line and files the row under `load_malformed`, which only the GUI home page ever shows. On the CLI an un-migrated store loads as an EMPTY store — the hook line reads `0 open bugs, 0 open standalone tasks, 0 open questions` and nothing says why. A silent empty result indistinguishable from a real one. So `summary` (and therefore the hook) gains two fields, `unreadable` (count) and `unreadable_first` (`line N: <error>`), and one rendered line, `  N rows unreadable, first: line N: …`, printed only when the count is nonzero. `load_malformed` rows carry the error as a third element to make that possible. This is the one place the retired-kind hint below can reach a screen.
- **The unknown-kind error names the retired vocabulary.** An error message accepts nothing, so it is not a compatibility surface — and it is the one place a hint is load-bearing: through the summary line above, an un-migrated store says `unknown kind: 'followup': the vocabulary changed on 2026-09-08 (…) and this store has not been migrated; see <this spec>`. One sentence, no code path beyond a membership test on three strings.
- The CLI refuses the old names with argparse's ordinary "invalid choice" message. No hint text naming the new word: a hint on an *input* is a compatibility surface, and it would have to be maintained for a vocabulary that no longer exists anywhere.
- **The four live stores are migrated once, by anchored `sed`.** Six substitutions on `notes.jsonl`, one on `activities.jsonl`, one on the toml comment, then `git mv activities.jsonl arcs.jsonl`, one commit per store. No script is shipped and none is kept — there is nothing here worth a tool.

  ```sh
  sed -i '' -e 's/"kind": "followup"/"kind": "task"/g' \
            -e 's/"kind": "anomaly"/"kind": "bug"/g' \
            -e 's/"kind": "audit"/"kind": "check"/g' \
            -e 's/"type": "activity"/"type": "arc"/g' \
            -e 's/"type": "global"/"type": "project"/g' \
            -e 's/"activity_id":/"arc_id":/g' notes.jsonl
  sed -i '' -e 's/"target_scope": "global"/"target_scope": "project"/g' activities.jsonl
  sed -i '' -e 's/symbion activity seed/symbion arc seed/' symbion.toml
  git mv activities.jsonl arcs.jsonl
  ```

  The second line is not optional: `arc_from_dict` refuses an unknown scope when the CLI hands it the legal set, and symbion's own store holds one arc scoped `global` (measured 2026-09-08; the other three stores hold only `item` and `file`). The third rewrites the one comment `init` wrote into each store's toml at creation and never touches again.

  **Anchor on the quoted key–value token, never on the bare word.** `s/followup/task/g` renames prose — 17 bodies in symbion's store alone carry the word. The anchored form cannot, and the reason is structural rather than lucky: inside a JSON string every `"` is written `\"`, so `"kind": "followup"` cannot occur inside a body. Verified 2026-09-08 over **every** row of all four stores, not a sample: the anchored `sed` output is byte-identical to a parse-and-rewrite renaming the same fields, and every row already round-trips through `json.dumps(ensure_ascii=False)` unchanged — which is what makes the `", "` / `": "` separators these patterns depend on safe to assume.

  `"type"` is the same key in `target` and in `refs`, so refs migrate for free. That matters: the reader would never have complained about a `refs` entry left behind. (Measured: 0 `global`/`activity` refs exist today, so this is belt-and-braces, not a repair.)

  A tag that happens to spell an old kind is untouched, which is correct and deliberate — one `audit`-tagged row exists, and a tag is never reinterpreted as a kind (see the `idea` and `ruling` bullets below).

- **Post-condition per store: `symbion summary` loads it, plus one grep.** `grep -c '"activity_id"' notes.jsonl` must print 0 — quoted, so a body mentioning the field cannot match; unanchored on the colon, so a compact-form row cannot hide. Everything else is covered by the load: the reader refuses any un-renamed `kind`, the CLI's type set refuses any un-renamed `target.type`, and a hand-edited compact-form row trips the `kind` check too. **Zero rows left is the check; a count of rows changed is not** — a per-field count computed by the same pass that did the renaming asserts only that the pass was deterministic.
- Existing `note` rows tagged `idea` stay `note`. A tag is never reinterpreted as a kind; `list --tag idea` finds old and new alike, and the SKILL says so.
- Existing `followup` rows tagged `ruling`, or with the other store's equivalent tag, become `task` rows with their tags. The `ruling` convention in SKILL.md's bootstrap step 4 is replaced by `question`; the tag is not migrated to a kind for the same reason `idea` is not.

Symbion's own store is one of the four. The editable-install hazard applies: the migration runs after the code change is installed and after every agent using the store has stopped.

## Behavior that changes

Four changes, each earned by the evidence above. Nothing else moves.

1. **`resolve <id> [--body TEXT | --body-file PATH] [--add-tag TAG]`.** Today `resolve` takes only an author, so closing a ruling with its answer meant `supersede --status resolved --body …`. A question's answer and an idea's fate are the value of closing them; they go on the resolving row in one command. `--body` replaces the inherited body and `--body-file` reads it from a path or stdin, exactly as on `supersede` and `add`; `--add-tag` is what makes the `adopted` / `retired` convention a one-command close instead of a reason to reach for `supersede` anyway. All three are the existing `supersede` plumbing — `resolve` is already `supersede` with a fixed status.
2. **`summary` counts open questions** beside open bugs and standalone open tasks, in the JSON and on the hook line. Questions are the owner's queue, which is what the `ruling` tag and its independent twin were reinventing; surfacing them at session start is what makes the kind earn its place over a tag.
3. **Parked means parked, everywhere.** `idea` is stateful so it can be closed, not so it can nag: it is excluded from `summary`, from `arc todo`, from `arc` progress `done/total`, and from `context`'s default open set (`summary.context`, which today is `read_status(n) == "open" or kind == "decision"`). `list --kind idea` is the shelf. Without this, adding the kind silently widens three "needs attention" views, and one parked idea filed under an arc pins that arc at `0/3` forever on the hook line. (Amended 2026-09-24: an open `idea` whose `--due` date is near or past shows in `summary`'s due block, and a starred one in the priority block: a date or a star asks to be reminded.) An idea you actually need decided before an arc can close is a **`question`** — that is the kind this spec added, and the reason `idea` does not have to double as one.
4. **`arc seed` mints `task` rows**, as `activity seed` minted followups. `arc todo` and `list --arc` keep their meaning with one narrowing: every stateful head carrying the `arc_id` **except `idea`**, filtered to open for `todo`. (Noted 2026-09-25: `list --arc` lists every head in the arc, of any kind; `arc todo` and progress count only unparked status rows.)

## Spec amendments

- v1 "Goal & non-goals": *"The five note kinds are fixed"* becomes *"The seven note kinds are fixed (amended 2026-09-08 — see `2026-09-08-symbion-vocabulary-design.md`)."* The v1 line stands as history.
- v1 "Note schema": the `kind ∈ {…}` bullet and the `activity_id` field name are annotated with a pointer here, not rewritten. The v1 text is the record of what was built first.
- v1 **"Migration"**: repealed in full — see **No backward compatibility** above. Annotated, not deleted: the section is the record of an intent, and it is where the `atlas` synonym's justification lived.
- GUI spec "Non-goals (still)": *"No new note kinds"* is annotated as superseded here. The GUI relabels wholesale — boards, filters, chips, the arc checklist page — and gains nothing else.

## File layout after

```
../<repo>-notes/
  notes.jsonl        # unchanged shape; kind ∈ the seven; target.type ∈ {commit, item, project, arc, <catalog>}; field arc_id
  arcs.jsonl         # was activities.jsonl
  symbion.toml
```

## Testing

Beyond the mechanical rename of every existing test:

- A store row with kind `followup`, `anomaly` or `audit` is refused on load with the existing unknown-kind error, and one with target type `activity` or `global` with the unknown-type error. One test per name; this is the assertion that no alias survived.
- The unknown-kind error **names** the retired kind and the vocabulary change for those three, and does **not** for a genuinely unknown kind like `sausage`. Both directions, or the message is decoration.
- `question` and `idea` default to `open`, accept `--status resolved`, and are refused nothing that `task` accepts.
- `question` appears in `arc todo`, in `arc` progress and in `context`; `idea` appears in none of them. Both directions on one fixture arc holding one of each: progress reads `0/1`, and `arc todo` lists the question only. This is the assertion that change 3 above is real — a suite that only tests the idea's absence would pass on a build where the arc query is broken outright.
- `--status` is still refused on `note`, `decision` and `check`, on both `add` and `supersede`.
- `resolve --body` and `resolve --body-file` set the resolving row's body and `resolve --add-tag` adds to inherited tags; `resolve` with none of them inherits body and tags unchanged.
- `summary` prints the open-question count, and prints `0 open questions` when there are none (the check that is normally true, so a stuck count reads as broken).
- `summary` reports an unreadable row with its line number and error, and reports nothing when every row reads (both directions). A raw row in the retired vocabulary produces a rendered summary containing the retired kind and the phrase `vocabulary changed`.
- **No migration test.** There is no migration code to test — the `sed` is six substitutions run once by hand, and its equivalence to a parse-and-rewrite was verified against every live row on 2026-09-08 (recorded above). A fixture test here would assert that `str.replace` works.
- The SKILL / hook drift test already in the suite catches a stale `symbion init` copy.

## Open questions

1. **`item` as a target-type name.** It is vague, and `--type item --name "flaky upload test"` reads oddly. No better one-word candidate surfaced (`topic`, `thing`, `freeform` are each worse), and it is the no-catalog type on which every plain checklist depends. Left alone; revisit if a fresh agent stumbles on it.
2. **Statuses.** Only `open` and `resolved`. `blocked` (4 rows in one store), `priority` and "declined" all live in tags or in the resolving body. Tags cover them today; a third status is a change to every open view and is not earned yet.
