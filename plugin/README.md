# symbion for Claude Code, Antigravity and Pi

A notebook and ticket registry for one project, shared by you and your coding
agent. Each new session starts with the open bugs, tasks and questions, and a
trap filed on a file reaches the agent when it opens that file.

Install, in Claude Code:

```
/plugin install symbion --marketplace phreakocious/symbion
/symbion:setup
```

`/symbion:setup` installs the `symbion` CLI, creates the project's store and
brings in the project's open threads. It says what it will write, and where,
and asks before each write. `/symbion:wrap` ends a session: the entries it owes,
then a store commit whose message is the next session's handoff.

In Antigravity, install the `symbion` CLI first (see the link below), then:

```
agy plugin install https://github.com/phreakocious/symbion/tree/main/plugin
```

There the plugin carries the skills (/symbion:setup, /symbion:wrap) and the
session-start hook: Antigravity has no gotcha hook.

In Pi, the repo itself is the package:

```
pi install git:github.com/phreakocious/symbion
/symbion-setup
```

There its extension, `plugin/pi/symbion.ts`, runs these hooks, adds the skill
(/skill:symbion) and carries /symbion-setup and /symbion-wrap.

The plugin carries no copy of the skill: its stub and hooks run the installed
CLI's own, so the two always match. More: https://github.com/phreakocious/symbion
