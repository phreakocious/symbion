# Symbion wrap: the session end, and a handoff kept in the store

**Date:** 2026-10-10
**Status:** Approved design, not implemented.
**Amends:** SKILL.md's "Session end, and a handoff file" section, which this design replaces with a procedure of its own.
**Relates to:** the continuity plugin (`/wrap`, `/next`, https://github.com/nullphase-net/enfurbish). This design changes nothing there; it states what symbion expects of a session-continuity tool on the same machine, and that tool decides how.

**Motivation:** A project with symbion and no continuity has no session end. SKILL.md says what to do "at the end of a session, or whenever a wrap runs", but on such a machine nothing runs a wrap, so nothing calls the moment. Loose ends that a wrap would tie off stay loose. They include details nobody wrote down. The handoff file has the opposite problem: it lives in the code repo, which does not want it, so it needs an ignore rule there, yet it must be easy to find. The store is beside the repo, in its own git, and already holds the project's notes. This design gives symbion its own wrap, moves the handoff into the store, and makes the wrap's store commit the project's journal. Continuity keeps the tooling loop (the retro and the tooling journal) and stays independent of symbion.

---

## Evidence

Measured 2026-10-10.

- **The wrap is when sessions write down what they missed.** On the maintainer's machine, Claude Code transcripts for every project (the oldest surviving one dates from 2026-09-07):
  - Fewer than half the sessions that wrote to a store ran a wrap.
  - In a little under half the wraps, the session wrote rows during the wrap.
  - The wraps held about a tenth of the write calls in wrapped sessions: over half of them `add`, a third `supersede`, under a tenth `resolve`.
  - So the moment ties loose ends mostly by writing down what no row held and by amending rows. It resolves few.
- **continuity's wrap writes no rows.** Its policy: it "writes the retro, the journal entry and `NEXT_SESSION.md` … and changes nothing else". The writes above come from SKILL.md, which tells the agent to act whenever a wrap runs. symbion answers the moment by its generic name, and continuity does not know symbion. That is already the uncoupled shape. It fails only where nothing runs a wrap.
- **A retro sends a project fact nowhere.** In one retro of this project, the agent named a trap that belongs on a test file's gotcha row, then added: "Not written: the wrap writes no rows." continuity's rule for a fact with no home is: "The wrap writes no doc, so carry it … the user moves it."
- **This project's handoff at its last wrap,** sorted by paragraph:

  | content | share |
  |---|---|
  | state of the moment: release position, unpushed counts, running servers, a test host's contents | 25% |
  | durable facts about the repo and its setup | 29% |
  | how to work here | 22% |
  | open rows told again | 15% |
  | orientation: the header and a command block | 8% |

  Most of the state is git or `lsof` output copied into prose. A handoff is the only free-form text that every session reads and that a wrap may write. Each proper home lacks one of the two: the instruction files are always read, but a wrap must not write them; a row on a file can be written, but nothing makes a session read it before an edit. So the text lands in the handoff.
- **The store already versions the handoff, by hand.** Twice, before a trim, the full file was copied into the store's `archive/` and committed.
- **A full retro is too big for `notes.jsonl`; its narrative is not.** This project's retros together come to about a sixth of its `notes.jsonl`, where a row's median body is under 1 KB and the 90th percentile about 2 KB. Three recent retros by section: learnings 1.8–2.5 KB, tooling assessment 1.8–2.4 KB, "What happened" 0.6–1.1 KB, follow-ups and handoff 0.8–1.2 KB together.

## Who owns what

Split by subject, not by tool.

| subject | owner |
|---|---|
| the tooling: how the agent's tools performed, across projects (the retro, the tooling journal) | continuity |
| the project: open work, facts, traps, the handoff, the journal | symbion |
| how to work in this project | the instruction files (`CLAUDE.md` and the like), moved there by the person |

Neither tool names the other in its code. symbion's skill acts on "a wrap" by name. continuity follows what a handoff file says about itself.

## `/symbion wrap`

A procedure in `src/symbion/data/skill/wrap.md`, beside `adoption.md` and `catalogs.md`. SKILL.md sends the agent there when the person asks for a wrap (`/symbion wrap` in Claude Code), and when any tool's wrap runs. It is skill text only. The one code change it relies on is the summary's handoff line (below).

0. **Find the handoff.** A `NEXT_SESSION.md` in the store is this project's handoff: keep it in step 2. If the store has none and the repo root has one, the project keeps its handoff there, by another tool or until someone moves it: skip step 2 and say so in the report. If neither exists, step 2 creates the store's file when there is anything to say.
1. **The end-of-day question.** What did this session learn, decide, find broken or start, that no row holds yet? Search first (`list --grep`). Then:
   - `add` each one on its object: a trap on the file it concerns (`--tag gotcha`), a decision, a bug, a task (with `--due` when a job is still running).
   - `supersede` each row the session changed the truth of.
   - `resolve` each row the session finished, with `--body` saying how.
2. **Rewrite the handoff,** prose only. The store's git keeps every version, so cut hard. The routing table below says what stays and where the rest goes.
3. **Name the learnings for the person.** A learning that should change how future sessions work goes to the person with its cheapest home, the first of these that closes the loop: the environment (an install, an alias, a config), a hook (a shape a program can detect), a scoped rule, an always-on instruction. The wrap does not edit instruction files. Most sessions have none.
4. **Commit the store, with the day as its message.** First `symbion commit --dry-run`, in a call of its own, to see what will go in. Then:

   ```bash
   symbion commit -m "$(cat <<'EOF'
   <one line: the day's gist>

   <one paragraph: what the session did and why, what it left open, the rows that matter by their 10-character tails>
   EOF
   )"
   ```

   The quoted heredoc keeps backticks and `$` literal. symbion appends the `Rows:` trailer. Until `commit` can take only one session's rows, it also takes another live session's pending rows; the paragraph describes this session only, and the trailer names every author. The wrap does not push unless the project's instructions say to.
5. **Report.** Rows added, superseded and resolved; the handoff (written, unchanged, or kept at the repo root); the commit; the learnings for the person.

## The handoff in the store

`NEXT_SESSION.md` at the store root, tracked in the store's git. The first wrap creates it; `init` does not. Its shape:

```markdown
# Next session: <project>

Kept by `symbion wrap` in this project's store. The open list is not in this file:
`symbion summary`, `symbion list --status open`.

## Start here
One or two sentences: what to pick up first, and anything in flight that no row can hold.

## Read first
Commands and paths, not values.

## To move into the instructions
Each practice the person has not yet moved, with the file it belongs in.
```

The second paragraph says who keeps the file. A session-continuity tool reads it as "another tool keeps this file" and leaves the file alone. That is the rule continuity already follows for a file that says its open list lives elsewhere.

What stays, and where the rest goes:

| text | home |
|---|---|
| an open item | a status row |
| a fact or a trap about one file or object | a row on that object (`--tag gotcha` for a trap) |
| a number git or symbion prints (unpushed, open, behind) | none; Read first names the command that prints it |
| state nothing prints (what a test host holds, which release is public) | a plain row on its item, superseded when it changes; Read first names its `context --target` |
| how to work here | the instruction files; the handoff's last section until the person moves it |
| what happened today | the store commit's message |

The last section makes the pile visible. The mechanism that grows a handoff is unchanged by this design: the file is still the only free-form text a session must read and a wrap may write. What changes is that each version is kept, so a cut loses nothing, and each wrap's diff shows what it added.

## Session start

`symbion summary` names the handoff when the store has one:

```
  handoff: ../symbion-notes/NEXT_SESSION.md (written 2h ago, 3 commits since): read it before you start
```

- **The path** is relative to the current directory.
- **"Written"** is the file's last commit in the store's git. If the file has uncommitted changes, or was never committed, it is the file's mtime instead. A fresh clone resets mtimes, so a committed file's mtime would lie.
- **"Commits since"** counts the commits on the repo's HEAD with a commit date after that time. A high count beside an old date is a session that ended without a wrap.
- **JSON:** `summary --json` gains `"handoff": {"path", "written", "commits_since", "committed"}`, or `null` when the store has no handoff. `empty_summary()` gives `null`.
- **Placement:** the line prints above the `symbion serve:` line. The person's one line (`summary --hook`) does not change.

Both hooks print the summary's text, so `session_start.sh` and `session_start.ps1` need no change. SKILL.md's session-start paragraph adds: if the summary names a handoff, read it before starting work.

## The journal

The journal is the store's git log. Each wrap leaves one commit holding that day's rows, the handoff's diff, and a paragraph about the day. Nothing is added to `notes.jsonl`. Read it with `git -C <store> log`; `git -C <store> log -p -- NEXT_SESSION.md` shows how the handoff changed from day to day.

On a machine with both tools, a session writes two narratives of its day: continuity's retro ("What happened") and the store commit's message. That is accepted (the owner, 2026-10-10). They serve different loops, the tooling and the project, and a past-tense record does not go stale.

## With continuity on the same machine

`/wrap` stays one command. continuity writes its retro and its journal entry. symbion's steps run because SKILL.md acts on any wrap. symbion expects three things of continuity. They are continuity's to design, and none of them names symbion:

- **C1.** A per-project setting for where the handoff lives, kept outside the repo. continuity's hook, `/next` and `/wrap` read the handoff there.
- **C2.** `/wrap`'s handoff step leaves alone a file that says another tool keeps it, the same way it already defers the open list to another tool.
- **C3.** The retro and the tooling journal are unchanged.

A project that keeps its handoff at the repo root, under continuity, keeps it there: step 0 skips the handoff. To move it, copy the file into the store, add the keeper paragraph, set C1's location, delete the repo-root file, and drop its ignore rule. `docs/guide.md` gives these steps.

## Changes in this repo

- `src/symbion/summary.py`: the `handoff` field, and its text line.
- `src/symbion/data/skill/wrap.md`: new, the procedure above.
- `src/symbion/data/skill/SKILL.md`: the session-start paragraph reads the handoff. "Session end, and a handoff file" shrinks to a pointer at `wrap.md`, plus the rule it keeps: never write the open list into a handoff.
- `docs/guide.md`: "Commit the store at session end" becomes the wrap, plus the move recipe above. `docs/reference.md`: the summary's handoff line.
- `tests/test_cli.py`: `_DOCS` gains `wrap.md`, so its command spans go through the parser.

## Testing

- **`tests/test_summary.py`:** no handoff gives no line and `null`. A committed handoff reads its commit time; a dirty one reads its mtime; `commits_since` counts repo commits after it. The text line prints in its place. Break each (return `None`, read mtime only, count from the wrong end) and watch its test fail.
- **`tests/test_hook.py`:** the hook's output carries the line when the store has a handoff.
- **The procedure:** a fresh general-purpose agent (not a fork), given a user-style prompt, on a scratch repo and store. Each arm runs once:
  1. A session that found something and wrote no row: does the wrap add the row on its object?
  2. Is the handoff written with the keeper paragraph, and without the open list or a count?
  3. Does the commit carry a narrative?
  4. With a handoff at the repo root and none in the store: is the root file left alone, and the skip reported?
- **After rollout:** re-run the transcript count on a project where symbion runs without continuity. Do wraps there write rows, as they do here?

## Rollout

1. **symbion:** the summary line, `wrap.md`, SKILL.md and the docs. This step ships alone: a project with symbion only gets the whole design here.
2. **continuity:** C1 and C2, in its own repo, by its own design.
3. **This project:** move `NEXT_SESSION.md` into the store and set C1. Not before step 2: until then, continuity's hook finds no handoff here, and its `/wrap` would start a new one at the repo root.

## Out of scope

- **A trigger without the person.** The wrap still waits for someone to ask, and half the sessions above never asked. A backstop at the next session start (rows a finished session left uncommitted) waits for a per-session key on rows.
- **A journal view** in the GUI or the CLI. The public demo is the first place that would want one: add it there, reading the store's git log.
- **One handoff per worktree.** The store holds one; continuity keeps one per directory. Add per-worktree files when a project needs them.
- **The tooling journal and the retro,** which stay continuity's.

## Known weak points

- The handoff still grows for the same reason as before. This design makes the growth visible and cheap to cut. It does not make the file smaller by force.
- The procedure is skill text. Only the fresh-agent run and later transcripts can show whether agents follow it.
- `commit` takes every writer's pending rows, so a wrap's commit can hold another session's rows under a message that does not describe them.
