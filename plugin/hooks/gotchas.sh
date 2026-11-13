#!/bin/sh
# The plugin's PostToolUse and PostToolUseFailure hook: the installed package's
# gotchas.sh, found through the skill dir session_start.sh beside this file
# cached. It runs on every tool call, so it never starts symbion; with no
# cache yet it does nothing. It also launches the plugin's upgrade worker when no
# marker says it ran, with builtins only once the marker exists. POSIX sh.
# As session_start.sh does: Pi on Windows names the hook with `\` alone.
here=.
case $0 in */*) here=${0%/*} ;; *\\*) here=${0%\\*} ;; esac
if [ -r "$here/upgrade_lib.sh" ]; then
  . "$here/upgrade_lib.sh"
  symbion_upgrade_check tool
fi
d=
{ read -r d < "${XDG_CACHE_HOME:-$HOME/.cache}/symbion/skill_dir"; } 2>/dev/null
# A result from the worker goes to the agent first: only then does the shim
# leave its builtins (the file test is one).
if [ -n "$CLAUDE_PLUGIN_DATA" ] && [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -f "$CLAUDE_PLUGIN_DATA/upgrade/result" ] \
   && command -v symbion_upgrade_deliver >/dev/null 2>&1; then
  symbion_upgrade_deliver "$d"
  exit 0
fi
[ -f "$d/gotchas.sh" ] && exec sh "$d/gotchas.sh"
exit 0
