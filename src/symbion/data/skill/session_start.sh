#!/usr/bin/env bash
# SessionStart: surface the symbion summary, and never fail a session doing it.
# An absent store or a broken config is NAMED on stdout, never fatal. An uninstalled symbion
# prints nothing too -- unless this project has adopted symbion, where
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
root="${SYMBION_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$PWD}}"
# The store belongs to the MAIN worktree, as in config.project_root(): a
# linked worktree shares it. Deriving it from $root made a session in a linked
# worktree check `<linked-name>-notes` and exit silent (found 2026-10-02).
# With no git on PATH, or no repo at $root, it stays $root. `summary` still
# runs in $root, where HEAD and check state are this worktree's.
main=$(git -C "$root" worktree list --porcelain 2>/dev/null) && main="${main%%$'\n'*}" && main="${main#worktree }"
[ -n "$main" ] || main="$root"
# Same precedence as config.store_dir: SYMBION_DIR, then the `.symbion`
# pointer beside the project, then the sibling the name implies. `read -r`
# with the default IFS already trims, and `|| [ -n "$line" ]` catches a file
# with no trailing newline. A blank pointer must fall through, never resolve
# to $main itself. A pointer that is there but not a readable file (a link to
# a directory) goes on to `summary`, which names it: `read` printed bash's
# "Is a directory" and the hook went silent (2026-10-03).
store="$SYMBION_DIR"
p="$main/.symbion"
# A linked worktree's pointer counts while main has none (a branch adopting
# symbion), and its path still resolves against $main, as in config._pointer.
if ! [ -e "$p" ] && ! [ -L "$p" ]; then
  top=$(git -C "$root" rev-parse --show-toplevel 2>/dev/null) && p="$top/.symbion"
fi
if [ -z "$store" ] && { [ -e "$p" ] || [ -L "$p" ]; } && ! { [ -f "$p" ] && [ -r "$p" ]; }; then
  store="$p"
elif [ -z "$store" ] && [ -r "$p" ]; then
  while read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    case "$line" in
      "~/"*) store="$HOME/${line#"~/"}" ;;
      /*)    store="$line" ;;
      *)     store="$main/$line" ;;
    esac
    break
  done < "$p"
fi
# User-level since 2026-09-24 (registered in ~/.claude/settings.json), so
# this runs in EVERY project. A store, a nonblank `.symbion` pointer or SYMBION_DIR
# marks a project that adopted symbion; with none of them it never did and
# hears nothing. A pointer or SYMBION_DIR naming a store that is not there
# still reaches `summary`, which names the absence.
if [ -z "$store" ]; then
  store="$main/../${main##*/}-notes"
  [ -e "$store/notes.jsonl" ] || exit 0
fi
if ! command -v symbion >/dev/null 2>&1; then
  echo "symbion: cannot read store at $store because 'symbion' is not on PATH -- see Install at https://github.com/phreakocious/symbion"
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
