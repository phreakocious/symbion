# Symbion map: a store's entries as a treemap

**Date:** 2026-10-29
**Status:** Agreed in conversation 2026-10-29, in parts, over a throwaway spike drawn
from a real store, then reviewed by a fresh reader; the user answered the one
question that review left them. This document waits on the user's read.
**Relates to:** the GUI design (2026-09-08); the Docs page (2026-10-16); the URL
filters of `/notes` (`gui/filters.py`).

## The need

The user's row, 2026-10-29: "'map' view.. a treemap with pivotable angle..
targets/tags/etc." In conversation the same day: "a visual representation of the
entries in a store, organized/grouped on our various axes". The question it answers:
"where the work and history pile up, at a glance". Of the spike: "pretty close to
what I had in mind".

Today the GUI lists entries: by target (`/targets`), by tag (`/tags`), by filter
(`/notes`). Nothing shows the whole store's shape at once.

## What the page shows

`/map`, in the sidebar under Docs. One treemap of the store's heads.

- **Area is history.** A tile's area is its count of entries, of every kind and
  state that the page shows.
- **Colour is work.** By default a tile's colour is its count of open entries:
  brighter for more, and a tile with none takes the board's surface colour. The
  scale is logarithmic, against the tile with the most, so one open entry still
  shows. Share open was tried and dropped: a tile with 1 entry, open, outshone a
  file with dozens of entries and several open (the user: "awkward for a 1/1").
- **Open** in a colour or a label means what the Docs page's "N open" means: open
  and not parked. A parked entry is not live work.
- **Labels** name the tile, its count, and its open count where it has any:
  `app.py 12 · 2 open`.
- **Groups** nest: each level's tiles sit inside the one above, with its name and
  count in a header strip.

## Controls

| Control | Values | Default |
|---|---|---|
| group by | up to three axes, in order | target |
| show | every entry; open only (parked included); status kinds only | every entry |
| colour | open count; kind; newest entry | open count |

The axes:

- **target**, as a path tree. A `file:` or `doc:` name splits on `/`, and so does
  a name of each `[repos]` type in a store over several repos. Other types nest
  one level: `item: NAME`, `commit: SHORTSHA`, `project`. A directory with one
  child and no entries of its own merges into it: one tile `src/pkg`, not `src`
  holding `pkg`. An entry on a node that also has children (a target named as a
  directory) gets a child tile of its own.
  An entry counts on its own target and on each object its refs name, once each,
  as `/targets`, `/docs` and `/object` count it (`_naming` in `gui/pages.py`; the
  map reuses it). The user, 2026-10-29, asked whether the map should count refs:
  "I think we should use the same logic". So target tiles add up to more than
  the store.
- **tag.** An entry counts once per tag, so tag tiles add up to more than the
  store. No tag reads `(no tag)`.

Beside the map, the page says when its tiles count an entry more than once (a
target or tag axis). A tile's area is its count of placements; a group's label
counts each entry once, as the panel lists them.
- **kind**; **author**; **arc** (by its name; `(no arc)`).
- **status**: open, parked, resolved, plain. Parked comes from the store's kinds
  table (`spec.parked`), never from a kind's name. Plain is a kind with no status.
- **week written**: the week's Monday, in the time zone the rest of the GUI shows.

**Written** means when the entry was first written: its chain's first stamp
(`Note.first_written`, else `created_at`), never the head's. A head's stamp moves
on every edit and resolve, so one retag sweep would move every row it touched into
this week. The week axis, the newest-entry colour and the panel's order all read it.

Colour by **kind** gives a tile the colour of its most common kind, in the GUI's
kind palette. Colour by **newest entry** is bright for a tile written in the last
day and fades to the surface colour at 21 days (the spike's scale).

*My call, after review:* a ramp from the surface colour to a bright hue passes a
band where neither the palette's dark text nor its light text reads at 4.5:1
(measured: the spike's open-count ramp failed from 14% to 33% of the way, its
newest-entry ramp from 27% to 45%). So a ramp starts past that band: a tile with
nothing to colour is the surface colour, with light text, and the first step
already takes dark text (`bg_page`). The theme test holds every step.

Every control lives in the URL, as `/notes` filters do, so a view can be bookmarked
and shared: `/map?by=tag,kind&show=open&color=kind`. An unknown value reads as the
default. The zoom does not go in the URL.

*2026-10-30, the user:* back undoes a focus. Asked whether that puts the focus
in the URL: "I don't care about bookmarking a map location, but I would just
like back to work". So each focus is an entry in the tab's history, and the URL
stays the view's. The entry holds the focus as its labels from the top, not a
tile id: back to the map from an entry its panel listed draws the page anew,
which finds the group by its labels, or shows the whole map once it is gone.

## A click

A click on a tile zooms the map to it. A bar under the map names the path and
zooms back out to any level of it.

*2026-10-30, the user:* a click on a group fills the map with it, as the bar's
levels do. ECharts' `zoomToNode` scaled the whole tree and panned to the group,
so its neighbours stayed in view, cut off at the edges. A click on a tile with
no children leaves the map as it is: the map filled with one tile shows nothing
the panel does not. So the panel no longer says to zoom in: a group's says to
click a tile in it, and a leaf's to search or pick another axis.

A click also lists the tile's entries in a panel under the map: open ones first,
then newest, each a row with its kind, status, target and the start of its body,
linked to the entry (`/notes?id=ID`). The user, asked what a pile is for once found:
"what it does now". The panel lists at most 60. Past that, a link opens all of
them where a page lists the same entries: a target's tile opens its `/object`
page, which counts refs as the map does; a path of other axes, each one a filter
`/notes` takes (tag, kind, author, arc, open or resolved), opens `/notes` with
those filters. `/notes?type=&name=` matches a row's own target only, so a path
that joins a target to another axis has no such page, nor has a directory or a
week: its panel says to zoom in.

*2026-10-29, at planning:* a status tile links only when it is `resolved`, since
`/notes?status=open` keeps parked entries, which the status axis splits off; the
object page has no filter, so a target tile links only when the map shows every
entry.

The server holds which entries each tile has. The chart's data carries each
tile's path, not its entries' ids: an id at every depth would grow the page by the
store's size times the depth.

## Non-goals

- Sizing a tile by anything but its entry count.
- One map across several stores.
- The search box filtering the map. It works on `/map` as on any page without
  cards: a pause searches the store.
- Editing from the map. Every write stays where it is.

## Constraints

- No new dependency: NiceGUI's `ui.echart` draws the treemap (ECharts, as NiceGUI
  ships it).
- The page is one chart element and a short panel, whatever the store's size: a
  page's server cost is in its elements, about 32 to an entry card. The chart's
  data grows with the number of distinct tiles.
- A read-only serve shows the map as any other serve does. It writes nothing.
- Dark only, in the GUI's palette (`gui/theme.py`); text on a tile in whichever of
  the palette's dark or light text reads on that colour, at the contrast the theme
  test holds.

## Left to the builder

- Where the tree is built: a module of pure functions, like `gui/filters.py`, so it
  tests without NiceGUI.
- How the panel's rows are drawn, within the card budget: a row, not a card.
- The ECharts options: borders, gaps, header strips, the label cut-off, the
  smallest tile drawn.
- The sidebar icon.
- The checks: the tree, the URL round trip and the panel test in the suite; the
  chart's zoom and click run only in a browser (the `user` fixture runs no client
  JS), so check them there. Time the page as it is served.
