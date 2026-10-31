#!/bin/sh
# The plugin's PostToolUse and PostToolUseFailure hook: the installed package's
# gotchas.sh, found through the skill dir session_start.sh beside this file
# cached. It runs on every tool call, so it never starts symbion; with no
# cache yet it does nothing. POSIX sh.
d=
{ read -r d < "${XDG_CACHE_HOME:-$HOME/.cache}/symbion/skill_dir"; } 2>/dev/null
[ -f "$d/gotchas.sh" ] && exec sh "$d/gotchas.sh"
exit 0
