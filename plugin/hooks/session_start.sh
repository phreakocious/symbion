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
# also names in SYMBION_PROJECT_DIR (spec 2026-11-02). Hermes's plugin runs it
# as `sh session_start.sh hermes` the same way (spec 2026-11-11).
# Pi on Windows names this file through Node's path.join, with `\` alone: cut at
# the last `/`, else at the last `\`, else it is the cwd's.
here=.
case $0 in */*) here=${0%/*} ;; *\\*) here=${0%\\*} ;; esac
# The no-CLI path and the agy plain-text path call the library's symbion_adopted and
# symbion_json_text, so a missing library breaks them; the hook files ship together,
# and only a hand-deleted file hits this.
if [ -r "$here/upgrade_lib.sh" ]; then
  . "$here/upgrade_lib.sh"
  symbion_upgrade_check start
fi
# shortcut: a sed match, not a JSON parse: a project path holding `"` or `\`
# reads as no project and the hook says nothing; parse it in Python if one does.
agy=
if [ "$1" = antigravity ]; then
  agy=$(cat)
  SYMBION_PROJECT_DIR=$(printf '%s' "$agy" | tr -d '\n' | sed -n \
    's/.*"workspacePaths"[[:space:]]*:[[:space:]]*\[[[:space:]]*"\([^"\\]*\)".*/\1/p')
  [ -n "$SYMBION_PROJECT_DIR" ] || exit 0
  export SYMBION_PROJECT_DIR SYMBION_HOST=antigravity
elif [ "$1" = pi ] || [ "$1" = hermes ]; then
  export SYMBION_HOST="$1"
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
      *) out=$(printf '%s\n' "$out" | symbion_json_text)
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
if symbion_adopted "$root"; then
  if [ "$1" = antigravity ]; then   # agy reads only JSON, and has no /symbion:setup
    echo '{"injectSteps":[{"systemMessage":{"systemMessage":"symbion: this project has a symbion store, but the symbion CLI is not installed or does not run: see Install at https://github.com/phreakocious/symbion"}}]}'
  elif [ "$1" = pi ]; then   # Pi's prompt names take no namespace; Hermes's skills do
    echo "symbion: this project has a symbion store, but the symbion CLI is not installed or does not run: run /symbion-setup."
  elif [ "$1" = hermes ] && ! command -v symbion >/dev/null 2>&1; then
    # This runs in Hermes's gateway, on the PATH it started with; Hermes's terminal reads a
    # login shell's: this said "not installed" while the terminal's `symbion -V` answered
    # (offline check, 2026-11-13), so setup would find the CLI and leave the gateway as it
    # was. PATH goes as an argument: a `\` in it.
    printf 'symbion: this project has a symbion store, but the Hermes gateway, where symbion'"'"'s hooks run, finds no symbion on its PATH (%s). If `symbion -V` works in your terminal, tell the user to run, from a shell where it works, `hermes gateway install --force` if the gateway runs as a service, else `hermes gateway stop`, which the next chat restarts. Otherwise run /symbion:setup.\n' "$PATH"
  else
    echo "symbion: this project has a symbion store, but the symbion CLI is not installed or does not run: run /symbion:setup."
  fi
fi
exit 0
