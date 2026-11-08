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
# The call less its symbion commands and their heredoc bodies: a write about a
# file, or a read of its entries, is no use of it, and spent the line before
# the first use (2026-11-05). A symbion command's word is `symbion` or a path
# to it, first in its segment.
# A variable set to symbion, or a path to it, is symbion where it is called:
# `S="symbion --dir D"; $S add` wrote about a file and spent its gotcha
# (2026-11-06).
# ponytail: a separator inside a quoted argument ends its segment early, and
# an env prefix (`X=1 symbion`) hides the command: both still fire, as before.
nosymbion() {
  rest=$1 out= r=$1
  while :; do
    case "$r" in *symbion*) ;; *) break ;; esac
    pre=${r%%symbion*}; r=${r#"$pre"symbion}
    case "$r" in ' '*|\\\"*|\'*|';'*|'&'*|'|'*|\\n*|'') ;; *) continue ;; esac
    w=${pre##*[ ;\&\|\(:]}; w=${w##*\\n}; w=${w#\"}      # NAME=, NAME=\" or NAME='
    n=${w%%=*} v=${w#*=}; v=${v#\\\"}; v=${v#\'}
    case "$w" in *=*) ;; *) continue ;; esac
    case "$n" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
    case "$v" in ''|*/) ;; *) continue ;; esac            # or a path to it
    for f in "\$$n " "\${$n} "; do
      while :; do
        case "$rest" in *"$f"*) rest=${rest%%"$f"*}"symbion "${rest#*"$f"} ;; *) break ;; esac
      done
    done
  done
  while :; do
    case "$rest" in *'symbion '*) ;; *) break ;; esac
    pre=${rest%%'symbion '*}
    rest=${rest#"$pre"symbion}
    w=${pre##*[;\&\|\(\"]}; w=${w##*\\n}; w=${w#"${w%%[! ]*}"}
    case "$w" in
      *' '*) out=$out${pre}symbion; continue ;;
      ''|*/) ;;
      *) out=$out${pre}symbion; continue ;;
    esac
    seg=${rest%%[;\&\|]*}; seg=${seg%%\\n*}
    rest=${rest#"$seg"}
    case "$seg" in
      *'<<'*)                                   # its body runs to the delimiter's line
        d=${seg#*<<}; d=${d#-}; d=${d#"${d%%[! ]*}"}
        case "$d" in
          \'*) d=${d#\'}; d=${d%%\'*} ;;
          \\\"*) d=${d#\\\"}; d=${d%%\\\"*} ;;
          *) d=${d%%[!A-Za-z0-9_]*} ;;
        esac
        case "$rest" in *"\\n$d"*) rest=${rest#*"\\n$d"} ;; *) rest= ;; esac ;;
    esac
    out=$out$pre
  done
  printf '%s' "$out$rest"
}
case "$call" in *symbion*) call=$(nosymbion "$call") ;; esac
# Windows sends C:\\proj\\src\\x.py; a row names src/x.py.
case "$call" in *'\\'*) call=$(printf '%s' "$call" | sed 's#\\\\#/#g') ;; esac
# Whether the call names file $2 of a repo whose root dir is named $3: at a
# path boundary, from the root (`cat src/x.py`, `./src/x.py`) or under a dir
# named as the root is (`/home/u/proj/src/x.py`), or under a linked worktree of
# it, whose `.git` file names `<root>/.git/worktrees/`: a Read or Edit in
# `.worktrees/NAME/` by absolute path got no gotcha (dogfood, 2026-11-04). A
# row on README.md fired on src/README.md (a clean-room review, 2026-10-13).
# ponytail: the root by its name, so a symlink that keeps the name matches;
# from a cwd below the root, `cat x.py` reads as the root's x.py; a relative
# gitdir (worktree.useRelativePaths) never matches: resolve it if that bites.
names() {
  rest=$1 lead=
  while :; do
    case "$rest" in *"$2"*) ;; *) return 1 ;; esac
    pre=$lead${rest%%"$2"*}
    rest=${rest#*"$2"}
    lead=${2#"${2%?}"}
    case "$rest" in [A-Za-z0-9._~/-]*) continue ;; esac
    # A line's start too: `\n` is JSON's newline, and its `n` no path's.
    case "$pre" in ''|*[!A-Za-z0-9._~/@+-]|*[!A-Za-z0-9._~/@+-]./|*\\[nrt]|*\\[nrt]./) return 0 ;; esac
    # No root (a list from before roots, or a repo not on this machine): any
    # dir, as before.
    case "$pre" in */"$3"/) return 0 ;; */) [ -z "$3" ] && return 0 ;; *) continue ;; esac
    # The dir: a JSON string's whole tail (a path with a blank), else a word's.
    for p in "${pre##*\"}" "${pre##*[!A-Za-z0-9._~/@+:-]}"; do
      [ -f "${p}.git" ] || continue
      g=; { IFS= read -r g < "${p}.git"; } 2>/dev/null
      case "$g" in "gitdir: "*/"$3/.git/worktrees/"*) return 0 ;; esac
    done
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
