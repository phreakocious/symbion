#!/usr/bin/env bash
# SessionStart: surface the symbion summary, and never fail a session doing it.
# An absent store or a broken config is NAMED on stdout, never fatal. An uninstalled symbion
# prints nothing too -- unless this project's store already exists, where
# silence would hide exactly the failure this hook is meant to surface.
# PATH may be empty here, so the store check uses bash builtins only.
#
# `root` is resolved OUTSIDE the branch on purpose. It used to be assigned
# inside the not-installed branch, so the installed path never saw it and ran
# `symbion summary` against the cwd instead -- the two halves then disagreed
# about which project this is. Measured 2026-09-05: invoked with cwd in one
# symbion project and CLAUDE_PROJECT_DIR naming another, the hook checked one
# store for existence and printed the OTHER one's summary. A wrong answer that
# reads as a right one; nothing in the output says which project it is.
root="${CLAUDE_PROJECT_DIR:-$PWD}"
# Same precedence as config.store_dir: SYMBION_DIR, then the `.symbion`
# pointer beside the project, then the sibling the name implies. `read -r`
# with the default IFS already trims, and `|| [ -n "$line" ]` catches a file
# with no trailing newline. A blank pointer must fall through, never resolve
# to $root itself.
store="$SYMBION_DIR"
if [ -z "$store" ] && [ -r "$root/.symbion" ]; then
  while read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    case "$line" in
      "~/"*) store="$HOME/${line#"~/"}" ;;
      /*)    store="$line" ;;
      *)     store="$root/$line" ;;
    esac
    break
  done < "$root/.symbion"
fi
[ -n "$store" ] || store="$root/../${root##*/}-notes"
# User-level since 2026-09-24 (registered in ~/.claude/settings.json), so
# this runs in EVERY project. A store, a `.symbion` pointer or SYMBION_DIR
# marks a project that adopted symbion; with none of them it never did and
# hears nothing. A pointer or SYMBION_DIR naming a store that is not there
# still reaches `summary`, which names the absence.
if [ ! -e "$store/notes.jsonl" ] && [ ! -r "$root/.symbion" ] && [ -z "$SYMBION_DIR" ]; then
  exit 0
fi
if ! command -v symbion >/dev/null 2>&1; then
  [ -e "$store/notes.jsonl" ] && echo "symbion: store exists at $store but 'symbion' is not on PATH -- see Install in symbion's own README"
  exit 0
fi
cd "$root" 2>/dev/null || true   # a failed cd falls back to cwd; the hook never fails a session
# summary only. `symbion schema` used to print here too -- ~14 lines of kinds
# table and catalog commands that change only when symbion.toml does, i.e.
# near-never, and SKILL.md already embeds it via `!`symbion schema``, so an
# agent with the skill loaded was paying for it twice. An agent about to write
# can run `symbion schema` then; one that never writes paid every session.
# What a session CANNOT re-derive without knowing to ask is the open heads,
# which summary now carries (capped, one line each).
# stderr is merged into stdout, not discarded: a SessionStart hook's stdout is
# what reaches the agent, and `2>/dev/null` made a store whose symbion.toml
# carries a broken [kinds] table start the session exactly like a project that
# never adopted symbion (measured 2026-09-22). Exit 0 still holds either way.
symbion summary 2>&1 || true
exit 0
