# Symbion on Pi: the repo as a Pi package, at parity with Claude Code

**Date:** 2026-11-02
**Status:** Designed with the user on 2026-11-02, in conversation. A reviewer's read
the same day corrected three points against the code and Pi's docs: marked "after
review" below. Not built.
**Relates to:** the plugin design (2026-10-30), whose hook shims this reuses; the
commit of 2026-11-02 that signs writes in Pi's shell `pi` and gives them Pi's session.

## The need

Pi (earendil-works/pi, MIT) is a terminal coding agent. A Hermes tester named it on
2026-11-01. Symbion supports Claude Code, Codex, Hermes and Antigravity. The user will
run symbion under Pi on their own machine "to make sure everything works". The bar is
what Claude Code gets today (the user: "that would be ideal"):

1. The summary at session start, and again after a compaction.
2. A gotcha line the first time a session's tool call names the gotcha's file.
3. Writes from Pi's shell signed `pi`, with Pi's session. Done on 2026-11-02; `pi`
   stays the name (the user, 2026-11-02: "pi is fine for now").
4. `/setup` and `/wrap`.
5. `symbion init --agent pi`.

## Goals, non-goals, anti-goals

**Goals**

- The five items above, seen working in a live Pi session.
- The repo is the Pi package: `pi install git:github.com/phreakocious/symbion` (the
  user's choice, 2026-11-02, over a package inside the installed CLI). As the Claude
  Code plugin does, the package carries no copy of the skill or of the hook scripts:
  it runs the installed CLI's. `/symbion-setup` installs the CLI, as `/symbion:setup`
  does in Claude Code.

**Non-goals** (the builder's calls, made in conversation)

- Publishing to npm or listing in Pi's package gallery. Both are outward-facing and
  the user's, after this ships.
- Windows. Untested in this cut, as Antigravity on Windows is. Where `sh` is missing,
  the extension says nothing.
- Model names in the author. The user parked that idea on 2026-11-02.
- Pi-specific skill text. Pi reads the same `SKILL.md` every host reads.

**Anti-goals.** Any of these makes it a failure even if it works:

- The summary on every prompt. `before_agent_start` fires before every run.
- The extension speaking in a project that never adopted symbion.
- An extension error, a hang or a slow `symbion` that fails or stalls a Pi session or a
  tool call.
- A second copy of the session-start logic, the gotcha matching, or the skill.

## What exists (read 2026-11-02)

**In symbion**

- `plugin/hooks/session_start.sh` is the plugin's shim. It asks `symbion skill
  --path`, caches the answer at `~/.cache/symbion/skill_dir`, and runs the package's
  `session_start.sh`, which runs `symbion summary --hook`. In a project that adopted
  symbion but has no working CLI, it prints one line that sends the user to
  `/symbion:setup`. With `antigravity` as `$1` it reads agy's JSON and sets
  `SYMBION_HOST=antigravity`; otherwise it sets `SYMBION_HOST=claude`.
- `plugin/hooks/gotchas.sh` reads the cached skill dir and runs the package's
  `gotchas.sh`. That script reads Claude Code's PostToolUse JSON on stdin
  (`session_id`, `hook_event_name`, `tool_name`, `tool_input`), matches file names
  against the raw text of `tool_input`, and prints
  `{"hookSpecificOutput":{"hookEventName":…,"additionalContext":…}}` or nothing.
  For the tool names `Edit|Write|MultiEdit|NotebookEdit` it matches only the value
  after the first key ending in `_path"` (Claude Code's `file_path`), and those
  names also get the re-print on a file's first edit. (Amended 2026-11-09: a row
  the reader already got in full re-prints as one line of ids, since the answer
  lands after the edit; the user's call.)
- `symbion summary --hook` reads `session_id` from its stdin JSON (it must match
  `[A-Za-z0-9_-]{1,128}`), writes that session's gotcha list, resets its seen marks,
  and prints Claude Code's JSON: `additionalContext` for the agent and a one-line
  `systemMessage` for the person. Codex gets no `systemMessage`; agy gets
  `injectSteps`.
- `api.shell_author` returns `pi` under `AI_AGENT=pi`, and `store.session_key` then
  reads `PI_SESSION_ID`.
- `plugin/skills/symbion/SKILL.md` is a stub whose body is one `` !`symbion skill` ``
  line, which Claude Code expands as it loads the skill. The shipped skill
  directory, which `symbion skill --path` prints, holds the full `SKILL.md`; its one
  `!` line (`symbion schema`) reads as literal text under Codex. The summary's
  "before a write: load the symbion skill" hint has no Pi branch.
  `plugin/skills/setup/SKILL.md` is 4,088 bytes against a test's 4,096-byte cap. It
  runs a bare `symbion init` and names `CLAUDE.md`, and `AGENTS.md` for Antigravity.
- `init --agent antigravity` links and registers nothing. It checks that the plugin's
  manifest exists and, if not, prints the install line. `init --agent codex` links
  `~/.agents/skills/symbion`.

**In Pi's docs** (`packages/coding-agent/docs/` on main, 2026-11-02; no Pi install
was run)

- Extensions are TypeScript loaded through jiti, with no build step.
- Events: `session_start` (`reason`: `startup`, `reload`, `new`, `resume`, `fork`),
  `resources_discover`, `session_compact`, `before_agent_start` and `tool_result`.
  - `before_agent_start` fires before every run, not before every turn.
  - Threshold compaction, the usual automatic kind, runs between turns inside a run;
    `/compact` runs between runs. Both fire `session_compact`.
  - `resources_discover` fires after `session_start` and on a reload, and can return
    extra skill directories.
  - `tool_result` fires after the tool, carries `toolName`, `input`, `content` and
    `isError`, and can patch `content`. Replacing `content` without returning
    `structuredContent` drops the structured content.
- `pi.sendMessage(message, {deliverAs: "steer"})` stores a custom message that the
  model receives. While a run streams, it arrives before the next model request;
  in an idle session it is stored and goes with the next prompt. `display` sets
  whether the TUI shows it.
- The read tool's input is `{path, offset, limit}`, and the edit tool's is `{path,
  edits}`. Pi expands no `` !`cmd` `` line in a skill.
- The extension context gives `ctx.ui.notify`, `ctx.sessionManager.getSessionId()`,
  `pi.registerCommand` and `pi.sendUserMessage`.
- Built-in tools: `read`, `bash`, `powershell`, `edit`, `write`, `grep`, `find`, `ls`.
- The bash tool exports `PI_SESSION_ID` and `PI_MODEL`. Pi sets `AI_AGENT=pi` for
  itself and its children. A session id is a UUID unless `--session-id` sets one.
- A package installs from git, npm or a local path. A local path loads in place.
  `package.json` names the package's extensions, skills and prompts under a `pi` key.
- Skills load from `~/.pi/agent/skills`, `~/.agents/skills` and a project's
  `.agents/skills`. On a name collision Pi keeps the first one it found and warns.
  Each skill is also a `/skill:NAME` command.
- A prompt template's file name is its command name, with no namespace.
- Ollama works through a `models.json` provider with `baseUrl`
  `http://localhost:11434/v1`.

## The design

### The package

A `package.json` at the repo root, `"private": true` so npm never publishes it by
accident (my call). Its `pi` key names one extension, `plugin/pi/symbion.ts` (path
my call). It names no skill and no prompts: the extension supplies both.

### The skill

On `resources_discover`, the extension runs `symbion skill --path` and returns that
directory, the shipped skill, as a skill path. The model then gets the full
`SKILL.md`, as under Codex. The stub is not used under Pi: Pi would show its `!` line
as text (my call, after review). With no working CLI it returns nothing, and
`/symbion-setup` is the way in; after setup installs the CLI, `/reload` brings the
skill. The summary's skill hint names `/skill:symbion` under Pi.

*2026-11-03:* the hint names a `SKILL.md` path instead, and the wrap preface tells
the model to read that file. `/skill:symbion` is a command only the user can type.
Pi's system prompt tells the model to load a skill by reading the file its skills
list names. In a live run, the model ran `symbion:symbion` as a shell command.
Where `~/.agents/skills/symbion` also exists, Pi listed that link, not the
package's skill. Both lead to the same installed directory.

### Session start

- On `session_start` with any reason but `reload`, and on `session_compact`, the
  extension runs `sh <package>/plugin/hooks/session_start.sh pi`. It runs in
  `ctx.cwd`, with `SYMBION_PROJECT_DIR` set to `ctx.cwd`, `{"session_id":
  "<getSessionId()>"}` on stdin, and a 10-second timeout. A `reload` keeps what the
  session has.
- The result goes out through `pi.sendMessage` with `deliverAs: "steer"` and no
  `triggerTurn`. That one path covers each case: at session start and after
  `/compact` the session is idle, and the message goes with the next prompt; after a
  threshold compaction inside a run, it arrives before the next model request, as
  Claude Code's SessionStart hook with source `compact` does (my call, after
  review: the user's bar includes the summary again after a compaction).
- Output that parses as the hook's JSON: `additionalContext` becomes the message,
  with `display: false`, and `systemMessage` goes to `ctx.ui.notify`. Any other
  output, such as an error or the not-installed line, becomes the message as it is.
  No output: no message. A failed or timed-out run sends nothing and is not retried
  until the next session start or compaction.
- The shim gets a `pi` branch: `SYMBION_HOST=pi`, and its not-installed line names
  `/symbion-setup`.

### Gotchas

- On `tool_result` for `read`, `edit`, `write` and `bash`, the extension runs `sh
  <package>/plugin/hooks/gotchas.sh` with a 5-second timeout. These are the tools
  Claude Code's matcher covers; Claude Code's matcher leaves out its Grep and Glob,
  and this design leaves out Pi's `grep`, `find` and `ls`.
- Its stdin is Claude Code's shape: `session_id`, `hook_event_name`
  (`PostToolUseFailure` when `isError`, else `PostToolUse`), `tool_name` mapped to
  `Read`, `Edit`, `Write` or `Bash`, and `tool_input` set to Pi's `input`. For
  `Edit` and `Write`, Pi's `path` key is renamed `file_path`: without it the edit
  match reads the literal word `path` and never fires. Read and bash need no
  rename, since their match reads the raw text of `tool_input`.
- `additionalContext` in the output is appended to the result's `content` as one
  text block. `structuredContent` is returned unchanged.

### Commands

`pi.registerCommand` adds `/symbion-setup` and `/symbion-wrap`. Each reads its
`plugin/skills/{setup,wrap}/SKILL.md`, strips the frontmatter, adds a short Pi
preface in front and the command's arguments after, and sends the result with
`pi.sendUserMessage`. The setup preface says: in Pi the command is `/symbion-setup`,
`init` takes `--agent pi`, the instructions file is `AGENTS.md`, and the agent runs
each `!` line itself. This leaves the 4,096-byte setup file as it is (my call).

### `init --agent pi`

- `pi` joins `--agent`'s choices, and the help text names Pi.
- It links and registers nothing: the package carries the skill and the hooks.
- It checks whether `~/.pi/agent/settings.json` lists a package whose source names
  symbion. If none does, it prints `pi install git:github.com/phreakocious/symbion`.
- `--merge-hook` merges nothing for Pi, and says so.

### Docs

The README's install section, the guide (install under Pi, and a test run with a
local model), the reference, `SKILL.md`'s sentence on which hosts get the gotcha
hook, and CLAUDE.md's "Where things are" (`package.json` and `plugin/pi/`).

## Verification

Before the live run, the user approves three installs: Pi from npm, a small Ollama
model that can call tools (my pick: `qwen3:8b`, about 5 GB), and that model's entry
in Pi's `models.json`. If the local model is too slow or too weak to drive the
checks, they run on Gemini (the user, 2026-11-02), with a credential the user sets
up in Pi (`GEMINI_API_KEY` or `/login`).

Live checks run in a scratch project with a scratch store. Each one is recorded as a
`check`:

1. The summary appears once in Pi's session file after the first prompt. It does not
   appear again after a second prompt in the same session. It appears again after
   `/compact`, and after a threshold compaction inside a run, forced with a small
   `contextWindow` in the model's `models.json` entry.
2. A gotcha on a file reaches the result of the first `read` that names it, once. The
   file's first `edit` prints it again.
3. A `symbion add` from Pi's bash signs `pi`, and its session equals the extension's
   `getSessionId()`. The docs do not say the two match: this check settles it.
4. In a project with no store, nothing is added and nothing is notified.
5. In an adopted project with `symbion` off PATH, the message names `/symbion-setup`.
6. `/symbion-setup` and `/symbion-wrap` send their text, and setup runs `init --agent
   pi`.
7. `init --agent pi` with the package installed, and without it.
8. `/skill:symbion` loads the full `SKILL.md`, and the summary's hint names it.

The live checks test symbion, not the model: a check may give the model the exact
command to run.

The shim's `pi` branch and `init --agent pi` get Python tests. Each test is seen to
fail with its piece broken in a copy. The repo has no TypeScript test harness, so the
live checks run from a repeatable script kept with the maintainer's tools, not in the
repo (my call).

## Left to the builder

- Read the installed Pi's own docs first. This design read the docs on main, and an
  npm release may predate an event or option it relies on.
- How the extension finds its package root under jiti.
- How `init --agent pi` recognizes a local-path install during development.
- Pi's collision warning where `~/.agents/skills/symbion` also exists (anyone who ran
  `init --agent codex`). Measure it. If it warns at every start, `init --agent pi`
  and the setup preface say how to quiet it.

## Development and release

The public repo gets the package at the next release cut. Until then the user installs
from a local path, `pi install <checkout>`, which loads in place. A broken save there
breaks every Pi session, as a broken save in the installed CLI breaks every Claude
Code session.
