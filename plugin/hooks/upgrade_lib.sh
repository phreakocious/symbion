# Sourced by session_start.sh and gotchas.sh, as `. "$here/upgrade_lib.sh"` with
# here=${0%/*}, or ${0%\*} when $0 has only `\` (Pi on Windows), or `.`.
# POSIX sh, builtins only where a caller runs per tool call: PATH may be empty there.

# Sets pv to the plugin's version, or to nothing when plugin.json or its key is
# missing or the value holds a character outside [0-9A-Za-z.+-]: pv names files
# in the state dir, so it must never carry a path or a command. The file is joined
# into one string first, so a formatter that squeezes it onto one line still reads.
symbion_plugin_version() {
  pv= _j=
  { while IFS= read -r _l || [ -n "$_l" ]; do _j="$_j$_l "; done \
      < "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json"; } 2>/dev/null
  case $_j in *'"version"'*) _j=${_j#*'"version"'} ;; *) return 0 ;; esac
  _j=${_j#*:}
  _j=${_j#"${_j%%[![:space:]]*}"}
  case $_j in '"'*) _j=${_j#\"}; _j=${_j%%\"*} ;; *) return 0 ;; esac
  case $_j in ''|*[!0-9A-Za-z.+-]*) ;; *) pv=$_j ;; esac
  return 0
}

# Returns 0 when ROOT's project adopted symbion, by the package script's own
# marks (SYMBION_DIR, a .symbion with a nonblank line or that is no readable
# file, the sibling store). A blank pointer falls through, as there; a test for
# a nonempty one sent such a project to setup (audit, 2026-11-12).
symbion_adopted() {
  [ -n "$SYMBION_DIR" ] && return 0
  root=$1
  nl='
'
  main=$root
  # A main worktree's root (a .git dir) is its own main and top: no git to ask.
  # While an upgrade result waits this runs on every tool call (audit, 2026-11-12).
  if ! [ -d "$root/.git" ]; then
    main=$(git -C "$root" worktree list --porcelain 2>/dev/null) && main="${main%%"$nl"*}" && main="${main#worktree }"
    [ -n "$main" ] || main="$root"
  fi
  p="$main/.symbion"
  if ! [ -e "$p" ] && ! [ -L "$p" ] && ! [ -d "$root/.git" ]; then
    top=$(git -C "$root" rev-parse --show-toplevel 2>/dev/null) && p="$top/.symbion"
  fi
  if [ -e "$p" ] || [ -L "$p" ]; then
    { [ -f "$p" ] && [ -r "$p" ]; } || return 0
    { while read -r _l || [ -n "$_l" ]; do
        case $_l in *[![:space:]]*) return 0 ;; esac
      done < "$p"; } 2>/dev/null
  fi
  # Without git on PATH, main is CLAUDE_PROJECT_DIR as given: on Windows, with `\`.
  name=${main##*/}; name=${name##*\\}
  [ -e "$main/../$name-notes/notes.jsonl" ]
}

# stdin to stdout: one string for a JSON value (agy reads only JSON).
symbion_json_text() {
  LC_ALL=C tr '\t' ' ' | LC_ALL=C tr -d '\000-\010\013-\037' \
    | LC_ALL=C sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s%s", (NR > 1 ? "\\n" : ""), $0}'
}

# Launches upgrade.sh for the plugin's version, detached, unless ok-PV says it is
# done. WHEN is start (a session start) or tool (a tool call). Inert outside Claude
# Code: agy, Pi and a hand run set neither env var. With its marker present this
# runs builtins only, as gotchas.sh calls it on every tool call. On the fresh
# path the shell's exclusive create of the file hold-PV (set -C) is the lock: of
# two hooks racing, one wins. mkdir was none where it is uutils' before 0.11
# (Ubuntu 26.04's): both of two racing creates exited 0 (a CI run, 2026-11-13).
# A hold older than an hour is mostly a failed or waiting attempt's throttle,
# which upgrade.sh keeps on purpose; only a session start retries it, so tool
# calls never do. A worker still hung past an hour gets a second beside it: uv
# and pip time out.
# Needs `here`, the hooks dir. Never reads stdin: gotchas.sh hands it on.
symbion_upgrade_check() {
  [ -n "$CLAUDE_PLUGIN_DATA" ] && [ -n "$CLAUDE_PLUGIN_ROOT" ] || return 0
  symbion_plugin_version
  [ -n "$pv" ] || return 0
  _up=$CLAUDE_PLUGIN_DATA/upgrade
  [ -e "$_up/ok-$pv" ] && return 0
  if [ -e "$_up/hold-$pv" ]; then
    [ "$1" = start ] || return 0
    # shortcut: two session starts in the gap between this age test and the rm,
    # over a hold past an hour, launch two workers (B's rm removes the fresh hold
    # A just made); uv locks its tool dir, pipx does not. Upgrade: make the claim
    # atomic by moving a pre-built hold into place.
    [ -n "$(find "$_up/hold-$pv" -prune -mmin +60 2>/dev/null)" ] || return 0
    rm -f "$_up/hold-$pv" 2>/dev/null || return 0
  fi
  mkdir -p "$_up" 2>/dev/null && (set -C; : > "$_up/hold-$pv") 2>/dev/null || return 0
  # All three streams go to /dev/null first, by exec, so the host never waits on the
  # hook's pipes: on a background compound OpenBSD's sh kept a copy of each pipe it
  # redirected, and the hook waited out the worker (an OpenBSD run, 2026-11-13).
  # A session of its own: at the hook's timeout Claude Code kills the hook's whole
  # process group, and a worker in it died mid-upgrade (live run, 2026-11-12).
  # shortcut: with neither setsid (Linux) nor perl (macOS, the BSDs, Git for Windows)
  # the worker stays in the hook's group; add a launcher when such a host shows up.
  (exec </dev/null >/dev/null 2>&1
   trap '' HUP
   if command -v setsid >/dev/null 2>&1; then setsid sh "$here/upgrade.sh" "$pv"
   elif command -v perl >/dev/null 2>&1; then
     perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' sh "$here/upgrade.sh" "$pv"
   else sh "$here/upgrade.sh" "$pv"; fi &)
  return 0
}

# Hands the worker's result to the agent, once. Called by gotchas.sh with `result`
# present and the package's skill dir ($1, maybe empty) as read from the cache;
# it reads stdin, prints, and returns, and the caller exits. SessionStart delivers
# nothing by design; the first tool call delivers, whether the result came from
# this session's worker or an earlier one's. The package script runs first, so a
# kill at the hook's timeout cannot orphan a claimed result. The result waits for
# a project that adopted symbion (a session elsewhere has no use for a note about
# it) and for the main agent: a call with an agent_id before tool_input is a
# subagent's. mv claims it, so of two hooks racing only one delivers. The package
# script's own answer, when it has one, gets the text first in its
# additionalContext; an answer of any other shape is passed on as is and the
# result goes back, to try again. Writes nothing to stderr.
symbion_upgrade_deliver() {
  _up=$CLAUDE_PLUGIN_DATA/upgrade
  _in=$(cat 2>/dev/null)
  _out=
  [ -f "$1/gotchas.sh" ] && _out=$(printf '%s\n' "$_in" | sh "$1/gotchas.sh" 2>/dev/null)
  _h=${_in%%'"tool_input"'*}
  _sub=
  case $_h in *'"agent_id"'*) _sub=1 ;; esac
  if [ -n "$_sub" ] || ! symbion_adopted "${CLAUDE_PROJECT_DIR:-$PWD}" \
     || ! mv "$_up/result" "$_up/result.$$" 2>/dev/null; then
    [ -n "$_out" ] && printf '%s\n' "$_out"
    return 0
  fi
  _t=
  { IFS= read -r _t < "$_up/result.$$" || [ -n "$_t" ]; } 2>/dev/null
  _k='"additionalContext":"'
  case $_out in
    '') case $_h in *PostToolUseFailure*) _e=PostToolUseFailure ;; *) _e=PostToolUse ;; esac
        printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}\n' "$_e" "$_t"
        rm -f "$_up/result.$$" 2>/dev/null ;;
    *"$_k"*) printf '%s\n' "${_out%%"$_k"*}$_k$_t\\n${_out#*"$_k"}"
             rm -f "$_up/result.$$" 2>/dev/null ;;
    *) printf '%s\n' "$_out"
       mv "$_up/result.$$" "$_up/result" 2>/dev/null ;;
  esac
  return 0
}
