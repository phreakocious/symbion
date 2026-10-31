# Symbion as a Claude Code plugin: two commands to a project's own notebook

**Date:** 2026-10-30
**Status:** Designed with the user on 2026-10-30, in conversation. A reviewer's read
the same day settled two points in the text and raised three the user answered:
marked "the user, after review" below. Not built.
**Relates to:** the public-demo design (2026-10-22), whose demo the setup sends an
empty project to; `adoption.md`, which the setup runs as it stands.

## The need

Today a Claude Code user installs symbion in five steps: `pipx install
'symbion[gui]'`, `symbion init --yes`, `init --merge-hook` when `settings.json`
already exists, a line in `CLAUDE.md`, and `symbion.toml` edits for file targets.
The tools that reached many Claude Code users installed with one or two `/plugin`
lines. A stranger who reads about symbion should get from "this looks useful" to
browsing their own project's open work with two commands and two approvals.

The user's picture of the first minutes (2026-10-30):

1. The user runs `/plugin install`.
2. The agent installs the CLI, learns `symbion init` and the adoption flow, and gets
   approval to proceed.
3. Adoption: the agent scouts the project, works out what to fold in and what to
   retire, and that plan gets approval.
4. Once done, the agent starts `symbion serve` and sends the user there to look
   through it.

The agent must "immediately have what it needs with no manual steps, and offer to run
the init and adoption flows, explaining exactly what's going to happen before a byte
is written to disk" (the user). The user should not have to restart the session.
Session start "is a nice feature, but it's hardly what makes this thing cool" (the
user): the point of the first minutes is their own project's history, folded in and
on screen, not an empty store.

## Goals, non-goals, anti-goals

**Goals**

- Two commands: `/plugin install …`, then `/symbion:setup`. Two is acceptable only
  because the install itself starts nothing in the agent (the user: "two steps is
  fine if the plugin install doesn't trigger anything"). If the spike below shows
  the install can start the agent, setup should start from it, and the second
  command goes. If it shows a third command (adding the marketplace first) or a
  restart, the build goes ahead and the README and setup say the extra step (the
  user, after review).
- No restart between the two.
- Before each write, the agent says what will change: which files, where, and why.
  Nothing is written until the user approves.
- The plugin is the only way to install on Claude Code. `init` keeps making the
  store, and for Claude Code it never links `~/.claude/skills/symbion` or registers
  hooks in `settings.json` (the user, after review; current users "are very few").

**Non-goals** (the builder's calls, made in conversation; the user did not object)

- Plugins for Codex or Hermes. `init --agent codex` and `--agent hermes` are
  unchanged.
- Submitting to Anthropic's plugin directory. That is outward-facing and the user's,
  after this ships.
- Moving a plugin eval suite into the repo.

**Anti-goals.** Any of these makes it a failure even if it works:

- A byte written before approval: the CLI install, the store, `settings.json`,
  `CLAUDE.md`, or any project file adoption retires.
- The hook speaking in a project that never adopted symbion.
- A skill that describes flags its installed CLI does not have.

## What exists (measured 2026-10-30)

- A scratch plugin, assembled outside the repo, worked with no code change: the
  shipped skill directory as `skills/symbion`, and `hooks/hooks.json` running
  `session_start.sh` (SessionStart) and `gotchas.sh` (PostToolUse and
  PostToolUseFailure, matcher `Read|Edit|Write|NotebookEdit|Bash`) through
  `${CLAUDE_PLUGIN_ROOT}`. `claude plugin validate --profile directory --strict`
  passed once it had a README and `.claude-plugin/icon.png`. `claude plugin
  details` put it at about 145 tokens always on and about 3.7k when the skill loads.
  In a headless session the plugin's session-start text reached the agent; with the
  `settings.json` hook also registered, it arrived twice.
- The validator printed the install line `/plugin install symbion --marketplace
  phreakocious/symbion`. Untested: whether that one line works, or whether
  `/plugin marketplace add phreakocious/symbion` must come first.
- The repo has no `.claude-plugin/` directory and no icon. The scratch kit's
  manifests and icon are the starting point.
- `session_start.sh` exits silently where no store, no `.symbion` pointer and no
  `SYMBION_DIR` exist. With a store and no `symbion` on PATH, it names the absence.
- `gotchas.sh` reads a per-session list under `~/.cache/symbion/gotchas/` that
  `symbion summary --hook` writes. The two share that file's layout and nothing else.
- `symbion init` without `--yes` lists each change and makes none. It writes no
  `CLAUDE.md` line; its only write into a project is a `.symbion` pointer, for a
  store that is not the default sibling. `adoption.md` turns on a `file` catalog in
  the store's `symbion.toml` at its first step, shows the user a candidate table
  before it writes an entry, and puts each retired source on a delete list.
- A `settings.json` hook merged mid-session went live without a restart. A plugin
  installed mid-session does too (the spike, below).

## The design

### 1. A thin plugin in the public repo

The plugin holds no copy of the skill or of the hook scripts. The installed `symbion`
package is the one source of both, so the skill and the hooks always match the CLI
that runs them (the user, after review). The plugin holds:

- `.claude-plugin/plugin.json`, and `.claude-plugin/marketplace.json` naming the
  repo as its own marketplace;
- `icon.png`;
- `commands/setup.md`, the setup command (§3);
- a stub skill, `skills/symbion/SKILL.md`: the full skill's frontmatter
  description, so it triggers as today, and a body whose `` !`cmd` `` prints the
  installed package's skill, or says to run `/symbion:setup` when no CLI is
  installed (the spike's third result says how that command reaches the agent);
- `hooks/hooks.json`, with small POSIX `sh` shims that hand each event to the
  package's own `session_start.sh` and `gotchas.sh`.

The shims keep two behaviours that cannot wait for the package. With no `symbion` on
PATH, the session-start shim does what `session_start.sh` does now: silent where no
store, `.symbion` or `SYMBION_DIR` exists, and naming the absence where one does.
The gotcha shim runs on every tool call, so it must not start Python to find the
package. How it finds the package's directory (session start can record it in the
per-session cache) is the builder's call.

The repo is the marketplace, so each release's cut carries the plugin, and cutting
a release publishes it. This checkout keeps working on live edits: with the plugin
installed, the shims reach the editable install's `src/symbion/data/skill/`.

### 2. Versions

The plugin's own parts (the command, the stub, the shims) are small, and they rely
on the CLI for everything else. Setup still relies on `init`'s behaviour under §4,
which an older CLI lacks.

- `plugin.json`'s `version` equals `pyproject.toml`'s. A test holds them equal, and
  the release recipe's version step names both files.
- Setup checks `symbion -V`. A CLI older than the plugin's version goes into
  approval 1 as an upgrade (the same `uv tool` or `pipx` command, with `upgrade`).
- The install in setup is unpinned. *(The builder's call: a pin to the plugin's
  version fails between a release's cut and its PyPI upload.)*

### 3. `/symbion:setup`

A plugin command (`commands/setup.md`). Its steps live in that file, not in
SKILL.md, whose size is capped. The agent:

1. **Says what symbion is**, in two sentences, and what setup will do.
2. **Checks the CLI**: `command -v symbion` and `symbion -V`.
3. **Plans the install** if the CLI is missing: `uv tool install 'symbion[gui]'` when
   `uv` is on PATH, else `pipx install 'symbion[gui]'`. With neither, it says so,
   links the README's Install section, and stops. It never runs `pip` into a system
   Python. *Changed at the build, 2026-10-30:* the uv command is `uv tool install
   --python '>=3.11' 'symbion[gui]'`. A stranger run on macOS, whose only Python was
   the system's 3.9, showed that the bare command picks 3.9 and refuses. With the
   floor, uv uses a Python 3.11 or later it finds, or downloads one into its own
   directory. Approval 1 says so.
4. **Describes `init`.** With the CLI present, it runs `symbion init` without
   `--yes` and shows that list. Without it, no dry run can run before approval
   (`uvx` would write a cache), so it describes what `init` does by default: the
   store `../<repo>-notes`, and no pointer for the default sibling. It adds what
   `init` does not write, the `CLAUDE.md` line (`- We use symbion for durable notes
   and tickets.`). Where `CLAUDE.md` is gitignored or reviewed, the line is proposed,
   not written, as the guide says now. *(The builder's call, after review.)*
5. **Asks once** for the install, `init` and the line together, saying what each
   writes and where: the tool's own directory and a `symbion` command on PATH; the
   store and its `symbion.toml`, including the `file` catalog adoption's first step
   turns on; the line. On no, it stops and has written nothing. On yes, it installs,
   then runs `init` without `--yes` again. A list that differs from what it described
   stops for its own approval. Otherwise it runs `init --yes` and writes the line.
   *(The builder's call, after review: two approvals is the usual case, not a
   promise. The catalog config is inside the store approval 1 created, so adoption
   runs unchanged.)*
6. **Runs adoption** per `adoption.md` as it stands: the catalogs, the sources (TODO
   files, handoff notes, Claude Code memory, the git log since the last handoff), the
   candidate table, and the retire list. The plan names each source file and what
   happens to it. That is the second approval. On yes, the entries go in as one
   validated `symbion add --from-json -`, the sources retire as `adoption.md` says,
   and `symbion commit` saves the store.
7. **Empty project.** When there is nothing to fold in, it says so. It sends the user
   to the live demo (https://symbion.nullphase.net/) to see a store weeks in, beside
   their own empty one.
8. **Serves.** It starts `symbion serve` in the background, gives the URL, and says
   how to stop it.

The store sits beside the project, outside its directory. Where Claude Code's
permissions or sandbox fence writes to the project, the store's writes ask too. The
step-5 explanation names the store's path so that prompt is no surprise.

### 4. `init` on Claude Code

`init` (and `init --agent claude`) makes the store as now, and:

- never links `~/.claude/skills/symbion`, and never registers or prints a
  `settings.json` hook. `--merge-hook` no longer applies to Claude Code;
- says that on Claude Code the skill and hooks come from the plugin, with its
  install line;
- where the old link or the old hooks exist, says the plugin replaces them, that
  both together give two session-start summaries and two gotcha lines, and prints
  the command or edit that removes each. It never removes them itself:
  `settings.json` is the user's.

`init` needs no test for whether the plugin is installed, so it never reads Claude
Code's own files. `init --agent codex` and `--agent hermes` are unchanged.

### 5. The skill's hint

The session-start text ends with "before a write: load the symbion skill (PATH)",
where PATH is `~/.claude/skills/symbion/SKILL.md` or the Codex link. Under §4 the
Claude link no longer exists. Where neither link exists, the hint names the
installed package's own `SKILL.md`, the file the stub skill sends the agent to.

*Changed at the build, 2026-10-30:* under the plugin's hook the hint names
`symbion:symbion`. The spike found `CLAUDE_PLUGIN_ROOT` in the hook's environment,
and the stub is the path that fills the schema in. The package-path fallback is
dropped. Its one case, a plugin-only install, is that case. An older decision
also holds: with no link and no plugin, the summary names no skill, since a
model once read a skill's path as the place to load it from.

### 6. Docs and the maintainer's checkout

- The README's Install section leads with the plugin commands. The pipx route stays
  below, for Codex, Hermes and the CLI alone.
- The guide gets a plugin section with the same steps as `/symbion:setup`, so a
  reader knows what the agent will do before they run it. Its Claude Code
  `settings.json` and `--merge-hook` text goes.
- The release recipe adds `plugin.json`'s version.
- CLAUDE.md's "If this checkout is your installed symbion" section says the plugin's
  shims, not a skill link, reach this checkout's `src/`. Moving an install made
  before the plugin to the plugin (install it, then remove the old link and
  `settings.json` entries) is the user's step.

## Spike (run 2026-10-30)

Throwaway: Claude Code 2.1.314 on macOS, a scratch config directory
(`CLAUDE_CONFIG_DIR`, signed in, with none of the maintainer's settings, skills or
instructions), a scratch project, and a scratch plugin served as a local-path
marketplace. Its stub skill and command carried `` !`cmd` `` lines; its hooks
printed a marker and logged their environment.

1. **Live without a restart: yes.** The install answered "Installed symbion. Plugin
   is now active." The next tool call got the plugin's PostToolUse context,
   `/symbion:setup` ran, and the skill loaded, all in the session that installed
   it. SessionStart fired at the next session's start. **The install starts
   nothing by itself,** but the agent sees it at the user's next message: it quoted
   "a local command record for /plugin" with the install's output. So
   `/symbion:setup` stays the second command. *(The builder's call: the stub
   skill's description also names "right after the symbion plugin was installed",
   so a user who types anything else next is offered setup.)*
2. **One line:** `/plugin install symbion --marketplace <source>` adds the
   marketplace and installs. It asks to add the marketplace (`y`) and for a scope
   (Enter picks user). That is two keypresses, not a second command. Tried with a
   local path as the source; with `phreakocious/symbion` it waits on the release
   that carries the plugin. A path source runs the plugin from that directory
   (`CLAUDE_PLUGIN_ROOT` was the source path), so the GitHub install's cache copy is
   untried.
3. **The stub reaches the full skill.** `` !`cmd` `` in a plugin skill ran inline
   for an `echo`. For a command reading the package's `SKILL.md` from outside the
   project, Claude Code handed it to the agent as an instruction instead. The agent
   ran it unprompted ("The skill asks me to run a command first to get its body
   text") and got the whole skill, about 10k characters, first line to last. Both
   paths deliver it. Text that injection inserts is not expanded again: the full
   skill's own `` !`symbion schema` `` arrived as literal text. So the command the
   stub runs should print the skill with the schema table already in it. *(The
   builder's call: a `symbion` subcommand for that.)*
4. **Not run: the shims finding the package** under `uv tool` and `pipx`, on macOS,
   Linux and Windows. That lookup is build work, not a question. It is the plan's
   first task, tested on all three platforms.

`CLAUDE_PLUGIN_ROOT` and `CLAUDE_PROJECT_DIR` are both in the hook's environment.

## Verification

- The anti-goal "nothing before approval" gets an eval: a scaffolded project with a
  TODO file and a handoff note, the prompt `/symbion:setup`, and the user declining
  at step 5. A grader checks that no file outside Claude Code's own state changed.
- An eval for the yes path: the store exists, the TODO items became entries, the
  retired files went on the delete list, and the serve URL was given.
- `claude plugin validate --profile directory --strict` passes on the repo.
- Tests: the version test fails when the two versions differ; `init` on Claude Code
  writes no link and no `settings.json` entry, and names an old one. Before trusting
  each, break it once (CLAUDE.md).
- By hand, a stranger's run on Linux: a fresh user with no symbion, the
  commands, both approvals, and the page opened.

## Left to the builder

- How the gotcha shim finds the package's directory without starting Python (§1).
- The stub skill's and the setup command's wording, within §1 and §3.
- The text `init` prints for the plugin and for an old link or hook (§4).
