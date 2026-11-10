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
# No capture group: OpenBSD's sed is quadratic with one over a long line, and a
# 20 KB call outran the 5 s timeout there (2026-11-10).
sid=$(printf '%s' "$input" | sed -n '/"session_id"/{s/.*"session_id" *: *"//;/^[A-Za-z0-9_-]*"/!d;s/".*//;p;}')
case "$sid" in ''|*[!A-Za-z0-9_-]*) exit 0 ;; esac
list="${XDG_CACHE_HOME:-$HOME/.cache}/symbion/gotchas/$sid"
[ -s "$list" ] || exit 0
# What the tool was asked to do, never what it returned: a file's content
# names other files, and so does an error.
head=${input%%'"tool_input"'*}
call=${input#*'"tool_input"'}
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
# spent it before the edit it was for (dogfood, 2026-10-14). A trap this
# reader already got in full comes back as its id (below).
gate=$mark.seen
tool=${head#*'"tool_name"'}; tool=${tool#*'"'}; tool=${tool%%'"'*}
case "$tool" in
  Edit|Write|MultiEdit|NotebookEdit)
    gate=$mark.edit
    call=${call#*'_path"'}; call=${call#*'"'}; call=${call%%'"'*} ;;
  # Not before the arm above: in bash (macOS's sh) each cut is quadratic over a
  # Write's content, and 100 KB outran the hook's timeout (audit, 2026-11-08).
  # A long call is cut by sed, which is linear.
  *)
    if [ ${#call} -gt 16384 ]; then
      call=$(printf '%s' "$call" | LC_ALL=C sed 's/"tool_response".*//; s/"error".*//')
    else
      call=${call%%'"tool_response"'*}; call=${call%%'"error"'*}
    fi ;;
esac
# A long call is read by its ends: each match below is quadratic over it in
# bash, and a 280 KB Bash command took 78 s against the 5 s timeout (2026-11-09).
# ponytail: past 16 KB only the first and last 8 KB are read. A call that long
# is a heredoc's body with its commands around it; read the middle with awk if
# a gotcha named there is missed.
if [ ${#call} -gt 16384 ]; then
  # awk, not cut -b1-8192: OpenBSD's cut takes no list past 2048 (2026-11-10).
  call="$(printf '%s' "$call" | LC_ALL=C awk '{ print substr($0, 1, 8192) }') \
$(printf '%s' "$call" | LC_ALL=C tail -c 8192)"
fi
# The call less its symbion commands and their heredoc bodies: a write about a
# file, or a read of its entries, is no use of it, and spent the line before
# the first use (2026-11-05). A symbion command's word is `symbion` or a path
# to it, first in its segment.
# A variable set to symbion, or a path to it, is symbion where it is called:
# `S="symbion --dir D"; $S add` wrote about a file and spent its gotcha
# (2026-11-06).
# ponytail: an env prefix (`X=1 symbion`) hides the command, which still fires.
# One awk per `symbion `, since a scan in sh is quadratic in bash: a
# `##*[;&|(]` cut took 49 s on 8 KB of lines (2026-11-09). It prints the
# command to its first `;`, `&`, `|`, `)` or newline outside shell quotes and
# comments, else to the end of the call: `--body 'first; then src/x.py'` fired
# on src/x.py (audit, 2026-11-08). It prints nothing for no command: a word
# before it (`grep "symbion x"`), or a comment around it. Inside a quoted
# string (`ssh h 'cd x; symbion …'`) it is another shell's command, which that
# quote also ends, unless a `$(` opens it (`"$(symbion …)"`). The text before
# it goes in the environment, since awk -v expands backslashes: SYMBION_WORD
# since the last cut, SYMBION_PRE all of it, whose quotes (heredoc bodies
# aside) set the state. In this JSON text `\"` is a shell `"`, `\\` a shell
# backslash, `\n` a newline, and a bare `"` a JSON string's end. No apostrophe
# may go in the program.
segment='function body(t, j, n,    h, k, L) {
  if (substr(t, j, 1) == "-") j++
  while (substr(t, j, 1) == " ") j++
  if (substr(t, j, 1) == "\047") j++
  else if (substr(t, j, 2) == "\\\"") j += 2
  h = ""
  while (substr(t, j, 1) ~ /[A-Za-z0-9_]/) { h = h substr(t, j, 1); j++ }
  if (h == "") return j
  L = length(h)
  k = index(substr(t, j), "\\n"); if (k == 0) return n + 1
  j += k + 1
  while (1) {
    if (substr(t, j, L) == h && (j + L > n || substr(t, j + L, 2) == "\\n" || substr(t, j + L, 1) == "\"")) return j + L
    k = index(substr(t, j), "\\n"); if (k == 0) return n + 1
    j += k + 1
  }
}
function scan(t, cut,    n, i, c, e, b, d) {
  n = length(t); i = 1; d = 0
  while (i <= n) {
    c = substr(t, i, 1)
    if (c == "\"") { if (cut) return n + 1; Q = ""; i++; continue }
    if (c == "\\") {
      e = substr(t, i + 1, 1)
      if (Q == "#") { if (e == "n") { if (cut) return i; Q = "" } i += 2; continue }
      if (Q != "\047" && e == "\\") { i += 2; i += (substr(t, i, 1) == "\\") ? 2 : 1; continue }
      if (Q == "" && e == "n" && cut) return i
      if (cut && Q == "" && e == "\"" && O == "\"") return i
      if (Q != "\047" && e == "\"") Q = (Q == "") ? "\"" : ""
      i += 2; continue
    }
    if (Q == "\047") { if (c == "\047") Q = "" }
    else if (Q == "") {
      b = substr(t, i - 1, 1)
      if (cut && c == "\047" && O == "\047") return i
      if (!cut && c == "<" && substr(t, i + 1, 1) == "<" && substr(t, i + 2, 1) != "<") { i = body(t, i + 2, n); continue }
      if (c == "\047") Q = "\047"
      else if (c == "#" && (i == 1 || b == " " || b == "(" || b == "\"" || substr(t, i - 2, 2) == "\\n" || substr(t, i - 2, 2) == "\\t")) Q = "#"
      else if (c == "(" && b == "$") d++
      else if (c == ")" && d > 0) d--
      else if (cut && (c == ";" || c == "|" || c == ")")) return i
      else if (cut && c == "&" && b != ">" && substr(t, i + 1, 1) != ">") return i
    }
    i++
  }
  return n + 1
}
{ s = s (NR > 1 ? "\n" : "") $0 }
END {
  p = ENVIRON["SYMBION_WORD"]
  if (p ~ /\\"$/) exit
  w = p; sub(/.*[;&|("]/, "", w); sub(/.*\\n/, "", w); sub(/^ +/, "", w)
  if (w ~ / / || (w != "" && w !~ /\/$/)) exit
  Q = ""; O = ""; scan(ENVIRON["SYMBION_PRE"], 0)
  if (Q == "#") exit
  O = Q; Q = ""
  if (ENVIRON["SYMBION_PRE"] ~ /[$][(] *$/) O = ""
  printf "%s", substr(s, 1, scan(s, 1) - 1)
}'
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
    seg=$(printf '%s\n' "$rest" | SYMBION_PRE=$out$pre SYMBION_WORD=$pre LC_ALL=C awk "$segment")
    case "$seg" in '') out=$out${pre}symbion; continue ;; esac     # no command here
    rest=${rest#"$seg"}
    case "$seg" in
      *'<<'*)                                   # its body runs to the delimiter's line
        d=${seg#*<<}; d=${d#-}; d=${d#"${d%%[! ]*}"}
        case "$d" in
          \'*) d=${d#\'}; d=${d%%\'*} ;;
          \\\"*) d=${d#\\\"}; d=${d%%\\\"*} ;;
          *) d=${d%%[!A-Za-z0-9_]*} ;;
        esac
        [ -n "$d" ] && case "$rest" in *"\\n$d"*) rest=${rest#*"\\n$d"} ;; esac ;;
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
# At an edit, a row this reader already got in full comes back as its id. The
# answer lands after the edit it was for, so a full reprint there steers
# nothing (the user's call, 2026-11-09).
shown=
[ "$gate" = "$mark.seen" ] || shown=$(cat "$mark.seen" 2>/dev/null)
out= tails= n=0
while IFS="$tab" read -r id name text root; do
  names "$call" "$name" "$root" || continue
  case "$nl$seen$nl" in *"$nl$id$nl"*) continue ;; esac
  seen="$seen$nl$id"
  [ "$gate" = "$mark.seen" ] || printf '%s\n' "$id" >> "$gate"
  case "$nl$shown$nl" in
    *"$nl$id$nl"*)
      tails="$tails ${id#"${id%??????????}"}" n=$((n + 1))
      # `symbion`, or `symbion --dir D`, from the row's own retire hint.
      sym=${text%' supersede '*}; sym=${sym##*\`} ;;
    *)
      printf '%s\n' "$id" >> "$mark.seen"
      out="$out${out:+\\n}$text" ;;
  esac
done < "$list"
if [ "$n" -gt 0 ]; then
  if [ "$n" = 1 ]; then w="gotcha${tails} was" it=it; else w="gotchas${tails} were" it=them; fi
  out="$out${out:+\\n}symbion: this file's $w shown in full earlier in this session (\`$sym show$tails\` prints $it again)."
fi
[ -n "$out" ] && printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' "$event" "$out"
exit 0
