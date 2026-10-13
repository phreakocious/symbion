#!/bin/sh
# PostToolUse on Read, Edit, Write, NotebookEdit and Bash: the first time a
# session's tool call names a file a #gotcha row is on, the agent gets that
# row. `symbion summary --hook` writes the session's list at session start
# (_list_gotchas in cli.py): an id, a name and the JSON-escaped line per row
# and name. This runs on every such call, so it reads that list with shell
# builtins and one sed, and never runs symbion. It never fails a tool call.
# POSIX sh, as session_start.sh is.
# ponytail: a subagent's calls share its session's `seen`, so a row the main
# agent got does not reach a subagent; key `seen` by agent_id if that bites.
input=$(cat)
sid=$(printf '%s' "$input" | sed -n 's/.*"session_id" *: *"\([A-Za-z0-9_-]*\)".*/\1/p')
case "$sid" in ''|*[!A-Za-z0-9_-]*) exit 0 ;; esac
list="${XDG_CACHE_HOME:-$HOME/.cache}/symbion/gotchas/$sid"
[ -s "$list" ] || exit 0
# What the tool was asked to do, never what it returned: a file's content
# names other files.
call=${input#*'"tool_input"'}
call=${call%%'"tool_response"'*}
# Windows sends C:\\proj\\src\\x.py; a row names src/x.py.
case "$call" in *'\\'*) call=$(printf '%s' "$call" | sed 's#\\\\#/#g') ;; esac
nl='
'
tab=$(printf '\t')
seen=$(cat "$list.seen" 2>/dev/null)
out=
while IFS="$tab" read -r id name text; do
  case "$call" in *"$name"*) ;; *) continue ;; esac
  case "$nl$seen$nl" in *"$nl$id$nl"*) continue ;; esac
  seen="$seen$nl$id"
  printf '%s\n' "$id" >> "$list.seen"
  out="$out${out:+\\n}$text"
done < "$list"
[ -n "$out" ] && printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s"}}\n' "$out"
exit 0
