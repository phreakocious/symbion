# symbion for Claude Code and Antigravity

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

There the plugin carries the skill and the session-start hook: Antigravity
loads no plugin commands, and has no gotcha hook.

The plugin carries no copy of the skill: its stub and hooks run the installed
CLI's own, so the two always match. More: https://github.com/phreakocious/symbion
