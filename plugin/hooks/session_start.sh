#!/bin/sh
# The plugin's SessionStart hook. It runs the installed package's own
# session_start.sh, so the hook always matches the CLI it runs: the plugin
# carries no copy (spec 2026-10-30). Each session start asks `symbion skill
# --path` (about 0.2 s), so a CLI that no longer runs counts as missing, and the
# hook follows the CLI on PATH, not an install it cached before: a pipx venv
# whose Python went away keeps its site-packages (the final review, 2026-10-30).
# The answer is cached for gotchas.sh beside this file, which must not start
# Python on every tool call. POSIX sh, as the package's scripts are; PATH may be
# empty here, so the cache is read with builtins.
# agy runs it as `sh hooks/session_start.sh antigravity` in the plugin's own
# dir, and names the project only in its stdin JSON, as workspacePaths[0]
# (agy 1.4.12, 2026-10-30). That JSON is replayed to the package script.
# SYMBION_HOST names the host to the package's hook, which answers in its JSON:
# a Claude Code started in agy's terminal can carry agy's marker.
# Pi's extension runs it as `sh session_start.sh pi`, in the project, which it
# also names in SYMBION_PROJECT_DIR (spec 2026-11-02).
# shortcut: a sed match, not a JSON parse: a project path holding `"` or `\`
# reads as no project and the hook says nothing; parse it in Python if one does.
agy=
if [ "$1" = antigravity ]; then
  agy=$(cat)
  SYMBION_PROJECT_DIR=$(printf '%s' "$agy" | tr -d '\n' | sed -n \
    's/.*"workspacePaths"[[:space:]]*:[[:space:]]*\[[[:space:]]*"\([^"\\]*\)".*/\1/p')
  [ -n "$SYMBION_PROJECT_DIR" ] || exit 0
  export SYMBION_PROJECT_DIR SYMBION_HOST=antigravity
elif [ "$1" = pi ]; then
  export SYMBION_HOST=pi
else
  export SYMBION_HOST=claude
fi
cache="${XDG_CACHE_HOME:-$HOME/.cache}/symbion/skill_dir"
d=
if command -v symbion >/dev/null 2>&1; then
  d=$(symbion skill --path 2>/dev/null) || d=
fi
# Python on Windows writes CRLF to a pipe. Git's sh drops the CR in `$(…)`, and
# dash keeps it: the path then named no dir, and a working CLI read as missing
# (audit 2026-11-08; dash measured 2026-11-10).
d=${d%"$(printf '\r')"}
if [ -f "$d/session_start.sh" ]; then
  cached=
  { read -r cached < "$cache"; } 2>/dev/null
  # Through a temp name: two sessions starting at once never leave half a path.
  [ "$cached" = "$d" ] \
    || { mkdir -p "${cache%/*}" && printf '%s\n' "$d" > "$cache.$$" && mv -f "$cache.$$" "$cache"; }
  if [ "$1" = antigravity ]; then
    out=$(printf '%s\n' "$agy" | sh "$d/session_start.sh")
    # agy reads only JSON. An error (a broken symbion.toml, a traceback) is
    # plain text, so it goes into agy's JSON as one string (audit, 2026-11-08).
    case "$out" in
      ''|'{'*) [ -z "$out" ] || printf '%s\n' "$out" ;;
      *) out=$(printf '%s\n' "$out" | LC_ALL=C tr '\t' ' ' | LC_ALL=C tr -d '\000-\010\013-\037' \
           | LC_ALL=C sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s%s", (NR > 1 ? "\\n" : ""), $0}')
         printf '{"injectSteps":[{"systemMessage":{"systemMessage":"%s"}}]}\n' "$out" ;;
    esac
    exit 0
  fi
  exec sh "$d/session_start.sh"
fi
# No symbion that runs. Speak only where the project adopted symbion, by the
# package script's own marks (SYMBION_DIR, a nonblank .symbion, the sibling
# store), and send it to setup; anywhere else, say nothing.
root="${SYMBION_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$PWD}}"
nl='
'
main=$(git -C "$root" worktree list --porcelain 2>/dev/null) && main="${main%%"$nl"*}" && main="${main#worktree }"
[ -n "$main" ] || main="$root"
p="$main/.symbion"
if ! [ -e "$p" ] && ! [ -L "$p" ]; then
  top=$(git -C "$root" rev-parse --show-toplevel 2>/dev/null) && p="$top/.symbion"
fi
# Without git on PATH, main is CLAUDE_PROJECT_DIR as given: on Windows, with `\`.
name=${main##*/}; name=${name##*\\}
if [ -n "$SYMBION_DIR" ] || [ -s "$p" ] || [ -L "$p" ] || [ -e "$main/../$name-notes/notes.jsonl" ]; then
  if [ "$1" = antigravity ]; then   # agy reads only JSON, and has no /symbion:setup
    echo '{"injectSteps":[{"systemMessage":{"systemMessage":"symbion: this project has a symbion store, but the symbion CLI is not installed or does not run: see Install at https://github.com/phreakocious/symbion"}}]}'
  elif [ "$1" = pi ]; then   # Pi's prompt names take no namespace
    echo "symbion: this project has a symbion store, but the symbion CLI is not installed or does not run: run /symbion-setup."
  else
    echo "symbion: this project has a symbion store, but the symbion CLI is not installed or does not run: run /symbion:setup."
  fi
fi
exit 0
