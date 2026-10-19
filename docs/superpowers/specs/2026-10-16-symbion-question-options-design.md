# Symbion question options: a question carries its answers as data

**Date:** 2026-10-16
**Status:** Designed with the user on 2026-10-16, in conversation. A reviewer's
read settled four points the same day, each marked "the builder's call" below.
Built 2026-10-17, 79fed5f..769749d, one commit per layer; the `options:` line prints
beside `measured:` wherever a row prints, and the GUI's options row, where no
buttons draw, is one line with that separator.
**Relates to:** the kinds design (2026-09-10), for the `question` kind and its bits;
the GUI design (2026-09-08), whose rule that the GUI adds no capability the CLI lacks
this design restores; the vocabulary design (2026-09-08), for the `question` kind.

## The need

A question is a row an agent writes when only a person can pick. Two conventions
grew on its prose, one on the other, and the user found the result fragile and
robotic (2026-10-16):

1. SKILL.md's rewrite of 2026-10-14 told the agent: "a default that ships goes in its
   first 100 characters beside the ask, with how to flip it." The reason was the
   summary's clipped line, 100 characters of body. The agents obeyed to the letter.
   Every open question in this store now opens `Default: … Flip: say "…"`.
2. The GUI's one-click answers of 2026-10-16 (be6d042) parsed that prose: a **keep the
   default** button when the first 100 characters contain the word `default`, and a
   button per `say "…"` phrase a regex finds in the body. The stored answer is the
   literal `Keep the default.` or the quoted phrase, so an answer reads as robotic as
   the ask.

The second convention also broke the GUI design's rule: the GUI had a capability the
CLI lacked, since nothing but the GUI knew what a `say "…"` phrase was.

The user's shape for the card, 2026-10-16: the question in plain words, then the
options as buttons, the default first, three at most. A question that cannot be put
in three options gets no options, and the person answers in text. A text field stays
beside the buttons either way: "you never know when the options don't quite slice
it."

## The design

The user asked that options follow `measurements`, the one structured field a row
carries today, wherever the two can be alike. Each section below names where they
match and the one place they differ.

### The field

A row has one more field, `options`: a list of one to three strings, in the order
the asker gave them. The first is the default. The field is `None` when absent, as
`measurements` is.

- `store.Note` declares `options: tuple | None = None` beside `measurements`.
  `note_from_dict` reads it as a tuple; `note_to_dict` writes it as a JSON array.
  `measurements` sorts its keys at read time so two rows line up; `options` keeps its
  order, because the order is the datum: the default is first. This is the one
  difference from `measurements`.
- The undo path parses lines to plain dicts (store gotcha, 2026-10-13); it needs
  nothing from this field.
- `list --json` and `read_dict` give it as an array of strings, verbatim.

### Validity, at every write

`api.check_options`, beside `check_measurements`, refuses by name, as that one does:

- not a list, or an empty list: "options is a list of one to three answers";
- more than three: "options holds at most three answers; a question with more is
  asked in prose, and the person answers in text" (the user's rule);
- an option that is not a string, or is empty after stripping: "option N is empty";
- an option over 60 characters: "option N is over 60 characters: a button's label,
  not a paragraph" (the regex that lifted phrases capped them at 60; a label longer
  than that wraps a card);
- two options alike after stripping and casefolding: "options repeat: …";

`fields_from_row` runs the check when the row carries `options`, so `--from-json`
and the flag path are judged alike. `api.supersede` runs it on `options` when given.

The kind rule lives one layer down, in `store.check_fields`, which takes the kind's
name and runs on every write under the lock: a row of any kind but `question` that
carries options is refused, "options on a K: only a question takes answers". There,
and not in `api.check_options`, because a supersede inherits the tip's fields and
`--kind` can move a question to a task or a bug (they share bits) or park it as an
idea: judged in api on the flags alone, the moved row would carry options that `add`
refuses. The precedent is the kind-change rule already there: a move that leaves a
field where its kind cannot hold it is refused, not quietly shed. So `supersede Q
--kind task` on a question with options is refused, and `supersede Q --kind task
--option ""` moves it, cleared (the clearing idiom is below). `supersede T --kind
question --option a --option b` passes: the rule judges the row as written.
ponytail: a question is the kind named `question`, as `summary.asks` already has
it; no fourth bit in `[kinds]`. A store that renames the kind loses options until a
bit lifts the name, the same ceiling `asks` carries.

### The flags

`add --option TEXT`, repeatable, as `--measure KEY=VALUE` is. `supersede --option
TEXT`, repeatable, replaces the whole list: a measurement is merged by name because
each is its own datum; an option list is one datum, ordered, so a correction gives it
whole. `supersede` without `--option` inherits the list, and `--option ""` alone
clears it, as `--due ""` clears a due: a question whose answers no longer fit in
three goes back to prose without a re-ask. An empty option among others is still the
refusal above. `resolve` takes no `--option`: a resolve is the answer, not the ask.

- `_row_from_flags` passes `options` only when given, as it passes `measurements`.
- `supersede`'s fast path to `api.retag` runs when every other field `is None`; a
  given `--option` must join that chain, or beside `--add-tag` it is silently dropped
  (cli gotcha, 2026-10-02).
- The `--measure a=1 b=2` hint, where a second pair is left over, has no analogue:
  a stray word after `--option` is argparse's "unrecognized arguments", which names
  it.
- `symbion schema`'s line for `question` says it: `--option` for each answer, the
  default first. The `when` text in `kinds.py` and the kinds block of `help.md` carry
  the same words (kinds gotcha, 2026-10-15: the help test fails until they match).
  That line reaches a store without a `[kinds]` table only: `init` writes the
  defaults into `symbion.toml`, and `schema` prints what the toml declares, so a
  store made before this change keeps its old line, as most keep one older than
  today's. SKILL.md is the channel that reaches every store; the `when` edit is for
  the stores to come, and no runtime hint is added for the rest.

### Printing

Wherever a row prints its measurements, it prints its options, on one labelled line
under the body: `options: ship as checked | hold for a Chrome check`. (Built
2026-10-17: the line prints where `measured:` prints, which in `list`, `show`,
`context` and the terminal view is above the body, as the metadata lines are; the
summary and the GUI card print it below. Following `measured:` was the user's
direction, and "under the body" here described the card.) The separator
is ` | `, not the comma measurements use, because an option is a phrase and phrases
hold commas. The first is the default; the label does not say so. SKILL.md,
help.md and the reference say it once each, and the GUI marks the first button.

- `cli._print_note`: `    options: …`, clipped at `LIST_BODY_CHARS` in a list, whole
  under `--full`, as `measured:` is. This is the line agents parse (term gotcha,
  2026-10-02): `term.print_note` prints the same words through `labelled`, and after
  the change `list --all --full --limit 0` differs from HEAD's only on rows with
  options.
- The summary: a question prints its options line under its row in whichever block
  it appears, so the agent reading its session start sees what the GUI shows, and the
  asker's default is readable without the 100-character rule. `digest` carries
  `options`; `_labelled` prints the line after the amendment lines, so every block
  gets it. `fit` trims whole rows, so the 10,000-character cap never cuts the line
  from its row.
- `grep` in `store.filter` searches each option, as it searches each measurement as
  `name=value`: an answer's words are what a reader greps for.
- `context --target` prints the row as `list` does, and gets the line with it.

### The GUI

`_answer_row` draws one button per option, in order, labelled with the option's
words. The first carries the check icon that **keep the default** carries today; the
others carry none. The text field stays after them, as now. A pick appends the
option's words below the question, as the person, through `api.supersede`, exactly
as a typed answer does; the question stays open for its asker to act on and resolve,
as SKILL.md has it. The `_SAY` regex and the `default` test on the first 100
characters go.

- A question without options draws the field alone. Every open question written
  under the old convention is one of these: its prose stays, and there is no
  migration. The `Default: … Flip:` prefix reads as the question's own words, and the
  person answers in text.
- Where the answer row is not drawn (the asker's own question, one the person has
  answered, a resolved one), the card shows the options as a quiet meta row, as it
  shows measurements: the answer below then reads against its alternatives, and the
  card shows what `show` prints. Where the buttons draw, they are the options, and no
  row doubles them.
- The peek's version diff names a change to the options between two versions, as it
  names a measurement's; untaught, a corrected option list would read as no change.
- The row renders the same way on the Summary, the lists and in the peek, because
  all three go through `render_note`.
- The peek's keys: **a** focuses the field today. It keeps doing that; the buttons
  are clicked.
- The edit dialog does not edit options. An asker is an agent at the CLI; a person
  who wants the options changed says so in the field. A later decision may add it.
- The add form does not offer options. Same reason.

### The agent surface

SKILL.md's rule changes. Out: "a default that ships goes in its first 100 characters
beside the ask, with how to flip it." In, at the same place and about the same
length (SKILL.md stays under 16 KB; a line goes in only when another comes out):

> A question whose answers fit in three gives them as `--option`, the default first,
> and its body is the question in plain words. One with more, or none that fit, gives
> no options; the person answers in text. When someone answers your question below
> it, act on the answer and resolve the question.

`help.md`'s paragraph on answering a question, `guide.md`'s bullet on the GUI's
answers, and `reference.md`'s line on the `question` kind say what the buttons are
made of now: the options the asker gave. The release notes name the change: a
question written with `--option` by this release prints an `options:` line that
0.8.0's `show` does not know. 0.8.0's `note_from_dict` takes the keys it knows and
drops the rest, so it reads such a row without its options; the cut's check that
0.8.0 reads a store this release wrote covers it.

### Tests

Each guard gets one test that fails when the guard goes, in the file that holds
its neighbours' tests:

- store: a round trip keeps three options in order; a row without the field reads
  `None`.
- api: each refusal above, by its message; a valid list on a question is accepted;
  `--from-json` with `options` is judged.
- store: a kind change off `question` on a row with options is refused; with
  `--option ""` in the same write it moves, cleared; a move onto `question` with
  options passes.
- cli: `add --option` three times prints an `options:` line in `show` and `list`,
  clipped in the list; a fourth exits with the message; `supersede --option` replaces
  the list, `--option ""` clears it, and beside `--add-tag` neither is dropped;
  `grep` finds an option's words.
- summary: a waiting question's options line prints under its row, and `fit` keeps
  it with the row.
- schema and help: the `question` line names `--option`, and the help's kinds block
  matches.
- gui: a question with options draws a button per option, in order, with the first
  marked, and the field; a question without draws the field alone; a pick appends
  the option's words as the person; the asker's own question shows the meta row and
  no buttons; the peek's diff names an options change; the regex tests of be6d042
  and bb2e164 are replaced, not kept.

### Not in this design

- Options on any kind but `question`.
- A structured answer: the pick is the option's words, appended below the question,
  which is how a typed answer and a CLI answer already read. A field that records
  which option was picked, and a GUI that reads it, are a later decision if a
  reader ever needs the pick as data.
- A CLI verb to answer with an option by number. `supersede ID --append --body
  "<the option's words>"` is the answer, as it is today.
- A migration of the open questions written under the old convention.
- A survey of existing stores: this changes no state and no label, so no
  existing row reads differently.

## Left to the builder

- Where in `render_note` the buttons sit relative to the body, as long as it is the
  order the user gave: question, then buttons, then the field.
- The exact wording of each refusal, within the shape above.
- How `digest` passes options to `_labelled`.
