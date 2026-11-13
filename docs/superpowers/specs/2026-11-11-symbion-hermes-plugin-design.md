# Symbion on Hermes: a plugin, listed in Hermes's catalog

**Date:** 2026-11-11
**Status:** Designed with the user on 2026-11-11, in conversation. Not built.
**Relates to:** the Pi design (2026-11-02), whose shape this follows; the plugin design
(2026-10-30), whose shims this reuses. It reverses the call of 2026-10-06 that Hermes
gets a skill and no hook.

## The need

symbion has supported Hermes Agent (NousResearch/hermes-agent) with a skill and no hook
since 2026-10-06. At that time the user judged a plugin too ugly for a harness that
neither of us ran. Three things changed since then:

- An external tester installed symbion under Hermes on Ubuntu (2026-10-13).
- Pi got a plugin at parity with Claude Code (2026-11-02).
- Hermes grew a public plugin catalog.

Without a hook, a Hermes session gets the summary only when the project's `AGENTS.md`
tells the model to run it. It gets no gotchas at all.

The user, 2026-11-11: build the plugin and list it in Hermes's catalog. It must work for
us first, then for that tester.

## Goals, non-goals, anti-goals

**Goals.** These are the Pi bar:

1. The summary at session start, after a resume, and after a compaction.
2. A gotcha line the first time a session's tool call names the gotcha's file.
3. Writes from Hermes's terminal are signed `hermes`, which is done (2026-10-06), and
   carry Hermes's session, which is not done.
4. `/symbion-setup` and `/symbion-wrap`.
5. `symbion init --agent hermes` names what is missing.
6. A catalog entry that `hermes plugins validate` passes.

The readers, in order:

1. The maintainer, on macOS.
2. The tester, on Ubuntu. They install from the public repo until the catalog lists the
   plugin, then by name.
3. A stranger who finds symbion in the catalog. This cut does not aim at them, but the
   listing must need no rewrite for them.

**Non-goals** (the builder's calls, made in conversation)

- The upgrade worker that the Claude Code plugin has. The catalog bans self-updating code
  (its rule 3).
- Hermes on Windows. Where `sh` is missing, the plugin says nothing.
- Model names in the author.
- Filing the catalog PR. The repo's owner files it (rule 5), after a release.

**Anti-goals.** If any of these happens, the build has failed, even if it works:

- symbion speaks in a project that never adopted it.
- A slow or broken `symbion` stalls a turn or a tool call.
- A second copy of the session-start logic, the gotcha matching, the skill, or the setup
  and wrap text.
- The summary on every turn.
- Two installs of the plugin that compete for one slot in `$HERMES_HOME/plugins`.

## What exists (read 2026-11-11)

**In symbion**

- `plugin/` is one plugin directory for three hosts:
  - Claude Code: `.claude-plugin/`.
  - Antigravity: `.antigravity-plugin/` and `hooks.json`.
  - Pi: `pi/symbion.ts`, which the root `package.json` names.
- The shims `hooks/session_start.sh` and `hooks/gotchas.sh` run the installed CLI's own
  scripts. They find them through `symbion skill --path`, whose answer they cache. `$1`
  names the host: `antigravity` or `pi`, else Claude Code. The shim exports it as
  `SYMBION_HOST`.
- The shims' upgrade check runs only when `CLAUDE_PLUGIN_DATA` and `CLAUDE_PLUGIN_ROOT`
  are both set.
- `init --agent hermes` links `$HERMES_HOME/skills/symbion` (the user's choice,
  2026-10-15). It also prints an `AGENTS.md` line that tells the model to run `symbion
  summary`.
- `api.author_default` gives `hermes` when `HERMES_AGENT` is set. `store.session_key` has
  no Hermes branch.
- When `HERMES_AGENT` is set, the summary's skill hint reads `skill_view symbion`. Hermes
  exports `HERMES_AGENT` to terminal commands. The plugin's hook runs in Hermes's own
  process, where `HERMES_AGENT` may not be set.
- Pi's extension, `plugin/pi/symbion.ts`, is the model for this plugin. It maps tool
  names, renames `path` to `file_path`, and builds a command's prompt from a preface and
  a skill body.

**In Hermes** (source at `8c1f473295`, v0.23.1 plus canary commits, 2026-11-11; nothing
was run)

- A plugin is a directory with `plugin.yaml` and an `__init__.py` that exports
  `register(ctx)`. It lives in `$HERMES_HOME/plugins/<name>/` and loads after `hermes
  plugins enable <name>`.
- Each callback takes `**kwargs`. When a callback raises, Hermes logs it and skips it.
  A `pre_llm_call` callback times out at 30 s (`plugins.hook_callback_timeout`).
- `hermes plugins install owner/repo#subdir` makes a blobless clone with a sparse checkout
  of that subdir. The installed directory takes the manifest's `name`. When a link
  already holds that slot, the install refuses. A catalog entry is the same install, at
  a pinned 40-character SHA, with `subdir:`.
- `pre_llm_call` gets `session_id`, `is_first_turn`, `model` and `platform`.
  `is_first_turn` is true only when the history is empty, so it is false on a resume.
  - A returned `{"context": ...}` is appended to that turn's user message.
  - Hermes stores the bytes it sent in the message's `api_content` column, and replays
    them on later turns and on a resume. So the injection stays in history. The developer
    guide still calls it ephemeral. The code keeps it.
  - Context over 10,000 characters spills to a file, with a head and tail preview.
- `register_system_prompt_section` freezes its text once per session, caps it at 4,000
  characters, and restores the frozen text on a resume.
- The hooks, tools and injected turns of every local session (classic CLI, TUI, Desktop)
  and of every chat run in one long-lived gateway process. `hermes chat` attaches to it,
  and `/quit` only detaches.
  - So a process-local record outlives a session. A resume rarely starts a new process.
  - `os.getcwd()` is the gateway's own directory. The hook's environment (`PATH`,
    `HERMES_HOME`, `XDG_CACHE_HOME`) comes from whatever started the gateway, not from
    the user's shell.
- `on_session_open` fires in the gateway before a local session's first turn, with
  `reason` set to `create` or `resume`. It does not fire for messaging chats.
- Compaction keeps the session id by default (`compression.in_place: true`).
  - `post_compaction` then passes `old_session_id: ""`. Rotation mode, the old default,
    gives a new id.
  - `kind` is one of `summarize`, `provider_native`, `micro_summarize` (opt-in, between
    turns) and `prune_tool_results` (proactive).
  - The first exchange is always kept verbatim (`protect_first_n`). That exchange is the
    first user message, which carries a first-turn injection.
- A `delegate_task` child is a separate agent with its own session id and `platform:
  "subagent"`.
  - Its `pre_llm_call` passes `parent_session_id`.
  - It skips context files, so it never reads `AGENTS.md`.
  - Its terminal commands carry the child's own `HERMES_SESSION_ID`.
- `transform_tool_result` passes `tool_name`, `args`, `result` (a string) and the call's
  ids, `session_id` among them. The first string that a callback returns replaces the
  result.
- The tools are `terminal` (`args.command`), and `read_file`, `write_file` and `patch`
  (`args.path`).
  - For OpenAI-family models, `patch` also has `mode: "patch"`. In that mode it takes a
    V4A patch and no `path`, and it names each file in a `*** Update File:`, `*** Add
    File:`, `*** Delete File:` or `*** Move to:` line. *2026-11-11, at the final review:* that is
    Codex's dialect. Hermes's parser (`tools/patch_parser.py`) takes any blanks after
    `***`, and a move as `*** Move File: a -> b`, which touches both files.
- `ctx.register_session_env(names, resolver)` gives each session's tool subprocesses the
  variables that `resolver(session_id)` returns, resolved when each one starts.
- `ctx.register_command(name, handler, description)` adds a slash command. The handler
  returns text, which is shown to the user.
- `ctx.inject_message(content)` queues a user message. In the classic CLI it goes into
  the REPL's input. Through the gateway it needs
  `plugins.entries.<id>.allow_gateway_injection`.
- The session's working directory comes from `ctx.session_cwd()` (v0.22.3 on), or from
  `agent.runtime_cwd.resolve_agent_cwd()`, which the guide documents as public.
- Terminal commands carry `HERMES_AGENT=true` and `HERMES_SESSION_ID`. This was measured
  in the CLI on 2026-10-06.
- The catalog's admission rules are in `plugin-catalog/README.md`. They require:
  - a PR that a human merges;
  - an exact SHA;
  - no self-updating code;
  - a PR from the repo's owner;
  - declared hooks that match the registered ones;
  - a scanner run at admission;
  - a SemVer `requires_hermes` floor, in the entry and in `plugin.yaml`;
  - disclosure of shell commands, network calls and reads outside the plugin's data.

## The design

### Where it lives, and how it installs

- `plugin/plugin.yaml` and `plugin/__init__.py` go beside the other hosts' manifests. The
  manifest has `name: symbion`, `provides_hooks: [pre_llm_call, transform_tool_result,
  on_session_open, post_compaction]`, and the `requires_hermes` floor.
- Before the listing: `hermes plugins install phreakocious/symbion#plugin --enable`.
  After it: `hermes plugins install symbion`. The clone carries `hooks/`, `skills/` and
  `pi/`, and the plugin reads them in place.
- There is one install path. `init` never links the plugin.

### Session start

- On `pre_llm_call`, the plugin sends the summary when the turn is a session's first
  (`is_first_turn`), or when the session's id is marked due:
  - `on_session_open` with `reason: "resume"` marks it due. This is Claude Code's
    SessionStart with source `resume`.
  - `post_compaction` with `kind` set to `summarize` or `provider_native` marks it due.
    So does a rotation's new id. This is SessionStart with source `compact`.
  - `micro_summarize` and `prune_tool_results` mark nothing: either can run every few
    turns.
- The plugin clears the mark when it tries a delivery, not when the delivery succeeds. A
  failed or slow `symbion` then costs one turn, never every turn.
- The marks live in the gateway process. A gateway restart sends nothing by itself. A
  mark made just before a restart is lost, which is accepted.
- After an in-place compaction, the first summary stays in the protected first exchange,
  and the new one comes later. The model reads both, and the new one is current. This
  is accepted. A new summary also rewrites the session's gotcha list and clears its
  marks, as at a Claude Code session start.
- A `delegate_task` child (`platform: "subagent"`) gets no summary, as a Claude Code
  subagent gets none. Cron runs, kanban workers and chats whose working directory is an
  adopted project get it, as a headless Claude Code run does.
- Three options were rejected:
  - `register_system_prompt_section`. Its cap is 4,000 characters, and a busy store's
    summary runs past that. A resume would also replay the frozen text.
  - `is_first_turn` alone. It is false on a resume, and a compaction keeps the id.
  - A record of ids already served. In the long-lived gateway, almost no resume would
    get a new summary, and a gateway restart would send one into every live session.
- The plugin runs `sh <plugin>/hooks/session_start.sh hermes`:
  - in the session's working directory, with `SYMBION_PROJECT_DIR` set to it;
  - with `{"session_id": "<id>"}` on stdin;
  - with a 10-second timeout;
  - with `CLAUDE_PLUGIN_DATA` and `CLAUDE_PLUGIN_ROOT` removed from its environment, so a
    Hermes started from a Claude Code shell never starts the upgrade worker;
  - with `HERMES_AGENT=true`, as Hermes gives its terminal commands, and
    `SYMBION_HERMES_SESSION` set to the root session's id. The gateway's own environment
    has neither, so without them the summary would name the wrong reader.
- When the output parses as the hook's JSON, its `additionalContext` is the context. Any
  other output, such as an error or the not-installed line, is the context as it is. No
  output gives no context.
- The shim gets a `hermes` branch. It sets `SYMBION_HOST=hermes`, and its not-installed
  line names `/symbion-setup`.
- With `HERMES_AGENT` set, the summary's existing skill hint branch applies, and it reads
  `skill_view symbion`.

### Gotchas

- `transform_tool_result` handles four tools:

  | Hermes tool | Claude Code name |
  |---|---|
  | `terminal` | `Bash` |
  | `read_file` | `Read` |
  | `write_file` | `Write` |
  | `patch` | `Edit` |

  It builds Claude Code's PostToolUse JSON and runs `sh <plugin>/hooks/gotchas.sh`, as
  Pi does. For `write_file` and `patch` it renames `path` to `file_path`.
- A V4A `patch` (`mode: "patch"`) runs `gotchas.sh` once per file named in its `***`
  header lines, each as an `Edit` with that `file_path`. The header pattern is the one
  that `codex_gotchas.py` reads for Codex's `apply_patch`. Reading a tool's input this
  way adapts it, as the `path` rename does. The matching stays in `gotchas.sh`.
- When the result is an error, the event name is `PostToolUseFailure`.
- The `session_id` comes from the call's ids. The timeout is 5 seconds.
- `additionalContext` is appended to the result string after a blank line. A result that
  is not a string is left alone.
  *2026-11-12:* a result that is a JSON object gets it as a first key, `symbion`, instead.
  Hermes parses the result after this hook to tell a failed call, so text after the JSON
  made a failed `terminal` call read as a success. The preview of a result over Hermes's
  size budget keeps a JSON object's scalar keys and cuts its tail, so text after the JSON
  was cut after `gotchas.sh` had marked it delivered. Both were measured on Hermes's own
  functions (`_detect_tool_failure`, `_pageable_text`).
- Hermes's search tools stay out, as Claude Code's Grep and Glob do.
- A `delegate_task` child's calls use its root session's list. The input gets the root's
  `session_id` and the child's id as `agent_id`, so each child keeps its own marks, as a
  Claude Code subagent does. The plugin learns each child's parent from
  `parent_session_id` in the child's `pre_llm_call`, and follows the chain to the root.

### Session key

- `store.session_key` reads `HERMES_SESSION_ID` when `HERMES_AGENT` is set. It sits in
  the order that `api.author_default` gives the markers.
- A child's writes count as its root session's, as a Claude Code or Codex subagent's do.
  The parent's wrap then commits them, and `undo` reaches them.
  - Through `ctx.register_session_env`, the plugin gives each session's terminal
    `SYMBION_HERMES_SESSION`, set to its root session's id.
  - `store.session_key` reads that variable before `HERMES_SESSION_ID`, and only when
    `HERMES_AGENT` is set.
  - Without the plugin, a child's writes count as its own session's.

### Commands

*2026-11-11, after the live checks:* the plugin registers `skills/setup` and
`skills/wrap` as plugin skills instead, and the installed CLI's `SKILL.md` as
`symbion`. Hermes offers them as `/symbion:setup` and `/symbion:wrap`, the names
Claude Code and agy use, and loads them as a user message with no config. A plugin
command's `inject_message` queued nothing until `plugins.entries.symbion.
allow_gateway_injection` was set in config.yaml (live check 6). `symbion:symbion` is
the skill the wrap stub tells the model to load first. setup's `SKILL.md` gained the
Hermes lines a preface would have carried. The user's choice, between the two.
The design as first written:

- `/symbion-setup` and `/symbion-wrap` come from `ctx.register_command`.
- The handler:
  1. reads `skills/{setup,wrap}/SKILL.md`;
  2. strips the frontmatter;
  3. puts a Hermes preface first and the command's arguments last;
  4. sends the result with `ctx.inject_message`.
- When `inject_message` returns false, the handler returns text that says how to run the
  step by hand.
- The preface says:
  - in Hermes the command is `/symbion-setup`;
  - `init` takes `--agent hermes`;
  - the instructions file is `AGENTS.md`;
  - the model runs each `!` line itself;
  - the model loads the symbion skill with `skill_view symbion`.

### `init --agent hermes`

- It keeps the skill link. That link is what puts symbion in Hermes's skill index. A
  plugin's skills are never listed there.
  *2026-11-11, at the final review:* that is wrong. Hermes lists every enabled
  plugin's skills in `<available_skills>` (`_plugin_skill_prompt_rows` in
  `agent/prompt_builder.py`), under the description given at registration, and the
  plugin passes each `SKILL.md`'s. With the plugin installed, the link lists symbion
  a second time. Whether init then stops linking is the user's to decide.
- It checks that `$HERMES_HOME/plugins/symbion/plugin.yaml` exists. If it does not, init
  prints the install line.
- It prints the `AGENTS.md` summary line only when the plugin is missing.
- `--merge-hook` merges nothing, and says so.

### The catalog entry

The entry is `plugin-catalog/symbion.yaml`:

- `repo: https://github.com/phreakocious/symbion`
- `sha:` a release commit in the public repo
- `subdir: plugin`
- `maintainer: phreakocious`
- `tier: community`
- `requires_hermes:` the same floor as `plugin.yaml`
- `capabilities.provides_hooks: [pre_llm_call, transform_tool_result, on_session_open,
  post_compaction]`

Its description discloses that:

- the plugin runs the installed symbion CLI's scripts as subprocesses, at a session's
  first turn and after file and terminal tool calls;
- they read the project's symbion store and write a cache under the user's cache
  directory;
- the CLI may fetch from the store's git remote where the store syncs;
- each session's terminal gets `SYMBION_HERMES_SESSION`, its root session's id;
- `hooks/upgrade.sh` is in the clone and runs only under Claude Code.

### Docs

These change:

- the README's install section;
- `docs/guide.md`, the Hermes install;
- `docs/reference.md`;
- `SKILL.md`'s sentence on which hosts get the gotcha hook;
- CLAUDE.md's "Where things are", which gains `plugin.yaml` and `__init__.py`.

## Verification

**The test setup.** A hermes-agent checkout beside this repo, with its own venv. A test
`HERMES_HOME` outside any repo, which keeps a provider sign-in between runs. A scratch project and a scratch store for each run.

**Live checks.** Each one is recorded as a `check`:

1. The summary appears after the first prompt and not after the second. It appears again
   after an in-place compaction (`/compress`), and after a `--resume` while the gateway
   stays up.
2. A gotcha on a file reaches the result of the first `read_file` that names it, once.
   The file's first edit gets the line that `gotchas.sh` gives an edit. A V4A patch to
   the file gets it too.
3. A `symbion add` from Hermes's terminal signs `hermes`, and its session equals the
   hook's `session_id`. A `delegate_task` child's `add` carries the parent's session,
   and the child gets no summary but does get the gotcha.
4. In a project with no store, nothing is injected.
5. In an adopted project with `symbion` off PATH, the context names `/symbion-setup`.
6. `/symbion-setup` and `/symbion-wrap` send their text, and setup runs `init --agent
   hermes`.
7. `init --agent hermes`, with the plugin installed and without it.
8. `hermes plugins validate` and `hermes plugins doctor` pass on the plugin dir. Their
   findings are recorded either way.

**Python tests.** These cover the shim's `hermes` branch, `session_key`, `init` and the
summary hint. They also cover `plugin/__init__.py` with a fake `ctx`: the delivery rule
(first turn, resume, each compaction kind, a child), the tool map, the `path` rename,
the V4A headers, the child-to-root map, a timeout and a failure. Unlike Pi's TypeScript, the
plugin is Python, so the repo's pytest suite runs it with no Hermes installed. Each test
is seen to fail with its piece broken in a copy.

## Left to the builder

- Read the Hermes docs of the version that runs the checks first. This design read
  `main`.
- Set `requires_hermes` from the oldest release that carries every API the plugin uses
  (`git log -S` in the checkout).
- How a development install points at this checkout.
- Whether Hermes has a surface that shows the hook's `systemMessage` line to the person
  without blocking. If it has none, drop the line.
- Measure `inject_message` under the TUI and the gateway, not only the classic CLI.
- Check that Hermes accepts `symbion-setup` and `symbion-wrap` as command names, and that
  no built-in command takes them.
- What `symbion summary --hook` does on the network, so that the disclosure is exact.
- Whether a gateway started as a service has `symbion` on its `PATH`. If it does not,
  the not-installed line in an adopted project should say that the gateway's `PATH` is
  the one that counts.
- Whether the protected first exchange keeps its `api_content` (the first summary)
  through an in-place compaction.
- Whether `register_session_env` needs a declared capability, or a line in the catalog
  entry.
- The scanner's verdict on the whole `plugin/` clone. If it refuses, bring a narrower
  subdir to the user.

## Development and release

Until a release cut carries `plugin/plugin.yaml`, the plugin is installed from the
development checkout. After that cut, the user files the entry PR, with the release's
commit in the public repo as its `sha`. A later bump is a new PR. It is needed only when
`plugin/` changes in a way that Hermes users need.
