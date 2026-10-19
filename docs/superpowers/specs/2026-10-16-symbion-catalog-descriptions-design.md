# Symbion catalog descriptions: a second datum in the GUI's pickers

**Date:** 2026-10-16
**Status:** The user set the scope on 2026-10-15 and made two choices on 2026-10-16,
in conversation: the newest 30 commits in the commit picker, and a filter that matches
descriptions. Built 2026-10-16. Both menus cap their items at 36rem: a long description
stretched a menu to the window's edge.
**Relates to:** the GUI design (2026-09-08); the resolvers design (2026-09-10), for the
catalog protocol; the repos design (2026-10-13), for commits over several repos.

## The need

The user, 2026-10-15: a picker's names need a second datum. The commit picker lists
bare shas. Descriptions show in the GUI's pickers only. A name still resolves on the
name.

Today the target name field offers a catalog's names. For a type without a catalog, it
offers the names that rows use. For `commit`, that is every sha a row names, 40
characters each, and nothing more. A catalog of serial numbers or hosts is as opaque.

## The catalog line

A catalog line may carry a description after a tab: `NAME<TAB>DESCRIPTION`.

```toml
[catalogs]
host = "cut -f1,3 inventory.tsv"     # name, then a description
```

- The name is the part before the first tab, verbatim, as a whole line is today.
- The description is the rest, with whitespace stripped at both ends.
- A line without a tab is a name with no description.
- A line whose name is empty or only spaces (a line that starts with a tab, say) is
  skipped, as a blank line is.
- When a name repeats, its first description holds.

Only the GUI's pickers read a description. Every other reader gets the names only:

- resolution (`canonical`, `pool`) and a resolver's stdin;
- `arc seed`, `arc reconcile`, a dropped file's lookup and the repos' file lookups;
- the CLI, which prints no description.

The `[renames]` command already prints `OLD<TAB>NEW`. It is not a catalog, and it does
not change.

## Compatibility

A catalog that prints a tab today changes its names: the part after the tab leaves the
name. A row on such a name then reads as one the catalog no longer lists.

- No catalog in the user's stores prints a tab. Each configured command was run once,
  2026-10-16.
- `git ls-files` C-quotes a tab in a path, also with `core.quotePath` off. A `file`
  catalog never prints one.
- The release notes name the change.

## Commits

`commit` has no catalog: a name resolves through git. Its picker offers:

1. the newest 30 commits on HEAD, newest first;
2. then each commit a row names, newest row first.

Each commit shows once, as its 7-character sha, with its subject as the description. A
pick writes the short sha. The write peels it to the full id, as it peels a typed one. A
stored name that git does not know (rebased away, or typed) shows as stored, with no
description.

In a store over several repos, the newest 30 come from all its repos together, by
commit time. `gitref.subjects` already asks each repo.

`gitref.subjects` gets every stored commit name here. On Windows a command line holds
at most 32,767 characters: 790 shas ran and 800 raised `WinError 206` (2026-10-16). The
raise is an exception, not a failed exit. `--stdin` is no way out: git drops
`--ignore-missing` for revisions it reads from stdin, and one unknown sha fails the
batch. So `subjects` runs git once per 500 shas.

## The pickers

**The target name field** (in the composer and the edit dialog). Each menu item shows
the name, and under it the description, on one line, cut with an ellipsis. Typing
matches any part of the name or of the description, in any case, and a pick writes the
name (the user, 2026-10-16). Text typed and not picked is taken as a name, as now.

**The "also on" field.** It offers what it offers today, the entry's refs and the
objects that rows name, and also the newest 30 commits. A commit's menu item shows its
subject under the ref. Typing matches either. A chip shows the ref alone.

This field shows no catalog's descriptions. It offers objects of every type, and to
describe them it would run every catalog type at each dialog open. Measured 2026-10-16:
a `git ls-files` catalog ran in 11 to 84 ms, one that runs Python or jq in 113 to
123 ms, and a store-derived one (`symbion list … | jq`) in about 244 ms. A store with
several such types would open each dialog about 300 ms later, for catalogs that print
no description today. A `ponytail:` comment marks this: describe them off the event
loop when a store wants them there.

## Cost

- The target name field runs its type's catalog once, as today.
- A commit list costs a `git log -n 30` (11 ms) and one `subjects` call over the
  stored commits (48 ms for a few hundred shas). The "also on" field pays it at each dialog
  open, and the target name field pays it again on `commit`.
- The composer is built at its first open and the edit dialog at each open, so no
  page load pays it.

## Where it lives

- `catalog.py`: one parse of a catalog's lines into names and descriptions. `names`
  returns the names; `described` returns each name's description.
- `gitref.py`: `subjects` in chunks of 500, and `recent(cfg, n)`: the newest `n`
  commits with their subjects, over every repo of a store over several.
- `gui/notes.py`: `target_names` returns each name with its description, and the
  commit list. `refs_field` puts a commit's subject in its label. `Suggest` takes the
  descriptions.
- `gui/suggest.js`: the description line, and the filter on it.
- `docs/reference.md` and the skill's `catalogs.md`: the line format.

## Tests

- Catalog: a tab line splits. `names` returns only names and `described` the
  descriptions. A leading tab skips its line. A repeated name keeps its first
  description. A resolver's stdin, and a description typed as a name, see names only.
- `subjects` with chunks of 2 finds each of 5 shas, and still drops an unknown one.
- `recent`: the newest `n`, newest first; over two repos, merged by commit time.
- The GUI, through NiceGUI's `user` fixture: the commit list's order, short shas and
  descriptions, and the "also on" labels. In Chrome: the description line shows,
  typing a word of a subject finds its commit, and a chip shows the ref alone.
- Each guard is broken once, to see its test fail.

## Rejected

- **A `[descriptions]` table**, a second command per type: two runs per picker, and the
  two lists can disagree.
- **JSON lines**: a name may start with `{`, and a shell one-liner gets harder to write.
- **Descriptions in the CLI**: the user's choice, GUI pickers only.
- **A description that resolves at a write**: a name resolves on the name (the user).

## Limits

- The "also on" field shows commit subjects, not catalog descriptions.
- 30 commits is a guess. It is one constant.
