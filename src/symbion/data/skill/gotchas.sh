#!/bin/sh
# PostToolUse and PostToolUseFailure on Read, Edit, Write, NotebookEdit and
# Bash (a Bash call that exits non-zero is a failure): the first time a
# session's tool call names a file a #gotcha row is on, the agent gets that
# row. `symbion summary --hook` writes the session's list at session start
# (_list_gotchas in cli.py): an id, a name, the JSON-escaped line and the
# name's root dir per row and name. This runs on every such call, so it reads
# that list with shell builtins and one sed, and never runs symbion. It never
# fails a tool call.
# POSIX sh, as session_start.sh is.
input=$(cat)
sid=$(printf '%s' "$input" | sed -n 's/.*"session_id" *: *"\([A-Za-z0-9_-]*\)".*/\1/p')
case "$sid" in ''|*[!A-Za-z0-9_-]*) exit 0 ;; esac
list="${XDG_CACHE_HOME:-$HOME/.cache}/symbion/gotchas/$sid"
[ -s "$list" ] || exit 0
# What the tool was asked to do, never what it returned: a file's content
# names other files, and so does an error.
head=${input%%'"tool_input"'*}
call=${input#*'"tool_input"'}
call=${call%%'"tool_response"'*}
call=${call%%'"error"'*}
# The answer names the event it answers; hook_event_name and tool_name come
# before tool_input.
case "$head" in
  *PostToolUseFailure*) event=PostToolUseFailure ;;
  *) event=PostToolUse ;;
esac
# A subagent's call carries its agent_id (Claude Code 2.1.69 on), and gets
# its own marks: a row the main agent had got never reached a subagent that
# went on to edit the file. Read before tool_input, where a file's text
# cannot put one; with none, the session's marks.
aid=$(printf '%s' "$head" | sed -n 's/.*"agent_id" *: *"\([A-Za-z0-9_-]*\)".*/\1/p')
mark=$list${aid:+.$aid}
# An edit is matched on the file it edits, not the text it writes, and gets
# its file's trap once more: a read or a Bash call that only named the file
# spent it before the edit it was for (dogfood, 2026-10-14).
gate=$mark.seen
tool=${head#*'"tool_name"'}; tool=${tool#*'"'}; tool=${tool%%'"'*}
case "$tool" in
  Edit|Write|MultiEdit|NotebookEdit)
    gate=$mark.edit
    call=${call#*'_path"'}; call=${call#*'"'}; call=${call%%'"'*} ;;
esac
# Windows sends C:\\proj\\src\\x.py; a row names src/x.py.
case "$call" in *'\\'*) call=$(printf '%s' "$call" | sed 's#\\\\#/#g') ;; esac
# Whether the call names file $2 of a repo whose root dir is named $3: at a
# path boundary, from the root (`cat src/x.py`, `./src/x.py`) or under a dir
# named as the root is (`/home/u/proj/src/x.py`). A row on README.md fired on
# src/README.md (a clean-room review, 2026-10-13).
# ponytail: the root by its name, so a symlink that keeps the name matches;
# from a cwd below the root, `cat x.py` reads as the root's x.py.
names() {
  rest=$1 lead=
  while :; do
    case "$rest" in *"$2"*) ;; *) return 1 ;; esac
    pre=$lead${rest%%"$2"*}
    rest=${rest#*"$2"}
    lead=${2#"${2%?}"}
    case "$rest" in [A-Za-z0-9._~/-]*) continue ;; esac
    case "$pre" in ''|*[!A-Za-z0-9._~/@+-]|*[!A-Za-z0-9._~/@+-]./) return 0 ;; esac
    # No root (a list from before roots, or a repo not on this machine): any
    # dir, as before.
    case "$pre" in */"$3"/) return 0 ;; */) [ -z "$3" ] && return 0 ;; esac
  done
}
nl='
'
tab=$(printf '\t')
seen=$(cat "$gate" 2>/dev/null)
out=
while IFS="$tab" read -r id name text root; do
  names "$call" "$name" "$root" || continue
  case "$nl$seen$nl" in *"$nl$id$nl"*) continue ;; esac
  seen="$seen$nl$id"
  printf '%s\n' "$id" >> "$mark.seen"
  [ "$gate" = "$mark.seen" ] || printf '%s\n' "$id" >> "$gate"
  out="$out${out:+\\n}$text"
done < "$list"
[ -n "$out" ] && printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' "$event" "$out"
exit 0
