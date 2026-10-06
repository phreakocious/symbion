"""What a person sees at a terminal. `list`, `show` and `context` print a
row in columns; every other text command colours its plain lines through
painter(), and --help prints through help_formatter(). A pipe gets the
plain text, which agents, scripts and tests parse; nothing here may change
it."""
from __future__ import annotations

from datetime import datetime

from markdown_it import MarkdownIt
from rich.console import COLOR_SYSTEMS, Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.style import Style
from rich.syntax import SyntaxTheme
from rich.text import Text
from rich.theme import Theme

from . import api, store
from . import kinds as K
from . import summary as summ

# Catppuccin Mocha: pastels made for a dark background. rich maps each hex to
# the nearest colour a 256- or 16-colour terminal has, and prints none under
# NO_COLOR. The body is a step below the target, so the head line leads.
# ponytail: dark backgrounds only; a light terminal needs Catppuccin Latte,
# picked by COLORFGBG or a flag, once someone runs one.
TEXT, BODY, META, FADED = "#cdd6f4", "#a6adc8", "#7f849c", "#6c7086"
GOOD, WARN, BAD = "#a6e3a1", "#f9e2af", "#f38ba8"
KIND = {"bug": "#eba0ac", "task": "#89b4fa", "question": "#cba6f7", "idea": "#f5c2e7",
        "decision": "#fab387", "check": "#94e2d5", "note": "#a6adc8"}
OWN_KIND = "#b4befe"        # a kind this store's [kinds] table declares


def kind_hex(kind: str, spec=None) -> str:
    """A kind's colour: the palette name its [kinds] `color` picks, else its
    default's, else the one every kind a store declares shares."""
    return K.COLORS[spec.color] if spec is not None and spec.color else KIND.get(kind, OWN_KIND)
# The names summary.plain's `role` takes, so the summary, schema and arc
# lines colour a row's parts as a `list` row does.
ROLE = {"text": TEXT, "body": BODY, "meta": META, "faded": FADED,
        "good": GOOD, "warn": WARN, "bad": BAD}
# Catppuccin's twin for each ANSI hue that rich's markdown colours with by
# default: its design, in this palette.
HUE = {"yellow": WARN, "blue": "#89b4fa", "magenta": "#cba6f7", "cyan": "#89dceb"}
# A body's prose is BODY, as under a `list` row; code loses rich's black box.
MARKDOWN = Theme({
    "markdown.paragraph": BODY,
    "markdown.code": f"bold {HUE['cyan']}", "markdown.code_block": HUE["cyan"],
    "markdown.list": HUE["cyan"], "markdown.item.number": HUE["cyan"],
    "markdown.table.border": HUE["cyan"], "markdown.table.header": f"not bold {HUE['cyan']}",
    "markdown.block_quote": HUE["magenta"], "markdown.h2": f"underline {HUE['magenta']}",
    "markdown.h3": f"bold {HUE['magenta']}", "markdown.h4": f"italic {HUE['magenta']}",
    "markdown.link": HUE["blue"], "markdown.link_url": f"underline {HUE['blue']}"})


def _underscore(state, silent: bool) -> bool:
    """`_` is text: bodies name `__init__.py` and `snake_case`, and
    CommonMark bolds the `init`. `*` still makes emphasis."""
    if state.src[state.pos] != "_":
        return False
    if not silent:
        state.pending += "_"
    state.pos += 1
    return True


def body_parser(highlight=None) -> MarkdownIt:
    """The markdown a row body is read in, at a terminal and in the GUI, so
    both read one text alike (2026-10-02; the GUI's markdown2 ran a list
    under a line of prose into it). CommonMark with tables and
    strikethrough. HTML is off: rich dropped a `<pre>` named in prose, and
    the GUI rendered one. `highlight` colours a fenced block in HTML."""
    md = MarkdownIt("commonmark", {"html": False, "highlight": highlight})
    md.enable(["strikethrough", "table"])
    md.inline.ruler.before("emphasis", "underscore", _underscore)
    return md


BODY_MD = body_parser()
# --help: what you type in code's colour, prose as a row body, headings and
# the program in the hues argparse 3.14 gives them.
HELP = {"argparse.args": HUE["cyan"], "argparse.syntax": HUE["cyan"],
        "argparse.help": BODY, "argparse.text": BODY, "argparse.metavar": META,
        "argparse.default": f"italic {META}", "argparse.groups": f"bold {HUE['blue']}",
        "argparse.prog": f"bold {HUE['magenta']}"}
# The id's microsecond digits and 3 hex. `show` takes a tail and refuses
# one that two ids share, so the short form is always safe to type.
ID_TAIL = 10
KIND_W = 8                  # the pipe line pads the kind to 8 too
MIN_TAGS = 8                # a tag list cut shorter than this says nothing
MIN_BODY = 30               # narrower, the body leaves the target's column

_UNITS = ((3600, 60, "m"), (86400, 3600, "h"), (14 * 86400, 86400, "d"),
          (63 * 86400, 7 * 86400, "w"), (365 * 86400, 30.44 * 86400, "mo"))


def age(stamp: str, now: datetime | None = None) -> str:
    """How long ago, in one short unit: `now`, `40m`, `8h`, `3d`, `5w`,
    `4mo`, `2y`. A stamp without an offset is local time, as in
    summary.age_days."""
    then = datetime.fromisoformat(stamp)
    if then.tzinfo is None:
        then = then.astimezone()
    s = max(0.0, ((now or datetime.now().astimezone()) - then).total_seconds())
    if s < 60:
        return "now"
    for below, size, unit in _UNITS:
        if s < below:
            return f"{int(s // size)}{unit}"
    return f"{int(s // (365 * 86400))}y"


class _CodeTheme(SyntaxTheme):
    """A fenced block in the inline code's colour, comments faded. rich's
    default, monokai, paints its own background box."""
    def get_style_for_token(self, token_type):
        comment = token_type[:1] == ("Comment",)       # pygments' Comment.*
        return Style(color=FADED if comment else HUE["cyan"])

    def get_background_style(self):
        return Style.null()


def _color_system():
    """rich's reading of this terminal, or None where it prints no colour:
    NO_COLOR, a dumb terminal, and a legacy Windows console, which rich
    colours through an API rather than codes."""
    con = Console()
    return None if con.no_color or con.legacy_windows else COLOR_SYSTEMS.get(con.color_system)


def help_formatter(plain):
    """The formatter that prints a parser's --help at a terminal, in place
    of argparse's `plain` class: rich_argparse's, in the palette. The words
    stay argparse's: its markup is off, since help text holds brackets
    (`'symbion[gui]'`), and section names keep argparse's spelling. The
    wrap is rich's, which keeps a hyphenated word (`near-synonyms`) whole."""
    import argparse
    from rich_argparse import RawDescriptionRichHelpFormatter, RichHelpFormatter

    class Help(RichHelpFormatter):
        styles = {**RichHelpFormatter.styles, **HELP}
        group_name_formatter = str
        help_markup = text_markup = False

    if issubclass(plain, argparse.RawDescriptionHelpFormatter):
        return type("RawHelp", (RawDescriptionRichHelpFormatter, Help), {})
    return Help


def painter(kinds=None):
    """A `paint(text, role)` for summary.render_summary and the other plain
    renderers: the text in its role's colour, as the escape codes this
    terminal takes; plain text where _color_system finds none. The width
    math stays the caller's: a colour code takes no column."""
    system = _color_system()
    if system is None:
        return summ.plain

    def paint(text: str, role: str) -> str:
        kind = role.removeprefix("kind:")
        c = ROLE[role] if kind == role else kind_hex(kind, (kinds or {}).get(kind))
        return Style(color=c).render(text, color_system=system)
    return paint


def _state(n, state) -> tuple[str, str]:
    st, dist = state
    if st == "unverifiable":
        return f"unverifiable ({api.why_unverifiable(n.provenance)})", BAD
    if st == "external":           # the run's age: a supersede inherits it
        return f"external ({summ.age_phrase(n.provenance['at'])})", META
    if st == "unstamped":          # the row's age: no stamp says when the run was
        return f"unstamped ({summ.age_phrase(n.created_at)})", META
    if st == "current":
        return st, GOOD
    if st == "pending":            # registered, not yet run: nothing to judge
        return st, META
    return (st if dist is None else f"{st} {dist}"), WARN


def _head(n, *, status, state, due, subject, head, width, gui, since=None) -> tuple[Text, int]:
    """The row's first line, and the column its target starts at. With a
    `width`, a line too long for it gives up the refs count first, then the
    end of its tags, then the end of its target. With `gui`, a running
    serve's URL, the id opens the row's page there."""
    faded = status == "resolved" or head is not None
    ink = (lambda c: FADED) if faded else (lambda c: c)
    # The link on the id's span only: a Text's own style reaches every span
    # appended to it, and the whole line linked (2026-10-01).
    line = Text(style=ink(META))
    line.append(n.id[-ID_TAIL:], Style(color=ink(META), link=gui and f"{gui}/notes?id={n.id}"))
    line.append(f"  {age(n.created_at):>4}  ", ink(META))
    mark, color = {"open": ("○", ink(TEXT)), "resolved": ("✓", GOOD)}.get(status, (" ", ""))
    line.append(mark + " ", color)
    line.append(f"{n.kind:<{KIND_W}}  ", ink(kind_hex(n.kind, n.spec)))
    indent = line.cell_len

    type_, _, name = summ.ref_label(n.target).partition(":")
    target = Text()
    if name:
        target.append(type_ + ":", ink(META))
        target.append(name, ink(TEXT))
        if subject:
            target.append(" " + summ.flatten(subject), ink(TEXT))
    else:
        target.append(type_, ink(TEXT))          # `project`

    post = Text()
    if state is not None:
        word, color = _state(n, state)
        post.append("  " + word, ink(color))
    if since:
        post.append(f"  {summ._count(since, 'commit')} since", ink(META))
    if due is not None:
        past, days = due
        post.append("  " + summ.due_phrase(due), ink(ROLE[summ.due_role(past, days)]))
    if head is not None:
        post.append(f"  superseded → {head.id[-ID_TAIL:]}", FADED)

    tags = Text("  " + " ".join(f"#{t}" for t in n.tags), ink(META)) if n.tags else Text()
    refs = Text()
    if n.refs and width is not None:     # the full view lists them on a line of their own
        refs = Text("  " + summ._count(len(n.refs), "ref"), ink(META))
    tail = tags + refs
    if width is not None:
        room = width - indent - post.cell_len
        if target.cell_len + tail.cell_len > room:
            tail = tags
        if target.cell_len + tail.cell_len > room:
            keep = room - target.cell_len
            tail.truncate(keep if keep >= MIN_TAGS else 0, overflow="ellipsis")
        if target.cell_len + tail.cell_len > room:
            target.truncate(max(1, room - tail.cell_len), overflow="ellipsis")
    return line + target + post + tail, indent


def _teaser(n, faded: bool, indent: int, width: int) -> Text | None:
    """One line under the target: a check's result, else the body, else what
    it checked."""
    if n.spec.verdict and n.result is not None:
        label, text = "result: ", n.result
    elif n.body:
        label, text = "", n.body
    elif n.checked is not None:
        label, text = "checked: ", n.checked
    else:
        return None
    if width - indent < MIN_BODY:
        indent = 4
    line = Text(" " * indent)
    line.append(label, FADED if faded else META)
    line.append(summ.clip(text, max(1, width - indent - len(label))), FADED if faded else BODY)
    return line


def _details(n, head, state) -> list[Text]:
    """The full view's lines between the head and the body."""
    def labelled(label, value):
        t = Text("    ")
        t.append(label, META)
        t.append(value, BODY)
        return t
    who = Text(f"    {n.id} · written {store.shown(n.created_at)} by {n.author}", META)
    if n.supersedes:
        who.append(f" · revises {n.supersedes}", META)
    out = [who]
    if n.refs:
        out.append(labelled("refs  ", ", ".join(summ.ref_label(r) for r in n.refs)))
    if n.spec.verdict:
        sha = store.stamp_sha(n.provenance)
        at = f" at {sha[:7]}" if sha else ""
        if state and state[0] == "pending":
            out.append(labelled(f"to check, registered{at}: ", summ.flatten(n.checked)))
        elif n.checked is not None or at:
            out.append(labelled(f"checked{at}: ", summ.flatten(n.checked)))
        if n.result is not None:
            out.append(labelled("result: ", summ.flatten(n.result)))
    return out


def print_note(n, *, status, state, due, subject, head, full, gui=None, since=None) -> None:
    """`status` is the chain head's, `state` api.verdict_state's pair and
    `due` store.due_state's pair for an open row, all computed by the caller
    the same way for the pipe line. `gui` is the URL of a serve on the row's
    store."""
    con = Console(highlight=False, markup=False, emoji=False, theme=MARKDOWN)
    width = None if full else con.width
    line, indent = _head(n, status=status, state=state, due=due, subject=subject,
                         head=head, width=width, gui=gui, since=since)
    if not full:
        con.print(line, no_wrap=True, overflow="ellipsis", crop=True)
        teaser = _teaser(n, status == "resolved" or head is not None, indent, width)
        if teaser is not None:
            con.print(teaser, no_wrap=True, overflow="ellipsis", crop=True)
        return
    con.print(line)
    for t in _details(n, head, state):
        con.print(t)
    if n.body:
        md = Markdown(n.body, code_theme=_CodeTheme())
        # ponytail: sets rich's own attribute; test_a_tag_in_a_body_prints_on_a_tty
        # fails if rich renames it.
        md.parsed = BODY_MD.parse(n.body)
        con.print(Padding(md, (0, 0, 0, 4)))
