# A plugin update brings the matching symbion

**Date:** 2026-11-11
**Status:** Designed with the user on 2026-11-11, in conversation. A reviewer's read
the same day raised five points; each is settled in the text, and the calls made
without the user are marked "my call". Built 2026-11-11; the dated notes marked as
built record where it differs.
**Relates to:** the plugin design (2026-10-30), §2 Versions, which this extends.
Its install stays unpinned, and its anti-goal against speaking in a project that
never adopted symbion stands.

## The need

The Claude Code plugin is a shim. The stub skill runs `symbion skill`, and the two
hooks run the installed package's own `session_start.sh` and `gotchas.sh`. The
package is the app: the CLI, the skill text, the hook logic and the GUI.

A plugin update replaces the shim and nothing else. Nothing upgrades the package,
and nothing says it is behind. Setup checks `symbion -V` against the plugin's
version, but only when setup runs, and a plugin update never runs it. The old CLI
keeps working through the new shim, so the user sees no symptom. They run the old
symbion until they happen to run setup again.

The user wants it "just happening with the upgrade in Claude Code", and said "a
workable upgrade path is important" (2026-11-11).

A checkout with an editable install and a plugin from a directory marketplace never
takes this path, so the maintainer's own use never shows the gap.

## Goals, non-goals, anti-goals

**Goals**

- After a plugin update, the package reaches at least the plugin's version with no
  step from the user. It happens at the next session start, or at the next tool
  call after `/reload-plugins`.
- One install: the one setup made with `uv tool` or `pipx`. The terminal, the agent
  and `symbion serve` run the same version.
- The agent hears what changed, and the user hears it from the agent.
- Setup offers to turn on auto-update for the symbion marketplace, so the plugin
  update itself needs no step either.

**Non-goals**

- Antigravity and Pi. Claude Code comes first (the user, 2026-11-11). Both carry
  their own copy of the plugin and have the same gap.
- Codex and Hermes. They link the skill from the installed package and carry no
  plugin, so they have no version mismatch to repair.
- An install setup did not make: plain `pip`, an editable install, a system package.
- A pin to the plugin's exact version. §2 of the plugin design keeps the install
  unpinned.

**Anti-goals**

- A second copy of symbion in the plugin's data dir. That moves the version
  mismatch to between the terminal and the agent.
- A hook that holds up the session for the install, or slows every tool call.
- A downgrade of a newer CLI. Any change to an editable install.
- A failed upgrade that retries against the network on every tool call.
- A failure nobody hears about.
- A line in a project that never adopted symbion (kept from the plugin design).

## What exists (measured 2026-11-11)

In this repo:

- `plugin/hooks/hooks.json`: SessionStart with matcher
  `startup|resume|clear|compact` and a 10 s timeout; PostToolUse and
  PostToolUseFailure on `Read|Edit|Write|NotebookEdit|Bash` with a 5 s timeout.
- `plugin/hooks/session_start.sh` asks `symbion skill --path` at each start (about
  0.2 s) and caches the answer. With no working CLI, it speaks only where the
  project adopted symbion: `SYMBION_DIR`, a nonblank `.symbion`, or the sibling
  store. `plugin/hooks/gotchas.sh` reads the cache with shell builtins and never
  starts Python. It runs on every tool call.
- Antigravity runs the same `session_start.sh` (`sh hooks/session_start.sh
  antigravity`), and so does Pi's extension (`sh session_start.sh pi`).
- `test_setup_command_carries_the_specs_strings` holds setup's "symbion X or
  later" to `plugin.json`'s version. A test caps setup's `SKILL.md` below 5,120
  bytes; it is 5,025.
- `symbion serve --restart` and `--stop` refuse on Windows: they need POSIX
  signals.
- The release recipe publishes from a release branch, and moves public `main`,
  the marketplace, only after PyPI's JSON for the version answers 200. A cut to
  `main` between releases is still possible, and its `plugin.json` then names a
  version PyPI does not have yet.

In Claude Code's docs (code.claude.com/docs/en/plugins/: `loading.md`,
`install.md`, `components.md`, `manifest-reference.md`, `cli-reference.md`; read by
a helper on 2026-11-11):

- Auto-update is on by default only for Anthropic's marketplaces and marketplaces
  added from claude.ai. It is off for every other marketplace, symbion's included.
  The user turns it on in `/plugin`, under Marketplaces, or with `autoUpdate` on an
  `extraKnownMarketplaces` entry in settings.
- A running session keeps the plugin versions it loaded. Claude Code shows "Plugin
  updated: <name> · Run /reload-plugins to apply". `/reload-plugins` switches hooks
  to the new version's path. The user types it; the docs give the model no way to
  run it.
- No plugin event runs code at install, update or enable. The documented pattern for
  dependencies is a SessionStart hook that compares a manifest in
  `${CLAUDE_PLUGIN_ROOT}` with a copy in `${CLAUDE_PLUGIN_DATA}`, installs when they
  differ, and deletes the copy when the install fails. The example is npm; for
  Python the docs say only that a hook can install into the data dir.
- `${CLAUDE_PLUGIN_DATA}` is `~/.claude/plugins/data/<id>/`. It is kept across
  plugin updates and deleted when the plugin is uninstalled.
- No environment variable names the plugin's version.
- SessionStart's documented sources are `startup`, `resume`, `clear`, `compact` and
  `fork`. Claude Code 2.1.322's binary holds the same five. Whether
  `/reload-plugins` fires SessionStart is undocumented, so this design does not
  rely on it.

## The design

### 1. The check

Each shim reads its own version from `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`
with shell builtins, and looks for a marker for that version in
`${CLAUDE_PLUGIN_DATA}`. When the marker exists, the check ends there. In
`gotchas.sh` the check starts no process and adds no measurable time to a tool call.

A marker per version, not one copy of `plugin.json` as in Anthropic's example (my
call). Two sessions can run two plugin versions at once: one reloaded, one not. With
one shared copy, each would see the other's file as a change and rewrite it, on
every tool call.

With `CLAUDE_PLUGIN_DATA` unset, the check does nothing. Antigravity and Pi run the
same `session_start.sh` and do not set it.

The SessionStart path covers a new session. The gotcha path covers `/reload-plugins`
in a running session: after a reload, the next tool call runs the new shim.

### 2. The upgrade

With no marker, the shim starts the upgrade detached and returns at once. It runs
in any project: the install is the user's, not the project's. The upgrade:

1. Takes a lock in the data dir, so two sessions starting together run one upgrade.

   *Changed at the build, 2026-11-11:* the lock is a `hold-<pv>` directory made with
   `mkdir`. A session start retries a hold over an hour old, and that retry is a
   known race: two session starts within milliseconds, over a hold past an hour,
   can launch two workers. It is recorded as a `shortcut:` in `upgrade_lib.sh`.
2. Stops if an attempt for this version ran less than an hour ago (my call), so a
   failure or a wait does not retry on every tool call.
3. Finds how symbion was installed: `uv tool`, `pipx`, editable, other, or none.
   - Editable or other: no upgrade. It writes the marker and leaves a notice (§3),
     so the notice comes once per plugin version.
   - None: no upgrade, no notice. It writes the marker. Setup installs the current
     version, and `session_start.sh` already sends an adopted project to setup.
4. Compares the CLI's version with the plugin's. A CLI at or past the plugin's
   version needs nothing; a `.devN` of the plugin's version counts, as in setup.
   It writes the marker.
5. Runs `uv tool upgrade symbion` or `pipx upgrade symbion`, whichever made the
   install. The install keeps its extras, `[gui]` included.
6. Checks `symbion -V` again.
   - At or past the plugin's version: it writes the marker and leaves the result
     (§3). It runs `symbion serve --restart`, except on Windows, where the result
     says to restart any serve by hand.
   - The command succeeded but the version is still short: PyPI does not have the
     plugin's version yet, as after a cut between releases. It is a wait, not a
     failure: no marker and no line (my call). The throttle retries it.
   - The command failed: no marker. It leaves the failure (§3). On Windows, a
     running `symbion serve` can hold the install's files; the failure line then
     says to stop the serve.
7. Appends the attempt to a log in the data dir (my call): its UTC time, the
   versions before and after, the install kind, the command, and on a failure the
   step it failed in and the command's output.

On Windows the shims run under Git's `sh`. If a detached process there dies with
the hook, Windows gets the notice for the "other" kind instead of an upgrade (my
call), and the build does not wait on a fix.

### 3. What the agent hears

Each attempt leaves at most one result: an upgrade, a failure, or a notice. The
next hook run in a project that adopted symbion prints it for the agent, once in
total, and marks it delivered (my call). The test for adoption is the one
`session_start.sh` already uses with no CLI. A project that never adopted symbion
hears nothing, so the line may wait for the user's next symbion project.

- After an upgrade: the old and new versions. Mid-session, that the skill text
  already in context is the old version's, so the agent runs `symbion skill`
  before its next symbion write.
- After a failure: what failed, and the command the user can run by hand.
- For an editable or other install: that the plugin expects symbion X or later,
  and how to upgrade.

SessionStart puts the line in its text. The gotcha hook puts it in its additional
context, as it does a gotcha.

*Changed at the build, 2026-11-11:* three things differ from the text above.
SessionStart delivers nothing, and the first tool call does: a worker launched at
session start has not finished by the time that session's text is built, so a
result could only wait for a later hook run anyway. Only the main agent takes the
line, not a subagent: the line asks the agent to tell the user and to re-read the
skill, which a subagent may never relay. A failure's line goes once per version
(`told-<pv>`), not once in total.

Once in total, not once per session, is the simple choice, and it has a cost. A
second session that was already running keeps the old skill text until it compacts
or ends. Its commands run the new CLI, whose own messages still guide it.

At a session start, the upgrade runs while the session goes on, so that session's
summary comes from the old CLI. The line arrives at a later hook run.

### 4. Setup offers auto-update

On Claude Code with the plugin enabled, `init` prints one more note: turn on
auto-update for the symbion marketplace, in `/plugin`, under Marketplaces, then
symbion. Setup already shows `init`'s list in its approval, so setup's prose does
not change, and its byte cap is not touched (my call). The user does it in
`/plugin`. Nothing writes a settings file: `init` treats Claude Code's
`settings.json` as the user's.

### 5. Release order

The release order already does what this needs: PyPI first, then `main`. A cut to
`main` between releases reaches users as a plugin version PyPI lacks, which §2's
wait covers.

### 6. Docs

The README and the guide gain a short "Upgrading" section. For a plugin user: turn
on auto-update, or update in `/plugin`, then run `/reload-plugins` or start a new
session; the package follows. For everyone else: `uv tool upgrade symbion` or
`pipx upgrade symbion`, then `symbion serve --restart`.

The plugin design's §2 gets a dated note that points here.

## Verification

- Tests for each branch of §1 and §2: unset data dir, marker present, marker
  absent, the lock, the throttle, each install kind, CLI already newer, success,
  the PyPI wait, failure, and one line delivered once, in an adopted project only.
- Two shims of different versions on one data dir: each writes only its own marker,
  and neither starts an upgrade after the first.
- The gotcha shim with its marker present starts no process.
- A hand run on macOS: install the previous release with `uv tool` and again with
  `pipx`, add the new plugin from a local marketplace, update it mid-session, run
  `/reload-plugins`, and watch the upgrade and the agent's line.
- The same on the Windows VM: whether the detached upgrade outlives the hook, and
  whether a running serve blocks the upgrade.
- Settle by a throwaway plugin that logs every SessionStart source whether
  `/reload-plugins` fires SessionStart. The design works either way; the answer
  decides which path fires first and goes in this document. (2026-11-12: skipped,
  the user's call. Whether a reload fires SessionStart stays unmeasured.)

## Left to the builder

- How the upgrade finds the install kind: the CLI's path, `uv tool list`, `pipx
  list`, or the package's own location. It must work with a CLI that predates
  this design.
- Where the logic lives: in the `sh` shims, or in a new CLI subcommand that the
  shims call, with a fallback in `sh` for an older CLI.
- How the lock, the throttle and the delivery mark work.
- Whether `pipx upgrade` keeps the `[gui]` extra. Measure it.
- Whether `init` can tell that auto-update is already on, and stay quiet then.
- The wording of each line.
