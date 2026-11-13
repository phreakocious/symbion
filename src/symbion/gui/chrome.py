"""Per-page shell: head CSS, the top bar, the author badge and the commit
button. The author badge is not decoration -- `serve` is launched from a shell
carrying CLAUDECODE=1, and showing the resolved identity is what makes a
misattribution visible BEFORE it is permanent in an append-only file."""
from __future__ import annotations

import itertools
import json
import re
import time
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote

from nicegui import app, run, ui

from .. import api, gitref, merge
from .. import store as S
from .. import summary as summ
from . import servers
from .filters import href
from .notes import (FILE_MD_EXTRAS, add_form, ago, demo_says, fenced, heading, kind_class,
                    peek_entry, reading, render_note, say_after_reload, short_id, show_said,
                    undo_dialog, viewer, discard_dialog)
from .theme import (DARK_CSS, FONTS_HTML, LOGO_SVG, NARROW, kind_css, quasar_colors,
                    root_vars_css)

# From anywhere but a field being typed in or an open dialog: `/` focuses the
# search box, `n` and `?` press the buttons they name. What is typed in the
# box hides the cards on the page that lack a word of it (`sbFilter`): every
# word, in any case, in the card's text or its id. A page of docs, tags or
# arcs marks its rows `sb-filterable`: with no cards, a word read as hiding
# them all and opened the store's search (the user, 2026-10-29). When it
# hides them all on a page of entry cards, a pause in the typing searches the
# store, as Enter does: a superseded id, or a row on no card of this page,
# read as gone (the user, 2026-10-05). Elsewhere it does not (2026-10-29),
# and the map, which narrows itself, filters nothing. While the box has words, `sb-sifting` on the body shows the
# rows a board drew past its first few (`sb-past`): the filter reaches them all.
_KEYS_JS = """<script>
let sbExpandTimer;
const sbKeys = {'/': '.sb-search input', 'n': '.sb-new', '?': '.sb-keys'};
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey || !Object.hasOwn(sbKeys, e.key)) return;
  if (e.target.closest && e.target.closest('input, textarea, [contenteditable]')) return;
  if (document.querySelector('.q-dialog')) return;
  const el = document.querySelector(sbKeys[e.key]);
  if (!el) return;
  e.preventDefault();
  if (e.key === '/') { el.focus(); el.select(); } else el.click();
});
function sbFilter(text, expand) {
  // A "quoted phrase" is one word, as filters.search_pattern reads the box.
  const words = [...(text || '').toLowerCase().matchAll(/"([^"]*)"?|([^\\s"]+)/g)]
    .map(m => (m[1] ?? m[2]).trim().split(/\\s+/).join(' ')).filter(Boolean);
  const units = ':is(.sb-note, .sb-filterable)';
  for (const card of document.querySelectorAll('.sb-main ' + units)) {
    const hay = (card.textContent + ' ' + (card.dataset.id || ''))
      .toLowerCase().replace(/\\s+/g, ' ');
    card.classList.toggle('sb-filtered', !words.every(w => hay.includes(w)));
  }
  for (const board of document.querySelectorAll('.sb-board'))
    board.classList.toggle('sb-filtered',
      words.length > 0 && !board.querySelector(units + ':not(.sb-filtered)'));
  document.body.classList.toggle('sb-sifting', words.length > 0);
  clearTimeout(sbExpandTimer);
  if (expand && words.length && !document.querySelector('.sb-main ' + units + ':not(.sb-filtered)'))
    sbExpandTimer = setTimeout(() => expand(text), 700);
}
</script>"""

# A file dropped anywhere on a page goes to the server as text, as one
# `sbDrop` event that `_new_note_button` takes (the drop design,
# 2026-10-15). The browser's own handling, which opened the file in place
# of the page, never runs. A drag of selected text carries no `Files` and
# keeps it. ponytail: text over the socket, so images and binary files are
# refused; `ui.upload`'s per-client HTTP route would take them.
_DROP_JS = """<script>
const sbDropBytes = DROP_BYTES;
const sbFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
document.addEventListener('dragover', e => {
  if (!sbFiles(e)) return;
  e.preventDefault();
  document.body.dataset.sbDrop = document.querySelector('.sb-new-card')
    ? 'drop into the entry being written' : document.querySelector('.q-dialog')
    ? 'close the open dialog, then drop the file' : 'drop to start an entry';
});
document.addEventListener('dragleave', e => {
  if (!e.relatedTarget) delete document.body.dataset.sbDrop;   // left the window
});
document.addEventListener('drop', async e => {
  if (!sbFiles(e)) return;
  e.preventDefault();
  delete document.body.dataset.sbDrop;
  const say = refused => emitEvent('sbDrop', {refused});
  const files = e.dataTransfer.files;
  if (files.length !== 1) return say('drop one file at a time');
  const f = files[0];
  if (f.size > sbDropBytes)
    return say(`${f.name} is over ${sbDropBytes.toLocaleString('en')} bytes`);
  let text;
  try { text = new TextDecoder('utf-8', {fatal: true}).decode(await f.arrayBuffer()); }
  catch { return say(`${f.name}: text files only`); }
  if (text.includes('\\0')) return say(`${f.name}: text files only`);
  const composer = document.querySelector('.sb-new-card');
  if (!composer && document.querySelector('.q-dialog'))
    return say('close the open dialog, then drop the file');
  const body = composer && composer.querySelector('.sb-compose-body textarea');
  const at = n => [...body.value.slice(0, n)].length;   // code points, as Python counts
  emitEvent('sbDrop', {name: f.name, text,
                       cursor: body ? [at(body.selectionStart), at(body.selectionEnd)] : null});
});
</script>""".replace("DROP_BYTES", str(api.DROP_BYTES))

# A plain click on a link to another site opens it in a new tab, and in the
# installed app, in the browser: in the app's window it would leave the app
# (the user, 2026-10-16). A body's HTML passes DOMPurify, which drops a
# `target`, so the click does it. A modified click keeps the browser's own
# meaning, and a link to an app (`vscode://`) its handler.
_LINKS_JS = """<script>
document.addEventListener('click', e => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  const url = new URL(a.href, location.href);
  if (url.origin === location.origin || !/^https?:$/.test(url.protocol)) return;
  e.preventDefault();
  window.open(url.href, '_blank', 'noopener,noreferrer');
});
</script>"""

# What a Chrome tab has and the installed app's window lacks (the user,
# 2026-10-16). Chromium draws no status bubble in an app window without
# minimal-ui's buttons, and a tabbed window has none
# (BrowserUiController::GetStatusBubbles): the address of the link under the
# pointer shows at the bottom left, as the bubble does. Cmd+T (Ctrl+T) opened
# no tab there, though the ⋮ menu's New Tab did; an app window reserves no key
# (BrowserCommandController::IsReservedCommandOrKey), so the page takes it and
# opens the start page, as that menu item does. Without an opener: Chrome's
# navigation capturing opens an app window's window.open that keeps one in a
# new app window, tabbed or not (navigation_capturing_process.cc), and one
# with none in a tab of the window (an installed tabbed app, 2026-10-16).
_APP_JS = """<script>
if (matchMedia('(display-mode: standalone), (display-mode: tabbed)').matches) {
  const tip = document.createElement('div');
  tip.className = 'sb-linktip';
  document.addEventListener('mouseover', e => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (a) { tip.textContent = a.href; document.body.append(tip); } else tip.remove();
  });
  document.addEventListener('mouseout', e => { if (!e.relatedTarget) tip.remove(); });
  const mac = /Mac/.test(navigator.platform);
  document.addEventListener('keydown', e => {
    if (e.key.toLowerCase() !== 't' || e.shiftKey || e.altKey
        || (mac ? !e.metaKey || e.ctrlKey : !e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    window.open('/', '_blank', 'noopener');
  });
}
</script>"""

# The peek: a plain click on a card's text, or j/k, shows the entry beside the
# page (`sbPeek`, which `_peek_panel` takes), and so does a click on a card the
# stale button lists in the peek (`_drift_list`); a modified click keeps the
# browser's own new tab, on every card alike (the user, 2026-10-15). The other
# keys press the peek's own buttons, so each does what its button does. Open
# is read from where the drawer sits: it slides off the right edge when closed.
# A card's tooltips (notes.Card, card.js): a `data-tip` attribute, shown in
# one shared element under the pointer's target, or above it near the
# window's foot, and kept inside the window as Quasar's are. A q-tooltip each
# was a server element and a Vue component a chip. A tip filled from the
# server on the first hover (`data-tip-key`) shows when its text arrives.
_TIPS_JS = """<script>
const sbTip = Object.assign(document.createElement('div'), {className: 'sb-tip', hidden: true});
let sbTipAt = null, sbTipWatch = null;
// data-tip-cut: a tip only while an ellipsis cuts the words it holds
function sbTipPlace() {
  const a = sbTipAt, text = a && (a.dataset.tip || a.scrollWidth > a.clientWidth && a.dataset.tipCut);
  if (!text) { sbTip.hidden = true; return; }
  if (!sbTip.isConnected) document.body.append(sbTip);
  sbTip.textContent = text;
  sbTip.hidden = false;
  const r = sbTipAt.getBoundingClientRect(), w = sbTip.offsetWidth, h = sbTip.offsetHeight;
  sbTip.style.left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8)) + 'px';
  sbTip.style.top = (r.bottom + 8 + h > innerHeight ? r.top - 8 - h : r.bottom + 8) + 'px';
}
function sbTipTo(el) {
  if (el === sbTipAt) return;
  sbTipAt = el;
  if (sbTipWatch) sbTipWatch.disconnect();
  sbTipWatch = el && new MutationObserver(sbTipPlace);
  if (el) sbTipWatch.observe(el, {attributes: true, attributeFilter: ['data-tip']});
  sbTipPlace();
}
const sbTipOf = e => e.target.closest ? e.target.closest('[data-tip], [data-tip-key], [data-tip-cut]') : null;
document.addEventListener('mouseover', e => sbTipTo(sbTipOf(e)));
document.addEventListener('mouseout', e => { if (!e.relatedTarget) sbTipTo(null); });
// Focus shows a tip as a hover does: a tip held facts no keyboard reached
// (the GUI review, 2026-11-07). A key hides it first, so Tab moves it.
document.addEventListener('focusin', e => sbTipTo(sbTipOf(e)));
document.addEventListener('focusout', e => { if (!e.relatedTarget) sbTipTo(null); });
for (const ev of ['scroll', 'mousedown', 'keydown'])
  document.addEventListener(ev, () => sbTipTo(null), true);
</script>"""

_PEEK_JS = """<script>
function sbCards() {
  return [...document.querySelectorAll('.sb-main .sb-note[data-id]')]
    .filter(c => !c.closest('.sb-filtered, .q-expansion'));
}
function sbPeek(card) {
  document.querySelectorAll('.sb-picked').forEach(c => c.classList.remove('sb-picked'));
  card.classList.add('sb-picked');
  // A click on a clipped body focuses its link, and the next key drew the
  // focus ring round the words, left on that card as j moved on (the user,
  // 2026-10-16). The pick is the mark.
  const f = document.activeElement;
  if (f && f.closest('.sb-main .sb-note')) f.blur();
  card.scrollIntoView({block: 'nearest'});
  sbHold();
  emitEvent('sbPeek', {id: card.dataset.id});
}
// The pick keeps its place in the window as the column changes width. Chrome's
// scroll anchoring stood still while Quasar animated the page's padding for the
// peek: the cards above re-wrapped taller, and the pick fell below the window
// (2026-11-09). With the pick out of the window, the card at its top, under
// the header, is held, as Chrome's own anchoring would: a close moved the
// cards a reader had scrolled to out of the window.
let sbColW = 0, sbHeld = null, sbHeldTop = 0;
function sbHold() {
  const m = document.querySelector('.sb-main');      // none on a page whose stale list picked
  if (m) sbColWatch.observe(m);
  sbPickAt();
}
function sbPickAt() {
  const pick = document.querySelector('.sb-main .sb-picked'), b = pick && pick.getBoundingClientRect();
  const m = document.querySelector('.sb-main')?.getBoundingClientRect();
  sbHeld = b && b.height && b.bottom > 0 && b.top < innerHeight ? pick : null;
  // a point under the header can fall in a gap between sections or on a heading
  let y = (document.querySelector('.q-header')?.getBoundingClientRect().bottom || 0) + 16;
  for (; !sbHeld && m && y < innerHeight / 2; y += 24)
    sbHeld = document.elementFromPoint(m.left + m.width / 2, y)?.closest('.sb-main .sb-note');
  if (sbHeld) sbHeldTop = sbHeld.getBoundingClientRect().top;
}
const sbColWatch = new ResizeObserver(([e]) => {
  const c = sbHeld, w = e.target.offsetWidth;
  if (c && c.isConnected && sbColW && w !== sbColW) {
    scrollBy({top: c.getBoundingClientRect().top - sbHeldTop, behavior: 'instant'});
    if (c.classList.contains('sb-picked'))      // it re-wrapped taller too
      c.scrollIntoView({block: 'nearest', behavior: 'instant'});
  }
  sbColW = w;
  sbPickAt();
});
// Not while the width is changing: Chrome lays out the next frame's width before
// this event, and the pick's drifted place was taken as its own.
addEventListener('scroll', () => {
  if (document.querySelector('.sb-main')?.offsetWidth === sbColW) sbPickAt();
}, {passive: true});
function sbPeekOpen() {
  // shell sets sb-peeking from the drawer's value. Its left edge read open
  // when closed on a page that did not scroll, under scrollbar-gutter:
  // stable, and x resolved the entry the hidden peek still held (2026-10-16).
  return !!document.querySelector('.q-layout.sb-peeking');
}
document.addEventListener('click', e => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const t = e.target.closest ? e.target : null;
  const card = t && t.closest('.sb-main .sb-note[data-id], .sb-drift .sb-note[data-id]');
  if (!card || getSelection().toString()) return;      // selecting words, not picking
  const control = t.closest('a, button, input, textarea, label, .q-btn');
  if (control && !control.matches('a.sb-note-open')) return;   // a link or control of its own
  if (!t.closest('a.sb-note-open, .sb-note-text, .nicegui-markdown, .sb-verdict')) return;
  e.preventDefault();
  sbPeek(card);
});
// A click on the page's background closes the peek, as Esc and its button
// do (the user, 2026-11-08): not one on a card, a control, the peek, or a
// dialog, menu or palette over the page, and not one that ends a selection.
const sbPeekStays = '.sb-peek, .sb-note, a, button, input, textarea, select, label, ' +
  '[role=button], .q-btn, .q-field, .q-dialog, .q-menu, .q-notification, .sb-palette';
document.addEventListener('click', e => {
  if (e.defaultPrevented || e.button !== 0 || !sbPeekOpen() || getSelection().toString()) return;
  const t = e.target.closest ? e.target : null;
  if (!t || !t.isConnected || t.closest(sbPeekStays)) return;
  document.querySelector('.sb-peek-close')?.click();
});
// The peek in the URL as #peek=<id>: it opened and closed beside history,
// so a phone's Back left the page under it, and a reload or a shared link
// lost it (the GUI review, 2026-11-07). Opening pushes one entry, j and k
// replace it, so one Back closes the peek; a close by its button goes back.
// The entry under the peek restores no scroll as history returns to it: it
// held the scroll from when the peek opened, so a close threw a reader who
// had scrolled back there, and where the peek locks the body it held 0
// (2026-11-09). It restores again from the moment history lands on it.
function sbPeekHash(id) {
  const h = '#peek=' + encodeURIComponent(id);
  if (location.hash === h) return;
  if (history.state && history.state.sbPeek) history.replaceState({sbPeek: 1}, '', h);
  else {
    history.scrollRestoration = 'manual';
    history.pushState({sbPeek: 1}, '', h);
    history.scrollRestoration = 'auto';         // the peek's own entry: a reload restores it
  }
}
function sbPeekUnhash() {
  if (!location.hash.startsWith('#peek=')) return;
  if (history.state && history.state.sbPeek) history.back();
  else history.replaceState(history.state, '', location.pathname + location.search);
}
function sbPeekFromHash() {
  const m = location.hash.match(/^#peek=(.+)$/);
  if (!m) return;
  const id = decodeURIComponent(m[1]);
  const card = sbCards().find(c => c.dataset.id === id);
  if (card) sbPeek(card); else emitEvent('sbPeek', {id});
}
addEventListener('popstate', () => {
  if (location.hash.startsWith('#peek=')) return sbPeekFromHash();
  history.scrollRestoration = 'auto';
  if (sbPeekOpen()) document.querySelector('.sb-peek-close')?.click();
});
// Below PEEK_WIDE Quasar locks the body while the peek is open, and at the
// unlock puts the scroll back only when the URL is the one it locked at. The
// #peek= entry changed the URL, and a close by Esc left the page at its top
// (2026-11-09). This puts it back where the lock found it. It watches from
// before the scripts that mount the page: a page built with the peek open
// locks before DOMContentLoaded, and takes its place from the cookie below,
// where the lock found scrollY at 0 (2026-11-10).
let sbKeptY = null;
document.addEventListener('readystatechange', () => {
  if (document.readyState !== 'interactive') return;
  let y = null;
  new MutationObserver(() => {
    const b = document.body, locked = b.classList.contains('q-body--prevent-scroll');
    if (locked && y === null) {
      y = -parseFloat(b.style.top) || 0;
      if (sbKeptY !== null) b.style.top = -(y = sbKeptY) + 'px';
      sbKeptY = null;
    } else if (!locked && y !== null) {
      if (Math.abs(scrollY - y) > 1) scrollTo({top: y, behavior: 'instant'});
      y = null;
    }
  }).observe(document.body, {attributes: true, attributeFilter: ['class']});
});
addEventListener('load', () => { sbKeptY = null; });     // the mount's lock has run by now
// A page that unloads with the peek open names itself, the entry and its
// scroll in a cookie, which a reload's request carries: `_peek_kept` builds
// that page with the peek open. A locked body holds its scroll in its top:
// scrollY read 0, and the reload came back at the page's top (2026-11-10).
// Each load reads the scroll and clears the cookie.
addEventListener('beforeunload', () => {
  const m = sbPeekOpen() && location.hash.match(/^#peek=(.+)$/);
  const b = document.body, y = b.classList.contains('q-body--prevent-scroll') ?
    -parseFloat(b.style.top) || 0 : scrollY;
  if (m) document.cookie = 'sb-peek=' + encodeURIComponent(location.pathname + location.search +
    ' ' + decodeURIComponent(m[1]) + ' ' + Math.round(y)) + '; path=/; max-age=5; samesite=lax';
});
{
  const c = document.cookie.match(/(?:^|;\\s*)sb-peek=([^;]*)/);
  const [at, , y] = c ? decodeURIComponent(c[1]).split(' ') : [];
  if (at === location.pathname + location.search && +y > 0) sbKeptY = +y;
}
document.cookie = 'sb-peek=; path=/; max-age=0; samesite=lax';
const sbPeekKeys = {x: '.sb-resolve', s: '.sb-star', e: '.sb-edit', o: '.sb-peek-page',
                    a: '.sb-answer-field input', Escape: '.sb-peek-close'};
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target.closest && e.target.closest('input, textarea, [contenteditable]')) return;
  if (document.querySelector('.q-dialog')) return;
  if (e.key === 'j' || e.key === 'k') {
    const cards = sbCards();
    if (!cards.length) return;
    const at = cards.indexOf(document.querySelector('.sb-picked'));
    const to = at < 0 ? 0 : Math.max(0, Math.min(cards.length - 1, at + (e.key === 'j' ? 1 : -1)));
    e.preventDefault();
    sbPeek(cards[to]);
    return;
  }
  if (!Object.hasOwn(sbPeekKeys, e.key) || !sbPeekOpen()) return;
  const el = document.querySelector('.sb-peek ' + sbPeekKeys[e.key]);
  if (!el) return;
  e.preventDefault();
  if (e.key === 'a') el.focus(); else el.click();
});
</script>"""

# An arc's board (arcs.board): a card dragged to a column moves there in the
# page at once, and the page writes it from one `sbMove` event, {id, phase,
# before}; at a column's end, {id, phase, after} its last card, since a
# column capped at a page of rows ends short of the store's. A redraw after
# each move would flicker and lose the board's scroll place (the phases
# design, 2026-11-05): the page redraws the cards of the column the card left
# and the one it entered, and a refused write redraws the board as the store
# has it. A drag that starts from a
# finger is dropped: a phone moves a box with its phase chip, and HTML5 drag
# is unreliable on iPhone Safari. The data is a type of its own, so a card
# dropped on a field types nothing and _DROP_JS, which takes `Files`, lets it
# be. `sbArcWide` answers the arc page: its tab's Board only where phase
# columns fit, a width the server cannot see.
# The arc's List drags an open row by its grip and drops it above or below
# the open row nearest the pointer, in that row's phase, so either end of a
# phase can be reached; the same `sbMove` writes it, and the page redraws the
# list. A drop there never resolves: the tick does.
_BOARD_JS = """<script>
let sbDragged = null, sbTouch = false;
const sbBox = 'application/x-symbion-box';
const sbMarks = ['sb-dropping', 'sb-drop-above', 'sb-drop-below'];
const sbUnmark = () => {
  for (const c of document.querySelectorAll(sbMarks.map(m => '.' + m).join(','))) c.classList.remove(...sbMarks);
};
document.addEventListener('pointerdown', e => { sbTouch = e.pointerType === 'touch'; }, true);
// a card's own drag only: one redrawn mid-drag never sends dragend here
const sbOurs = e => sbDragged && [...e.dataTransfer.types].includes(sbBox) && e.target.closest;
const sbColumn = e => sbOurs(e) && e.target.closest('.sb-board [data-phase]');
const sbRow = e => {
  const list = sbOurs(e) && e.target.closest('.sb-list');
  if (!list || !list.contains(sbDragged)) return null;
  let row = null, gap = Infinity;
  for (const r of list.querySelectorAll('.sb-note[data-phase]')) {
    const b = r.getBoundingClientRect(), d = Math.max(b.top - e.clientY, e.clientY - b.bottom, 0);
    if (d < gap) { gap = d; row = r; }
  }
  if (!row) return null;
  const b = row.getBoundingClientRect();
  return {row, below: e.clientY > b.top + b.height / 2};
};
document.addEventListener('dragstart', e => {
  const grip = e.target.closest && e.target.closest('.sb-list .sb-grip');
  const card = grip ? grip.closest('.sb-note')
    : e.target.closest && e.target.closest('.sb-board [data-phase] .sb-note[draggable="true"]');
  if (!card) return;
  if (sbTouch) { e.preventDefault(); return; }
  sbDragged = card;
  const r = card.getBoundingClientRect();
  e.dataTransfer.setDragImage(card, e.clientX - r.left, e.clientY - r.top);   // a link's own drag shows the link
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.clearData();                      // a link's URL, which a field would paste
  e.dataTransfer.setData(sbBox, card.dataset.id);
});
document.addEventListener('dragover', e => {
  const hit = sbRow(e), col = !hit && sbColumn(e);
  if (!sbDragged) return;
  sbUnmark();
  if (!hit && !col) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  if (col) col.classList.add('sb-dropping');
  else if (hit.row !== sbDragged) hit.row.classList.add(hit.below ? 'sb-drop-below' : 'sb-drop-above');
});
document.addEventListener('dragend', () => {
  sbDragged = null;
  sbUnmark();
});
document.addEventListener('drop', e => {
  const hit = sbRow(e), col = !hit && sbColumn(e), card = sbDragged;
  if (hit) {
    e.preventDefault();
    sbDragged = null;
    sbUnmark();
    const {row, below} = hit;
    if (row === card || !card.isConnected) return;
    if (row.dataset.phase === card.dataset.phase
        && (below ? row.nextElementSibling : row.previousElementSibling) === card) return;   // where it was
    row.parentElement.insertBefore(card, below ? row.nextElementSibling : row);
    emitEvent('sbMove', {id: card.dataset.id, phase: row.dataset.phase,
                         before: below ? null : row.dataset.id, after: below ? row.dataset.id : null});
    return;
  }
  if (!col) return;
  e.preventDefault();
  sbDragged = null;
  if (!card.isConnected) return;
  const from = card.closest('[data-phase]'), list = col.querySelector('.sb-phase-cards');
  const done = col.dataset.phase === '@DONE@';
  if (from === col && done) return;                     // done keeps its order: newest first
  const others = [...list.children].filter(c => c !== card);
  const next = done ? others[0] : others.find(c => {
    const r = c.getBoundingClientRect();
    return e.clientY < r.top + r.height / 2;
  });
  if (from === col && others[[...list.children].indexOf(card)] === next) return;   // where it was
  list.insertBefore(card, next || null);
  const last = others.at(-1);
  emitEvent('sbMove', {id: card.dataset.id, phase: col.dataset.phase,
                       before: next ? next.dataset.id : null,
                       after: !next && last ? last.dataset.id : null});
});
function sbArcWide() { if (matchMedia('(min-width: @WIDE@px)').matches) emitEvent('sbArcWide'); }
// After a move pick: the arc's redraw lands first, then the card's menu chip takes focus.
function sbFocusMove(id, ...parts) {      // the first part the card has takes focus
  requestAnimationFrame(() => {
    for (const part of parts.length ? parts : ['.sb-chip-opens']) {
      const el = document.querySelector(['.sb-board', '.sb-list']
        .map(s => `${s} .sb-note[data-id="${id}"] ${part}`).join(','));
      if (el) return el.focus();
    }
  });
}
</script>""".replace("@DONE@", S.DONE).replace("@WIDE@", str(NARROW + 1))

# The palette (the user, 2026-10-16): Cmd or Ctrl K, from anywhere but an open dialog,
# opens one box for the places, the top bar's actions and the entries here
# and in every other running store. `/palette` gives the rows for what is
# typed (palette.py), asked again at each pause. Focus stays in the box: the
# arrows move the pick, Enter takes it, and Cmd or Ctrl Enter opens its link
# in a new tab. Enter while an answer is due waits for it, so it takes a row
# for what was typed, not one drawn for the letters before.
_PALETTE_JS = """<script>
const sbMod = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl ';
let sbPal, sbPalRows = [], sbPalAt = 0, sbPalSeq = 0, sbPalTimer, sbPalShown = null, sbPalMouse,
    sbPalRest;
document.addEventListener('keydown', e => {
  if ((e.key || '').toLowerCase() !== 'k' || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
  if (document.querySelector('.q-dialog')) return;
  e.preventDefault();
  sbPalOpen();
});
function sbEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}
function sbIcon(name) {
  const i = sbEl('i', 'q-icon notranslate material-icons', name);
  i.setAttribute('aria-hidden', 'true');
  return i;
}
function sbPalOpen() {
  if (!sbPal) sbPalBuild();
  const box = sbPal.querySelector('input');
  if (!sbPal.open) {
    box.value = '';
    sbPal.querySelector('.sb-pal-list').replaceChildren();
    sbPal.showModal();
    // The rows at rest wait on the top bar's git read: the last ones show
    // while it runs (130 ms, 2026-10-16).
    if (sbPalRest) sbPalDraw(sbPalRest, '');
    sbPalAsk();
  }
  box.focus();
  box.select();
}
function sbPalBuild() {
  sbPal = sbEl('dialog', 'sb-palette');
  sbPal.setAttribute('aria-label', 'go to, run or find');
  const label = sbEl('label', 'sb-pal-box'), box = sbEl('input');
  box.placeholder = "Go to, run or find · an id's tail works";
  // A combobox and its listbox: the pick moved with nothing said, since
  // aria-activedescendant means nothing on a bare input (the design critique, 2026-11-07).
  for (const [k, v] of [['role', 'combobox'], ['aria-label', 'go to, run or find'],
                        ['aria-controls', 'sb-pal-list'], ['aria-expanded', 'true'],
                        ['aria-autocomplete', 'list']]) box.setAttribute(k, v);
  box.autocomplete = 'off';
  box.spellcheck = false;
  label.append(sbIcon('search'), box, sbEl('span', 'sb-chip sb-mono', 'esc'));
  const list = sbEl('div', 'sb-pal-list');
  list.id = 'sb-pal-list';
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'results');
  const foot = sbEl('div', 'sb-pal-foot');
  foot.append(sbEl('span', '', '↑↓ move'), sbEl('span', 'sb-pal-enter', '↵ go'),
              sbEl('span', 'sb-pal-tab', sbMod + '↵ in a new tab'), sbEl('span', '', 'esc close'));
  sbPal.append(label, list, foot);
  document.body.append(sbPal);
  box.addEventListener('input', () => {
    clearTimeout(sbPalTimer);
    sbPalTimer = setTimeout(sbPalAsk, 120);
  });
  box.addEventListener('keydown', async e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      sbPalPick(sbPalAt + (e.key === 'ArrowDown' ? 1 : -1));
    } else if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      const tab = e.metaKey || e.ctrlKey;
      if (sbPalShown !== box.value) await sbPalAsk();
      const row = sbPalRows[sbPalAt];
      if (!row) return;
      if (!tab) row.click();
      else if (row.href) window.open(row.href, '_blank', 'noopener');
    }
  });
  // A plain click runs a button, peeks at an entry here, or closes the box
  // and follows the link; a modified click keeps the browser's new tab.
  list.addEventListener('click', e => {
    const row = e.target.closest('.sb-pal-row');
    if (!row || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    sbPal.close();
    const r = row.sbRow;
    if (r.click) {
      e.preventDefault();
      document.querySelector(r.click)?.click();
    } else if (r.peek) {
      e.preventDefault();
      const card = document.querySelector(`.sb-main .sb-note[data-id="${CSS.escape(r.peek)}"]`);
      if (card) return sbPeek(card);
      document.querySelectorAll('.sb-picked').forEach(c => c.classList.remove('sb-picked'));
      emitEvent('sbPeek', {id: r.peek});
    }
  });
  sbPal.addEventListener('click', e => { if (e.target === sbPal) sbPal.close(); });  // the backdrop
}
async function sbPalAsk() {
  clearTimeout(sbPalTimer);
  const q = sbPal.querySelector('input').value, seq = ++sbPalSeq;
  let groups;
  try {
    groups = await (await fetch('/palette?' + new URLSearchParams({q}))).json();
  } catch {
    return;
  }
  if (seq === sbPalSeq) sbPalDraw(groups, q);
}
function sbPalDraw(groups, q) {
  const list = sbPal.querySelector('.sb-pal-list'), here = location.pathname + location.search;
  list.replaceChildren();
  sbPal.querySelector('input').removeAttribute('aria-activedescendant');
  sbPalRows = [];
  sbPalShown = q;
  if (!q) sbPalRest = groups;
  for (const [gi, g] of groups.entries()) {
    // A button the read named can be gone since: the top bar redraws on its own tick.
    const rows = g.rows.filter(r => !r.click || document.querySelector(r.click));
    if (g.rows.length && !rows.length) continue;
    const group = sbEl('div', 'sb-pal-group'), head = sbEl('div', 'sb-pal-head');
    group.setAttribute('role', 'group');
    head.id = 'sb-pal-g' + gi;
    group.setAttribute('aria-labelledby', head.id);
    head.append(sbEl('span', 'sb-nav-head', g.head));
    if (g.aside) head.append(sbEl('span', 'sb-pal-aside', g.aside));
    group.append(head);
    for (const r of rows) group.append(sbPalRow(r, here));
    list.append(group);
  }
  if (!list.childElementCount) {
    const none = sbEl('div', 'sb-pal-none', `nothing here or in another running store says “${q}”`);
    none.setAttribute('role', 'option');                 // what a listbox may hold
    none.setAttribute('aria-disabled', 'true');
    list.append(none);
  }
  sbPalPick(0);
}
function sbPalRow(r, here) {
  const row = sbEl(r.href ? 'a' : 'button', 'sb-pal-row' + (r.kind ? ' sb-pal-entry ' + r.kind : '')
                   + (r.done ? ' sb-pal-done' : ''));
  if (r.href) row.href = r.href; else row.type = 'button';
  row.id = 'sb-pal-' + sbPalRows.length;
  row.tabIndex = -1;
  row.setAttribute('role', 'option');
  row.setAttribute('aria-selected', 'false');
  row.append(r.icon ? sbIcon(r.icon) : sbEl('span', 'sb-dot'));
  const text = sbEl('span', 'sb-pal-text' + (r.mono ? ' sb-mono' : ''));
  if (r.html != null) text.innerHTML = r.html;   // escaped by the server: <mark> is its only markup
  else text.textContent = r.text;
  row.append(text);
  if (r.store) {
    const store = sbEl('span', 'sb-pal-store');
    store.append(sbIcon('swap_horiz'), r.store);
    row.append(store);
  }
  if (r.key) row.append(sbEl('span', 'sb-chip sb-mono', r.key));
  const meta = r.href === here ? 'this page' : r.meta;
  if (meta) row.append(sbEl('span', 'sb-pal-meta' + (r.notable ? ' sb-pal-notable' : ''), meta));
  if (r.kind_name) row.append(sbEl('span', 'sb-pal-kind', r.kind_name));
  if (r.id != null) {
    const id = sbEl('span', 'sb-pal-id'), tail = sbEl('span');
    if (r.done) {
      id.append(sbEl('span', 'sb-pal-good', '✓'), ' ');
      id.title = 'resolved';
    }
    tail.innerHTML = r.id;                       // escaped by the server, as the text
    id.append(tail);
    row.append(id);
  }
  row.sbRow = r;
  const at = sbPalRows.length;
  // Moved, not scrolled under: an arrow key's scroll sends Chrome a mousemove too.
  row.addEventListener('mousemove', e => {
    const where = e.clientX + ',' + e.clientY;
    if (where !== sbPalMouse && at !== sbPalAt) sbPalPick(at);
    sbPalMouse = where;
  });
  sbPalRows.push(row);
  return row;
}
function sbPalPick(i) {
  if (!sbPalRows.length) return;
  sbPalRows[sbPalAt]?.classList.remove('sb-pal-on');
  sbPalRows[sbPalAt]?.setAttribute('aria-selected', 'false');
  sbPalAt = Math.max(0, Math.min(sbPalRows.length - 1, i));
  const row = sbPalRows[sbPalAt], r = row.sbRow;
  row.classList.add('sb-pal-on');
  row.setAttribute('aria-selected', 'true');
  row.scrollIntoView({block: 'nearest'});
  sbPal.querySelector('input').setAttribute('aria-activedescendant', row.id);
  sbPal.querySelector('.sb-pal-enter').textContent = r.click ? '↵ run' : r.peek ? '↵ peek' : '↵ go';
  sbPal.querySelector('.sb-pal-tab').hidden = !r.href;
}
</script>"""

# The installed app's icon badge (the Badging API: Chromium's installed apps,
# Safari's Dock apps). In a browser tab it is missing or does nothing.
_BADGE_JS = """<script>
function sbBadge(n) {
  if (!navigator.setAppBadge) return;
  (n ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => {});
}
// A hidden tab slows the page's git poll, which the badge rides on (_git_buttons).
document.addEventListener('visibilitychange', () => emitEvent('sbHidden', document.hidden));
</script>"""

# What `?` lists. The kinds are the store's, read at render.
_KEYS = (("/", "filter this page"), ("Enter", "in the filter: search the whole store"),
         ("n", "new entry"), ("?", "this list"),
         ("⌘/Ctrl K", "go to a place, run an action, or find an entry by words or id, "
                      "here and in every other running store"),
         ("Esc", "close a dialog, the palette or the peek"),
         ("j k", "peek at the next or the previous entry on the page"),
         ("x s e", "in the peek: resolve, star, edit"), ("a", "in the peek: answer its question"),
         ("o", "in the peek: open the entry's page"),
         ("⌘/Ctrl Enter", "add the entry being written, or save an edit"),
         ("Shift Enter", "add it and write another"),
         ("#tag", "in an entry: adds the tag"))


def store_name(ctx) -> str:
    """Named from outside any repo, or a store over several repos (the serve
    is the store's, whichever repo it started in): the store's name."""
    root = ctx.cfg.project_root
    return root.name if root and not ctx.cfg.repos else ctx.store_dir.name


_BUILDS = itertools.count(1)


def shell(ctx, author: str, crumbs, *, q: str = "", keep: dict | None = None,
          here: str = "", new=("project", None, None), kind: str | None = None,
          cards: bool = True, search=None, tab: str | None = None) -> None:
    """`crumbs` end in the page's title, which the top bar shows: a title of
    its own under the bar left a band of nothing above it (the user,
    2026-10-01). A parent comes before it only on a page one level down (an
    arc); a "Notebook" before every title said nothing the sidebar does not
    (the user, 2026-10-13). `q` is the search being shown, `keep` the filters a new
    search keeps: on /notes a search narrows the list in view, elsewhere it
    starts one. `here` is the sidebar place this page is (`/`, `/notes`,
    `/tags`, `/arcs`), `new` the (type, name, arc) a new note starts on, and
    `kind` the kind it starts as. `cards` is False on a page with no entry
    cards, and `search` maps a page that narrows itself to the URL of the
    words searched: see `_search_box`."""
    # Chrome restores a reload's scroll by the DOM id of the element at the
    # window's top, and NiceGUI numbers elements as the page builds them:
    # after a write that moved a card or added a button, that id was another
    # card's, and the page jumped to it (2026-11-09). No two builds number
    # alike, so Chrome finds no such id and restores the offset.
    ui.context.client.next_element_id += next(_BUILDS) * 100_000
    ui.dark_mode().enable()
    name = store_name(ctx)
    # The project, then the page: the tab strip truncates from the right, and
    # tabs of several stores share a window. "<store> · symbion" on every
    # page left the tabs of the installed app alike; its window is one store's
    # and names it, so there the tab is the page alone (the user, 2026-10-15).
    # symbion last, where the strip cuts first: a shared link's preview read
    # the store's name and no description (the user, 2026-10-28).
    # `tab` where the title alone would not tell tabs apart: one entry's page.
    page = tab or crumbs[-1][0]
    ui.page_title(f"{name} · {page} · symbion")
    ui.add_head_html(f'<meta name="description" content="{ABOUT}">'
                     f'<meta property="og:description" content="{ABOUT}">')
    # A shared demo link previewed as the store's page title with no image
    # (the demo home review, 2026-11-05); a serve of your own is never previewed.
    if S.read_only():
        ui.add_head_html('<meta property="og:title" content="symbion: live demo">'
                         f'<meta property="og:image" content="{BANNER}">'
                         '<meta name="twitter:card" content="summary_large_image">')
    js_page = json.dumps(page).replace("<", "\\u003c")   # a search is in it: no </script>
    ui.add_head_html('<script>if (matchMedia("(display-mode: tabbed)").matches) '
                     f'document.title = {js_page}</script>')
    # The installed app in a desktop Chrome without tabbed mode, which falls
    # back to standalone: the sidebar's .sb-tabhint names the flag (the user,
    # 2026-10-16). userAgentData is Chromium's alone: Safari's Dock apps run
    # standalone too, and have no tab strip to turn on.
    ui.add_head_html('<script>if (navigator.userAgentData && !navigator.userAgentData.mobile'
                     ' && matchMedia("(display-mode: standalone)").matches'
                     ' && !matchMedia("(display-mode: tabbed)").matches)'
                     ' document.documentElement.classList.add("sb-untabbed")</script>')
    ui.add_head_html('<link rel="manifest" href="/manifest.webmanifest">')
    ui.colors(**quasar_colors())
    ui.add_head_html(root_vars_css())          # no border flash on first paint
    ui.add_head_html(FONTS_HTML)
    # Dark Reader applies its theme at once and detects a dark page only after
    # the body has content; this meta stops its theme. Its fallback style does
    # not wait for the theme: at document_start, before this head is parsed,
    # it sets every border a light brown !important, and only the lock check takes
    # it off, once Dark Reader's background answers. A slow answer showed every
    # card in a bright full border (Dark Reader 4.9.133, 2026-10-01). The
    # script takes it off first. A theme begun before this meta was parsed
    # leaves the meta to remove it, but a copy of an @import still loading
    # lands after: Quasar's rules out of their layer, its `body.body--dark`
    # painted the ground in Dark Reader's shade of Quasar's own grey until a
    # reload (4.9.135, 2026-11-10). So the script also turns off each style
    # Dark Reader adds later. Off, not removed: a removed copy came back
    # every 2 s, all 1150 rules.
    ui.add_head_html('<meta name="darkreader-lock"><script>'
                     'document.querySelectorAll(".darkreader--fallback").forEach(e => e.remove());'
                     'new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n =>'
                     ' n.matches?.("style.darkreader") && (n.media = "not all"))))'
                     '.observe(document.documentElement, {childList: true, subtree: true})'
                     '</script>')
    ui.add_head_html(f"<style>{DARK_CSS}{kind_css(ctx.kinds)}</style>")
    ui.add_head_html(_KEYS_JS)
    ui.add_head_html(_DROP_JS)
    ui.add_head_html(_LINKS_JS)
    ui.add_head_html(_APP_JS)
    ui.add_head_html(_BADGE_JS)
    ui.add_head_html(_TIPS_JS)
    ui.add_head_html(_PEEK_JS)
    ui.add_head_html(_BOARD_JS)
    ui.add_head_html(_PALETTE_JS)
    # Scripts off, as a blocker leaves a page: nothing is drawn, and NiceGUI's
    # notice says only that the page needs JavaScript. This one also says what
    # symbion is. The template styles that notice after this HTML: only
    # !important hides it.
    # An agent's fetch runs no script and drops <noscript>, but reads text
    # hidden by CSS: sb-about tells it what symbion is, and <noscript> shows it.
    ui.add_body_html('<noscript><style>#esm-fallback{display:none!important}'
                     '#sb-about{display:block!important}</style></noscript>'
                     '<div id="sb-about" style="display:none"><div style="margin:30vh auto 0;'
                     'max-width:40em;padding:0 16px;text-align:center;color:var(--q-body)">'
                     '<p>symbion\'s GUI needs JavaScript. Allow scripts for this site, then '
                     f'reload.</p><p>{ABOUT} An agent reads a store with the '
                     '<code>symbion</code> command, not this page. Source, install and docs: '
                     f'<a href="{source_href("noscript")}">{SOURCE}</a></p></div></div>')
    heads = S.heads(S.load(ctx.store_dir))
    # show-if-above: open beside the page on a wide window, a slide-over behind
    # the menu button on a narrow one (open by default, it covered a phone).
    # Not value=None: its open-state probe waits 1 s on each connect and logs a
    # traceback when the visitor has left or the tab sleeps. Quasar reads
    # show-if-above at mount and sends "open" itself when wide.
    # No-swipe-backdrop: Quasar pans the dimmed page with the mouse in any
    # direction, and a click there that moved a few px closed nothing.
    with ui.left_drawer(value=False, fixed=True, bordered=False) \
            .props(f"show-if-above width=248 breakpoint={NARROW} no-swipe-backdrop") \
            .classes("sb-side") as drawer:
        _sidebar(ctx, author, name, heads, here)
    with ui.header(elevated=False).classes("sb-header"):
        # Dozens of stops came before the first card on every page (the GUI
        # review, 2026-11-07): this one, first, goes past them.
        ui.link("skip to entries · j k move", "#sb-start").classes("sb-skip").mark("skip-link")
        with ui.row(wrap=False).classes("items-center gap-3 w-full sb-topbar") \
                .style("min-height:56px"):
            ui.button(icon="menu", on_click=drawer.toggle, color=None) \
                .props('flat dense round aria-label="menu"').classes("sb-narrow text-body") \
                .mark("menu")
            with ui.row(wrap=False).classes("items-center gap-2 sb-crumbs").mark("crumbs"):
                for i, (label, to) in enumerate(crumbs):
                    if i:
                        # a mark (3.84:1, past the 3:1 a mark needs), so out of a
                        # screen reader's text, which would hold it to 4.5:1
                        ui.label("›").classes("sb-sep").props('aria-hidden="true"')
                    if i < len(crumbs) - 1:
                        ui.link(label, to) if to else ui.label(label)
                        continue
                    with heading(1, "" if to else label).classes("sb-here") as last:
                        (ui.link(label, to) if to else last).mark("page-title")
            ui.space()
            if S.read_only():
                # The public demo: a visitor from a link learns what they see,
                # and where to get it (the spec, 2026-10-22).
                with ui.row(wrap=False).classes("items-center gap-1 text-body sb-demo") \
                        .mark("demo-banner"):
                    # The link named a version and read as a label: few visitors
                    # clicked it (2026-11-05). The sidebar's foot keeps the version.
                    ui.label("read-only demo ·")
                    ui.link("symbion on GitHub", source_href("banner"),
                            new_tab=True).mark("demo-link")
            # Left of New entry, so that button stays put as these come and go
            # (the user, 2026-10-02).
            # the peek is made below; a click on the stale button comes later
            _git_buttons(ctx, author, heads, lambda: show_drift())
            _new_note_button(ctx, author, *new, kind=kind)
            _search_box(q, keep or {}, cards=cards, to=search)
            with ui.element("div").classes("sb-who-top sb-narrow") \
                    .tooltip(f"viewing as {author}: a read-only demo, which writes nothing"
                             if S.read_only() else
                             f"writing as {author}: identity stamped on every write made here"):
                ui.label((author or "?")[:1]).classes("sb-avatar")
                ui.label(author).classes("sb-badge").mark("author-badge-top")
    # Beside the page from PEEK_WIDE up, over it below, where a panel beside
    # it left the cards too narrow to read.
    # No swipe: below the breakpoint Quasar's drawer pans with the mouse too,
    # and handles a pan's last event 50 ms after the release. The release is
    # also a click that closes the peek, and once the drawer is hidden that
    # last event returns before it clears the pan: the backdrop then stayed
    # over the page, clear, and nothing took a click (the user, 2026-11-09).
    kept = _peek_kept()
    with ui.right_drawer(value=bool(kept), fixed=True, bordered=False) \
            .props(f"width={PEEK_WIDTH} breakpoint={PEEK_WIDE - 1} "
                   "no-swipe-open no-swipe-close no-swipe-backdrop").classes("sb-peek") as peek:
        show_drift = _peek_panel(ctx, author, peek, kept)
    # The pick is lit while the peek is open: it stayed lit after the panel
    # was gone (the user, 2026-10-16). The card keeps `sb-picked`, j/k's
    # place; every way to close the peek sets the drawer's value.
    layout = ui.context.client.layout
    if kept:
        layout.classes(add="sb-peeking")
    def peeking(e) -> None:
        if e.value:
            layout.classes(add="sb-peeking")
        else:
            layout.classes(remove="sb-peeking")
            ui.run_javascript("sbPeekUnhash()")     # the URL drops #peek=, and Back is the page's
    peek.on_value_change(peeking)
    # The sidebar runs the full height; the top bar sits beside it. After
    # all three: creating a header or drawer rewrites the layout's view.
    ui.context.client.layout.props('view="lHh LpR lFf"')
    # Where the skip link lands: before the page's own content, out of its gaps.
    ui.element("div").props('id=sb-start tabindex=-1').classes("sb-start").mark("skip-target")
    show_said()


# The peek's width, and the narrowest window it sits beside the page in.
PEEK_WIDTH = 440
PEEK_WIDE = 1280


def _drift_list(ctx, author: str, drawn: dict, arcs: bool) -> None:
    """What the stale button counts, newest first, as compact cards: a click
    on one peeks it."""
    now = S.heads(S.load(ctx.store_dir))
    new, changed = _drift(drawn, now)
    with ui.row(wrap=False).classes("items-center w-full"):
        ui.label(f"{_what(new, changed, arcs) or 'nothing new'} since this page was drawn") \
            .classes("text-body").mark("drift-head")
        ui.space()
        ui.button("reload page", icon="refresh", on_click=lambda: ui.navigate.reload()) \
            .props("flat dense no-caps no-wrap color=notable").classes("shrink-0") \
            .mark("drift-reload")
    rd, git_head = reading(ctx), gitref.head_sha(ctx.cfg)
    with ui.element("div").classes("sb-drift w-full"):
        for n in S.newest_first([n for n in now if drawn.get(n.id) != n]):
            render_note(ctx, n, lambda: None, author=author, show_target=True, compact=True,
                        actions=False, git_head=git_head, seen=rd)


PEEK_COOKIE = "sb-peek"


def _peek_kept() -> str | None:
    """The entry the peek showed as this page was reloaded: `_PEEK_JS` names
    the page's URL and the entry in a cookie as a page unloads, and a link to
    another URL carries it too. Opened once the socket connected, the peek
    slid in over a page drawn without it, and every card wrapped a second
    time (the user, 2026-11-09)."""
    try:
        request = ui.context.client.request
    except RuntimeError:            # a client built with no request
        return None
    at, _, nid = unquote(request.cookies.get(PEEK_COOKIE, "")).partition(" ")
    nid = nid.partition(" ")[0]         # the page's scroll follows, for its script
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return nid if nid and at == here else None


def _peek_panel(ctx, author: str, drawer, kept: str | None = None):
    """The entry `_PEEK_JS` names in an `sbPeek` event, as its own page shows
    it. A write in it reloads the page, as a card's write does, and the page
    comes with the peek on `kept`, that entry, again: redrawn alone, the peek
    left an answered question on the board (the user, 2026-10-16). Returns
    what opens it on `_drift_list` of the entries the page drew, as the
    page's git buttons, made first, recorded them."""
    shown = [kept]
    key = (str(ctx.store_dir), author)

    @ui.refreshable
    def body():
        if shown[0] is _DRIFT:
            seen, arcs, _ = _page_seen(key)
            _drift_list(ctx, author, seen, _arcs_stamp(ctx.store_dir) != arcs["stamp"])
        elif shown[0]:
            peek_entry(ctx, shown[0], author, lambda: ui.navigate.reload())

    with ui.element("div").classes("sb-peek-inner").mark("peek"):
        with ui.row(wrap=False).classes("items-center w-full sb-peek-head"):
            ui.space()
            # `o` clicks it hidden too. A link (Quasar's href): a button that
            # navigated opened nothing on a middle click (the GUI review, 2026-11-07).
            page = ui.button(icon="open_in_new", color=None) \
                .props('flat dense round aria-label="open its page"').classes("sb-peek-page") \
                .tooltip("open its page (o)").mark("peek-page")
            ui.button(icon="close", color=None, on_click=drawer.hide) \
                .props('flat dense round aria-label="close the peek"').classes("sb-peek-close") \
                .tooltip("close (Esc)").mark("peek-close")
        body()

    def show(nid, built: bool = False) -> None:
        shown[0] = nid
        page.set_visibility(nid is not _DRIFT)
        if nid is not _DRIFT and isinstance(nid, str):
            page._props["href"] = href(id=nid)          # set, not parsed: the id is the URL's
            page.update()
            ui.run_javascript(f"sbPeekHash({json.dumps(nid)})")
        if not built:
            body.refresh()
            drawer.show()
    ui.on("sbPeek", lambda e: show((e.args or {}).get("id")))

    client = ui.context.client
    # The first connect only: a reconnect runs `again` too, and the hash is
    # current by then. Kept, it pointed the peek back at `kept` (audit, 2026-11-12).
    first = [kept] if kept else []

    def again() -> None:
        with client:
            if first:
                nid = first.pop()
                # its card, still on the board, keeps j/k's place
                ui.run_javascript(f"sbCards().find(c => c.dataset.id === {json.dumps(nid)})"
                                  "?.classList.add('sb-picked'); sbHold()")
                show(nid, built=True)       # the page came with it drawn
            else:
                ui.run_javascript("sbPeekFromHash()")   # a link with #peek=, or no cookie came
    client.on_connect(again)
    return lambda: show(_DRIFT)


_DRAFT = "sb-draft"
_DRIFT = object()           # the peek's other content: `_drift_list`


def _once(build):
    """An opener for the dialog `build` makes on the first call: one per
    page, so a draft closed with Esc is there when it opens again."""
    made = []

    def _open():
        if not made:
            made.append(build())
        made[0].open()
    return _open


# A dropped text longer than this is offered to the store's docs/: SKILL.md
# puts an entry at about 500 characters and its detail in docs/ (the drop
# design, 2026-10-15). A guess until the user has tried it.
DROP_DOC_CHARS = 2_000


def _dropped(ctx, a: dict, made: dict, open_form) -> None:
    """A file dropped on the page, as `_DROP_JS` sends it: {refused}, or
    {name, text, cursor}, the cursor set while the composer is open. The
    drop design (2026-10-15) has the table this follows."""
    if a.get("refused"):
        ui.notify(a["refused"], type="warning")
        return
    # Its catalog match and doc name said whether a file the repo or docs/
    # holds exists, tracked or not (audit, 2026-11-08).
    if demo_says("opens the dropped file in a new entry"):
        return
    name, text, cursor = a["name"], a["text"], a.get("cursor")
    md = name.lower().endswith((".md", ".markdown"))

    def use_body():
        # Other text goes in as a code block, which the composer's #tag and
        # !kind harvest leaves alone: a dropped CSS file lost `#main`. A
        # textarea holds LF alone, so a CR kept here put the next drop's
        # cursor, counted in the browser, a character off for each.
        flat = text.replace("\r\n", "\n").replace("\r", "\n")
        open_form()
        made["form"].insert(flat if md else fenced(flat, name), cursor, block=not md)

    # Open, or closed with a draft: the drop adds to what is there.
    busy = bool(made) and (made["dialog"].value or made["form"].busy()) \
        or not made and _DRAFT in app.storage.tab       # kept over a reload, not yet built
    # ponytail: runs every catalog on the event loop, about 55 ms here
    # (2026-10-16), as the name field does; run.io_bound if one is slow.
    hits = api.known_files(ctx, name, text)
    if len(hits) == 1:
        open_form()
        if busy:
            made["form"].add_ref(f"{hits[0][0]}:{hits[0][1]}")
        else:
            made["form"].set_target(*hits[0])
        return
    if hits:
        ui.notify(f"{name} matches {len(hits)} files: "
                  + ", ".join(f"{t}:{n}" for t, n in hits[:3]) + ("…" if len(hits) > 3 else ""))
    if len(text) > DROP_DOC_CHARS:
        _offer_doc(ctx, name, text, use_body, made, open_form)
    else:
        use_body()


def _offer_doc(ctx, name: str, text: str, use_body, made: dict, open_form) -> None:
    """The choice for a long dropped text: save it to the store's docs/ and
    ref it from the composer, or use it as the body. The name starts on one
    `save_doc` takes: the file's, or the first free `-N`."""
    try:
        start = api.free_doc_name(ctx, name, text)
    except ValueError:                  # `.env`: no name save_doc takes
        start = ""
    with ui.dialog() as offer, ui.card().classes("bg-panel"):
        ui.label(f"{name}: {len(text):,} characters, long for an entry") \
            .classes("sb-subtitle")
        field = ui.input("name in docs/", value=start).props("dense outlined") \
            .classes("w-full").mark("drop-doc-name")

        def save():
            if demo_says("saves the doc in the store"):
                return
            try:
                saved = api.save_doc(ctx, field.value, text)
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return
            except OSError as e:        # a name the disk refuses: too long, under a file
                ui.notify(f"cannot save docs/{field.value}: {e.strerror or e}", type="negative")
                return
            offer.close()
            open_form()
            made["form"].add_ref(f"doc:{saved}")
        field.on("keydown.enter", save, js_handler=f"(e) => {{ if ({_ENTER}) emit() }}")

        def as_body():
            offer.close()
            use_body()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("cancel", on_click=offer.close).props("flat")
            ui.button("use as body", on_click=as_body).props("flat").mark("drop-body")
            ui.button("save to docs/", on_click=save) \
                .props("unelevated color=primary").mark("drop-save")
    offer.on_value_change(lambda e: e.value or offer.delete())     # one per drop
    offer.open()


def _new_note_button(ctx, author: str, target_type, target_name, arc_id,
                     kind: str | None = None) -> None:
    """The composer, from every page: on the page's object, or the project
    where a page has none. It sat on two pages, as the page's top. From a
    kind= view it starts as that kind and says so (the user, 2026-10-11).
    A file dropped on the page comes here too (`_DROP_JS`)."""
    kind = kind if kind in ctx.kinds else None
    word = kind or "entry"
    made = {}                       # the composer's dialog and handle, once built
    # build() runs where the first opener runs. From a drop's offer, that was
    # a dialog the offer then deleted, and the composer went with it.
    slot = ui.context.slot

    def build():
        added = []                  # by Shift Enter: the page shows them on close
        with slot, ui.dialog() as dialog, ui.card().classes("bg-panel sb-new-card"):
            made["form"] = add_form(ctx, target_type, target_name,
                                    lambda: ui.navigate.reload(), author=author,
                                    arc_id=arc_id, another=lambda: added.append(1),
                                    start_kind=kind, close=dialog.close)
            if draft := app.storage.tab.get(_DRAFT):     # in `slot`: the tab is the page's
                made["form"].restore(draft)

        def moved(e):
            # A draft closed with Esc outlives the reload a card's write
            # brings (audit, 2026-10-28); open, it is the form's again.
            app.storage.tab.pop(_DRAFT, None)
            # The read-only demo keeps nothing a visitor types: a tab's storage
            # lives 30 days (audit, 2026-11-08).
            if not e.value and made["form"].busy() and not S.read_only():
                app.storage.tab[_DRAFT] = made["form"].state()
            if added and not e.value:
                ui.navigate.reload()
        dialog.on_value_change(moved)
        made["dialog"] = dialog
        return dialog

    open_form = _once(build)
    ui.on("sbDrop", lambda e: _dropped(ctx, e.args, made, open_form))
    with ui.button(icon="add", on_click=open_form) \
            .props(f'unelevated dense no-caps color=primary aria-label="new {word}"') \
            .classes("px-2 sb-new").mark("new-note").tooltip(f"new {word} (n)"):
        ui.label(f"New {word}").classes("sb-btn-word")


def _keys_dialog(ctx):
    with ui.dialog() as dialog, ui.card().classes("bg-panel"):
        ui.label("keyboard").classes("sb-stat-label")
        with ui.element("div").classes("sb-keys-list").mark("keys-list"):
            for key, what in _KEYS + (("!kind", "in an entry: sets its kind, one of "
                                       + ", ".join(ctx.kinds)
                                       + "; its first letters do, while one kind "
                                         "alone starts with them"),):
                ui.label(key).classes("sb-chip")
                ui.label(what)
    return dialog


HELP = Path(__file__).resolve().parents[1] / "data" / "help.md"
# The help's kinds are the store's. The block between these lines holds the
# defaults in plain words, for a reader of the file itself; the GUI puts the
# store's kinds there, each with its `when`. The words are the store's, in
# symbion.toml, for wherever they show (the user, 2026-10-17); for a day the
# page kept the file's own prose for a default kind.
_KINDS_BLOCK = re.compile(r"<!-- kinds -->.*?<!-- /kinds -->", re.S)


def kind_does(k) -> str:
    """A kind's properties in words."""
    return ("parked" if k.parked else
            "open until judged, then records the verdict" if k.status and k.verdict else
            "open until resolved" if k.status else "records a run" if k.verdict else "record")


def kind_line(label: str, k) -> str:
    """A kind as a line of the help: its properties in words, then what the
    store says it is for."""
    return f"- **{label}** ({kind_does(k)})" + (f": {k.when}" if k.when else "")


def help_text(ctx) -> str:
    text = HELP.read_text(encoding="utf-8")
    block = _KINDS_BLOCK.search(text).group(0)
    lines = [kind_line(label, k) for label, k in ctx.kinds.items()]
    return text.replace(block, "This store's kinds:\n\n" + "\n".join(lines))


def _help(ctx) -> None:
    text = help_text(ctx)
    viewer("help", lambda: ui.markdown(text, extras=FILE_MD_EXTRAS).mark("file-view"))


SOURCE = "https://github.com/phreakocious/symbion"
BANNER = "https://raw.githubusercontent.com/phreakocious/symbion/main/docs/images/banner.png"


def source_href(link: str) -> str:
    """A link to the source. On a read-only serve, the public demo, by way of
    /go/github: its proxy's access log then keeps each click out, which
    nothing else sees, and `?from=` names the link there (the user,
    2026-11-03)."""
    return f"/go/github?from={link}" if S.read_only() else SOURCE


ABOUT = ("symbion is a notebook and ticket registry for one project, shared by you and your "
         "coding agent.")

# The sidebar's places: (label, path, Material icon).
_PLACES = (("Summary", "/", "dashboard"), ("All entries", "/notes", "notes"),
           ("Targets", "/targets", "my_location"), ("Tags", "/tags", "tag"),
           ("Arcs", "/arcs", "linear_scale"), ("Docs", "/docs", "description"),
           ("Map", "/map", "view_quilt"))


def _sidebar(ctx, author: str, name: str, heads, here: str) -> None:
    """Brand, places, an open count per kind that has a status, the parked
    ones under their own head (an open idea showed nowhere: the user,
    2026-10-02), a row count per kind with none, and who is writing. The badge is not decoration: see the
    module docstring."""
    with ui.element("div").classes("sb-side-inner"):
        with ui.link(target="/").classes("sb-brand").mark("brand-link"):
            ui.html(LOGO_SVG, sanitize=False)
            with ui.column().classes("gap-0"):
                ui.label(name).classes("sb-brand-name").mark("brand")
                ui.label(f"symbion · {summ._count(len(heads), 'entry')}").classes("sb-brand-sub")
        # Only the groups scroll: the badge sat below them, cut off in an
        # 800px window once the RECORDS group came (2026-10-06).
        with ui.element("div").classes("sb-scroll"):
            with ui.element("nav").classes("sb-nav").props('aria-label="places"'):
                for label, path, icon in _PLACES:
                    with ui.link(target=path).classes(
                            "sb-nav-item" + (" sb-on" if path == here else "")) \
                            .mark(f"place-{path.strip('/') or 'home'}"):
                        ui.icon(icon)
                        ui.label(label)
            for head, parked in (("open", False), ("parked", True)):
                kinds = [k for k, spec in ctx.kinds.items() if spec.status and spec.parked == parked]
                if not kinds:
                    continue
                with ui.element("div").classes("sb-nav"):
                    ui.label(head).classes("sb-nav-head")
                    for k in kinds:
                        _kind_item(k, len(summ.open_notes(heads, k)),
                                   href(kind=k, status="open"), f"open-{k}", ctx.kinds[k])
            # The kinds with no status, which showed nowhere here (the user,
            # 2026-10-02): no open count, so each shows its rows.
            plain = [k for k, spec in ctx.kinds.items() if not spec.status]
            if plain:
                with ui.element("div").classes("sb-nav"):
                    ui.label("records").classes("sb-nav-head")
                    for k in plain:
                        _kind_item(k, sum(h.kind == k for h in heads), href(kind=k), f"kind-{k}",
                                   ctx.kinds[k])
            _closed(heads)
            _other_stores(ctx)
        with ui.element("div").classes("sb-who"):
            ui.label((author or "?")[:1]).classes("sb-avatar")
            ro = S.read_only()
            ui.label(f"{'viewing' if ro else 'writing'} as {author}").classes("sb-badge") \
                .mark("author-badge") \
                .tooltip("whose view this is; the demo writes nothing" if ro
                         else "identity stamped on every write made here")
            with ui.element("div").classes("sb-who-icons"):
                ui.button(icon="keyboard", on_click=_once(lambda: _keys_dialog(ctx))) \
                    .props('flat dense round size=sm aria-label="keyboard shortcuts"') \
                    .classes("sb-keys").mark("keys").tooltip("keyboard shortcuts (?)")
                ui.button(icon="help_outline", on_click=lambda: _help(ctx)) \
                    .props('flat dense round size=sm aria-label="how symbion works"') \
                    .classes("sb-help").mark("help-open").tooltip("how symbion works")
        ui.link(f"symbion {metadata.version('symbion')}", source_href("sidebar"), new_tab=True) \
            .classes("sb-source").mark("source").tooltip("source on GitHub")
        # Shown only under html.sb-untabbed (the head script). A page cannot
        # link to chrome://, so the flag is text, selected whole on a click.
        ui.html("tab strip off: <code>chrome://flags/#enable-desktop-pwas-tab-strip</code>",
                sanitize=False).classes("sb-tabhint").mark("tab-hint") \
            .tooltip("Chrome opens this app without tabs. Turn this flag on, then relaunch the app.")


def _kind_item(k: str, n: int, target: str, mark: str, spec) -> None:
    """A kind, its count, and in a tooltip what it is for, as the user's
    design draws it: its name and properties, then its `when`. The words are
    the store's, from symbion.toml, as its colour is (the user, 2026-10-16);
    for a day they were split back out of the help's prose."""
    with ui.link(target=target).classes(
            f"sb-nav-item {kind_class(k)}" + ("" if n else " sb-zero")).mark(mark):
        ui.element("span").classes("sb-dot")
        ui.label(k)
        ui.label(str(n)).classes("sb-nav-n")
        if spec.when:                            # beside it: below, it hid the next kind
            with ui.tooltip().props('anchor="center right" self="center left"') \
                    .classes("sb-kindtip"):
                ui.label(f"{k} · {kind_does(spec)}").classes("sb-kindtip-head")
                ui.label(spec.when[:1].upper() + spec.when[1:])


def _closed(heads, n: int = 5) -> None:
    """The newest rows resolved, each a link to itself: a resolve writes a
    new head, so its stamp is when the row closed."""
    rows = [h for h in S.newest_first(heads) if S.read_status(h) == "resolved"][:n]
    if not rows:
        return
    with ui.element("div").classes("sb-nav"):
        ui.link("recently closed", href(status="resolved")).classes("sb-nav-head") \
            .mark("closed-all").tooltip("every resolved entry")
        for r in rows:
            text = r.body or (f"{r.target.type}: {r.target.name}"
                              if r.target.name else r.target.type)
            with ui.link(target=href(id=r.id)) \
                    .classes(f"sb-nav-item sb-closed {kind_class(r.kind)}") \
                    .mark("closed-item").tooltip(summ.clip(text, summ.BODY_CHARS)):
                ui.element("span").classes("sb-dot")
                ui.label(summ.clip(text, 120)).classes("sb-closed-text")
                ui.label(ago(r.created_at)).classes("sb-nav-n")


def _other_stores(ctx) -> None:
    """Every other store a `serve` runs on here, a link to its notebook: a
    click switches, a cmd-click opens it beside this one. Named by the
    store's directory, `<repo>-notes` less its suffix: the repo a serve was
    started from need not be the store's (`--dir`), and two read the same."""
    here = str(ctx.store_dir.resolve())
    others = {r["store"]: r for r in servers.running() if r["store"] != here}
    if not others or S.read_only():         # a read-only serve's visitors see no other store
        return
    with ui.element("div").classes("sb-nav"):
        ui.label("other stores").classes("sb-nav-head")
        for path, r in sorted(others.items()):
            with ui.link(target=r["url"]).classes("sb-nav-item").mark("store-item") \
                    .tooltip(f"{path}\n{r['url']}"):
                ui.icon("swap_horiz")
                ui.label(servers.name(path))


def _search_box(q: str, keep: dict, *, cards: bool = True, to=None) -> None:
    """Typing filters the cards on this page, in the browser; Enter searches
    the whole store (the user, 2026-10-01), and so does a pause once the
    filter hides every card (2026-10-05), on a page of entry cards only
    (2026-10-29). `to` maps the words to the URL of a page that narrows
    itself by them, as the map does: there typing filters nothing."""
    narrows = to is not None
    to = to or (lambda text: href(**{**keep, "q": text}))
    long, short = (("Search · Enter narrows the page", "Search") if narrows
                   else ("Filter · Enter searches all", "Filter or search"))
    # From 601 to NARROW px the box gets what the bar leaves, 129px at 640, and
    # the long text read "Filter · Enter searchi" (the user's pick, 2026-11-12).
    # shortcut: read at each render, so a resize across NARROW keeps the old text
    # until the next page; a matchMedia listener if that matters.
    box = ui.input(placeholder=long, value=q) \
        .props(f'''dense outlined clearable aria-label="search" :placeholder="matchMedia('(min-width: 601px) and (max-width: {NARROW}px)').matches ? '{short}' : '{long}'"''') \
        .classes("sb-search").mark("search")
    with box.add_slot("prepend"):
        ui.icon("search", size="xs")
    with box.add_slot("append"):
        ui.label("/").classes("sb-chip sb-mono").style("line-height:16px;padding:0 5px")

    def _go(e):
        # The field's own text: the key can reach the server before the value.
        text = e.args if isinstance(e.args, str) else box.value
        ui.navigate.to(to((text or "").strip() or None))

    def _expand(e):
        # The page that shows this search can hide all its cards: an id hit's
        # card lacks the id's text. Reloading it would change nothing.
        if (e.args if isinstance(e.args, str) else "").split() != q.split():
            _go(e)

    if cards and not narrows:
        box.on("update:value", _expand, js_handler="(v) => sbFilter(v, emit)")
    elif not narrows:
        box.on("update:value", js_handler="(v) => sbFilter(v)")
    box.on("keydown.enter", _go, js_handler=f"(e) => {{ if ({_ENTER}) emit(e.target.value) }}")
    words = ('every word, in any case, in body, target, checked, result, refs or tags; '
             'a "quoted phrase" as one word')
    box.tooltip(f"Enter narrows this page to the entries that hold {words}" if narrows else
                "typing filters this page" +
                (", and searches the store when nothing here matches" if cards else "") +
                f"; Enter searches the store: {words}; or an entry id")


# An Enter that acts, in a field's js_handler: not the one that ends an IME
# word, which sent a half-typed search or name (the GUI review, 2026-11-07).
# Safari sends that Enter after compositionend, as keyCode 229.
_ENTER = "!e.isComposing && e.keyCode !== 229"

# Seconds between reads of the store's git state for the two buttons; while
# the tab is hidden, the slower one.
_GIT_EVERY = 5
_HIDDEN_EVERY = 60

# (store, writer) -> {id: entry} of the entries the writer's latest page
# drew. The app badge counts what others wrote since. ponytail: one serve's
# memory; after a restart it counts from the next page drawn.
_DRAWN: dict = {}


def _arcs_stamp(store_dir):
    try:
        st = S.arcs_path(store_dir).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _page_seen(key):
    """(drawn, arcs, writes): what this page's git buttons recorded for `key`, or None.
    Kept on the page's client: by (store, writer) alone, a second tab's draw
    took the first's, which then lit its own writes as news (audit, 2026-11-08)."""
    try:
        seen = getattr(ui.context.client, "sb_seen", None)
    except RuntimeError:                    # no page: a background task
        return None
    return seen[1:] if seen and seen[0] == key else None


@contextmanager
def own_write(ctx, author: str, ids=()):
    """Wrap the page's own write: it is no news to the page, so the stale
    button lights for others' writes only. The arcs stamp is adopted only if
    arcs.jsonl was as the page last recorded it before the write; otherwise
    an arc change the poll has not seen stays offered. The entries `ids`
    count as drawn on this page alone: another tab of the writer's drew them
    before the write, and lights for it. Counted on the writer's latest page
    too, the newer tab kept its old card and lit nothing (audit, 2026-11-12);
    the app badge counts other writers' heads, so it never needed them.
    ponytail: an entry in `ids` another writer changed in the same instant
    counts as drawn. A tick reads in a thread while the write runs on the
    loop: one that read between the write and this mark lit the page's own
    write for that tick (2026-11-12), so the write is counted, and a tick
    that read across it drops what it read."""
    page = _page_seen((str(ctx.store_dir), author))
    before = _arcs_stamp(ctx.store_dir)
    try:
        yield
    finally:
        if page is not None:
            page[2][0] += 1
    if page is None:
        return
    drawn, arcs, _ = page
    if before == arcs["stamp"]:
        arcs["stamp"] = _arcs_stamp(ctx.store_dir)
    drawn.update({n.id: n for n in S.heads(S.load(ctx.store_dir)) if n.id in ids} if ids else {})


def _drift(drawn: dict, now) -> tuple[int, int]:
    """(new, changed) between the entries a page drew, {id: entry}, and the
    heads `now`: entries it lacks, and entries edited or taken back since.
    Whole entries compare: an edit and an undo each keep the id."""
    new = sum(n.id not in drawn for n in now)
    changed = sum(n.id in drawn and drawn[n.id] != n for n in now)
    return new, changed + len(drawn.keys() - {n.id for n in now})


# A top-bar tip lists entries, a line each: a page of them was more than a glance.
_TIP_LINES = 8
_TIP_WORDS = 40


def _did(prev, n) -> str:
    """What turned `prev`, an earlier version of entry `n` (None: there was
    none), into `n`, as a tip says it."""
    if prev is None:
        return "new"
    was, now = S.read_status(prev), S.read_status(n)
    if was != now:
        return {"resolved": "resolved", "open": "reopened"}.get(now, f"now {now}")
    if n.body != prev.body:
        return "edited"
    return summ.change_words(prev, n) or "changed"


def _tip_line(n, did: str, who: str | None = None, eid: str | None = None) -> str:
    """`eid`, the entry's id where `n` is an earlier version of it."""
    return " · ".join([*([who] if who else []), did, f"{n.kind} {short_id(eid or n.id)}",
                       summ.clip(n.body, _TIP_WORDS)])


def _capped(lines: list[str]) -> list[str]:
    cut = lines if len(lines) <= _TIP_LINES else lines[:_TIP_LINES - 1]
    return cut + ([f"+{len(lines) - len(cut)} more"] if len(cut) < len(lines) else [])


def _drift_tip(drawn: dict, now, arcs: bool) -> str:
    """The stale button's tip: each entry written since the page was drawn,
    who wrote it and what they did, newest first. Its count alone said
    little: we know what changed (the user, 2026-11-07)."""
    lines = [_tip_line(n, _did(drawn.get(n.id), n), n.author)
             for n in S.newest_first([n for n in now if drawn.get(n.id) != n])]
    lines += [_tip_line(drawn[i], "taken back") for i in drawn.keys() - {n.id for n in now}]
    return "\n".join(["since this page was drawn:", *_capped(lines),
                      *(["arcs changed"] if arcs else []), "click to list them"])


def _commit_tip(ctx, author: str) -> str:
    """The commit button's tip: each entry it would commit and what the
    writer did to it since the store's last commit. An entry is new when its
    oldest pending version supersedes nothing: a version an edit kept is
    pending only when the text it holds was (store.added_rows)."""
    rows = S.pending_rows(ctx.store_dir)
    keep = S.with_bases(rows, lambda r: r.get("author") == author)
    mine = [r for r in rows if keep(r)]
    pend = {r["id"]: r for r in mine if r.get("id")}
    loaded = S.load(ctx.store_dir)
    by_id, tip = {n.id: n for n in loaded}, S.chain_tips(loaded)
    did = {}
    for r in S.entries(mine):
        if (n := by_id.get(r.get("id"))) is None:
            continue                    # a line that did not parse: commit takes it as it is
        base = r
        while base.get("supersedes") in pend:
            base = pend[base["supersedes"]]
        did[n.id] = _did(by_id.get(base.get("supersedes")), n)
    # Another writer's later version leaves the commit an earlier one, whose
    # saved id no one knows the entry by.
    lines = [_tip_line(n, did[n.id], eid=tip(n.id).id)
             for n in S.newest_first(by_id[i] for i in did)]
    others = len(S.entries([r for r in rows if not keep(r)]))
    arcs = gitref.registry_modified(ctx.store_dir)
    head = f"commit your {summ._count(len(lines), 'entry')}:" if lines else "commit the arcs' changes"
    return "\n".join([head, *_capped(lines), *(["and the arcs' changes"] if lines and arcs else []),
                      *([f"left for their writers: {summ._count(others, 'entry')}"] if others else []),
                      *(["then pull from origin and push"] if S.shared(ctx.store_dir) else [])])


def _tip_on_hover(el, text) -> None:
    """`el`'s tip, made by `text()` when the pointer or focus reaches it, so it
    is current then and costs no poll a read (chrome._TIPS_JS shows it)."""
    el._props["data-tip-key"] = "hover"

    async def fill() -> None:
        el._props["data-tip"] = await run.io_bound(text)
        el.update()
    el.on("mouseenter", fill).on("focus", fill)


def git_state(ctx, author: str) -> tuple:
    """(the writer's pending entries, other writers', a changed registry,
    commits not on the remote, whose the undo level is): what the top bar's undo,
    commit and push buttons show, and the palette's actions."""
    rows = S.pending_rows(ctx.store_dir)
    keep = S.with_bases(rows, lambda r: r.get("author") == author)
    mine = len(S.entries([r for r in rows if keep(r)]))
    return (mine, len(S.entries([r for r in rows if not keep(r)])),
            gitref.registry_modified(ctx.store_dir),
            gitref.unpushed(ctx.store_dir), S.undo_state(ctx.store_dir))


def _git_buttons(ctx, author: str, heads, on_stale) -> None:
    """Undo, commit and push, read again every `_GIT_EVERY` seconds and redrawn
    when a count moves: a commit or push made at a terminal left them on the
    page, and rows written there never showed the commit button (the user,
    2026-10-01). The button counts and commits `author`'s rows only: it took
    an agent's pending rows under its own message (the user, 2026-10-10:
    "serve should only be allowed to commit its own rows").

    The same read finds entries written since `heads`, the ones this page
    drew, and offers a reload; it also sets the app badge. An entry an agent
    wrote showed only after a reload, and nothing said one was due (the
    user's pick, 2026-10-16)."""
    drawn = {n.id: n for n in heads}
    key = (str(ctx.store_dir), author)
    _DRAWN[key] = drawn
    seen = {"stamp": None, "heads": heads}
    arcs = {"stamp": _arcs_stamp(ctx.store_dir)}
    writes = [0]                              # the page's own writes, counted by own_write
    ui.context.client.sb_seen = (key, drawn, arcs, writes)

    def store_state():
        # The file is parsed again only when it changed: every page ticks.
        try:
            st = S.notes_path(ctx.store_dir).stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != seen["stamp"]:
            seen["stamp"], seen["heads"] = stamp, S.heads(S.load(ctx.store_dir))
        now, latest = seen["heads"], _DRAWN.get(key, drawn)
        badge = sum(n.author != author and latest.get(n.id) != n for n in now)
        return (*_drift(drawn, now), _arcs_stamp(ctx.store_dir) != arcs["stamp"]), badge

    def read():
        return (*git_state(ctx, author), *store_state())

    @ui.refreshable
    def buttons(state):
        if any(state[5]):
            _stale_button(*state[5], on_stale,
                          lambda: _drift_tip(drawn, seen["heads"], state[5][2]))
        if state[4] == "mine":
            # No label to compute: the dialog says what the undo takes back.
            ui.button(icon="undo", on_click=lambda: undo_dialog(ctx, author)) \
                .props('flat dense round aria-label="undo your last write"') \
                .classes("sb-undo").mark("undo-button").tooltip("undo your last write")
        elif state[0] and state[4] == "other":
            # The person's write is uncommitted, but the one level is another
            # writer's: the icon had gone, and the user looked for it
            # (2026-10-16). A click shows undo's refusal, which names the
            # write that holds the level. With no level (a commit ended it) a
            # click could say only "nothing to undo" (the user, 2026-11-08).
            ui.button(icon="undo", on_click=lambda: undo_dialog(ctx, author)) \
                .props('flat dense round aria-label="why your last write cannot be undone"') \
                .classes("sb-undo sb-undo-gone").mark("undo-gone") \
                .tooltip("your last write can no longer be undone: click to see why")
        _commit_button(ctx, author, *state[:3])
        _discard_button(ctx, author, *state[:3])
        _push_button(ctx, state[3])

    shown = [read()]
    buttons(shown[0])
    # Reload appeared with no word to a screen reader (the GUI review,
    # 2026-11-07): this says what it says, where no one sees it.
    said = ui.label().classes("sb-sr").props("role=status").mark("stale-said")
    sent = [None]                             # the first tick sends the badge

    async def tick():
        began = writes[0]
        state = await run.io_bound(read)      # git, off the event loop
        if state is None:                     # the app is stopping
            return
        if writes[0] != began:                # the page's own write ran during the read
            return
        if state[:6] != shown[0][:6]:
            buttons.refresh(state)
            new, changed, arcs = state[5]
            said.set_text(f"{_what(new, changed, arcs)} since this page was drawn: reload"
                          if new + changed or arcs else "")
        shown[0] = state
        if state[6] != sent[0]:
            sent[0] = state[6]
            ui.run_javascript(f"sbBadge({state[6]})")
    if not S.read_only():           # nothing changes there: only anonymous load
        # A hidden tab polled as often as a shown one (the GUI review,
        # 2026-11-07). It slows, never stops: the installed app's badge is set
        # from this tick, and a hidden window is when it counts. The timer
        # keeps its pace: NiceGUI reads `interval` when a sleep ends, so a tab
        # shown again waited out the hidden one's minute (audit, 2026-11-08).
        away = [None]                       # when a hidden tab last read

        async def every() -> None:
            if away[0] is not None:
                if time.monotonic() - away[0] < _HIDDEN_EVERY:
                    return
                away[0] = time.monotonic()
            await tick()
        ui.timer(_GIT_EVERY, every, immediate=False).mark("git-poll")  # the page has just read it

        async def hidden(e) -> None:
            away[0] = time.monotonic() if e.args else None
            if not e.args:
                await tick()                # shown again: the bar is read now
        ui.on("sbHidden", hidden)


def _what(new: int, changed: int, arcs: bool = False) -> str:
    return " and ".join([*([summ._count(new, "new entry")] if new else []),
                         *([summ._count(changed, "changed entry")] if changed else []),
                         *(["arcs changed"] if arcs else [])])


def _stale_button(new: int, changed: int, arcs: bool, on_click, tip) -> None:
    """The store changed since this page drew it: another writer, a terminal,
    another tab. The page does not redraw by itself: the cards would move
    under the reader. A click lists those entries in the peek, with the
    reload at its top: a reload redraws only what the page filters for, so
    on an arc or a search it showed none of them (the user, 2026-10-27).
    The count is of entries: a changed arcs file read "1 arcs changed"."""
    word = "changed" if not new else "new or changed" if changed else "new"
    with ui.button(icon="refresh", on_click=on_click) \
            .props("flat dense no-caps color=notable").classes("px-2 sb-stale") \
            .mark("stale-button") as b:
        _tip_on_hover(b, tip)
        if new + changed:
            ui.label(str(new + changed))
        ui.label(f"{word} · arcs" if arcs and new + changed else "arcs changed" if arcs else word) \
            .classes("sb-btn-word")


def _synced(ctx, s) -> tuple[list[str], bool]:
    """What api.sync did, as a shared store's `commit` says it at a terminal
    (cli._pull_then_push), less the commands a terminal runs: the pull's
    lines, then the push's, and whether the push waits."""
    p = s.pulled
    said = [*filter(None, [p.recovered]), *(merge.said_split(kept, new) for kept, new in p.splits),
            *p.dropped, *p.notices(ctx)]
    if p.conflicts:
        return [*said, f"not pushed: git cannot merge {' '.join(p.conflicts)}; `symbion pull` "
                       f"at a terminal names the commands that finish the merge"], True
    if p.error:
        return [*said, f"not pushed: the pull failed: {p.error}"], True
    if p.merged:
        said.append(f"pulled {summ._count(p.merged, 'commit')} from {p.upstream}")
    if s.timed_out:
        return [*said, "not pushed: the push ran too long and was stopped"], True
    if s.pushed.returncode:
        why = s.pushed.stderr.strip() or f"git push exited {s.pushed.returncode}"
        return [*said, f"push failed: {why}"], True
    return [*said, f"pushed {summ._count(s.carried, 'commit')} to origin"], False


def _commit_button(ctx, author: str, n: int, others: int, registry: bool) -> None:
    """The store is a sibling git repo. A GUI that writes but cannot commit
    only grows the number the SessionStart hook nags about. In a shared
    store a commit pulls, then pushes, as `commit` does at a terminal."""
    if not n and not registry:
        return
    shared = S.shared(ctx.store_dir)

    async def _do():
        if demo_says("commits the store"):
            return
        try:
            ok = api.commit(ctx, "entries: via symbion serve", author=author)
        except (RuntimeError, S.NoSide) as e:     # a hook refused: no reload, so it stays read
            ui.notify(str(e), type="negative", multi_line=True,
                      close_button="dismiss", timeout=0)
            return
        said, failed = [gitref.committed(ctx.store_dir) if ok else "nothing to commit"], False
        if ok and shared and gitref.unpushed(ctx.store_dir) is not None:
            try:                                  # a thread: the pull and push wait on origin
                more, failed = _synced(ctx, await run.io_bound(api.sync, ctx))
            except Exception as e:                # the commit is made: say why the push waits
                more, failed = [f"not pushed: {str(e) or type(e).__name__}"], True
            said += more
        say_after_reload("\n".join(said), failed)
        ui.navigate.reload()

    # On a phone the word goes and the count stays: the icon says commit.
    with ui.button(icon="save", on_click=_do).props("outline dense no-caps color=notable") \
            .classes("px-2 sb-commit").mark("commit-button") as b:
        _tip_on_hover(b, lambda: _commit_tip(ctx, author))
        ui.label("commit").classes("sb-btn-word")
        ui.label(str(n) if n else "registry")


def _discard_button(ctx, author: str, n: int, others: int, registry: bool) -> None:
    """Commit's other way, beside it: throw away what the commit would take
    for you, after a dialog lists it (notes.discard_dialog). Quiet, as push
    is: a commit is the usual end of a write."""
    if not n and not registry:
        return
    ui.button(icon="delete_sweep", on_click=lambda: discard_dialog(ctx, author)) \
        .props('flat dense round color=muted aria-label="discard your uncommitted work"') \
        .classes("sb-discard").mark("discard-button") \
        .tooltip("discard your uncommitted work: a list first, and undo brings it back")


def _push_button(ctx, n: int | None) -> None:
    """Committed is not off this disk. Quieter than commit: nothing here is
    at risk of being lost by a crash, only by the disk. In a shared store it
    pulls first, as a commit does: a bare push is refused once origin moves."""
    if not n:                       # None: no remote, nowhere to push
        return
    shared = S.shared(ctx.store_dir)

    async def _do():
        # A thread: a push waits on the network, and the event loop serves
        # every open page.
        if demo_says("pushes the store"):
            return
        if shared:
            try:
                said, failed = _synced(ctx, await run.io_bound(api.sync, ctx))
            except Exception as e:
                said, failed = [f"not pushed: {str(e) or type(e).__name__}"], True
            say_after_reload("\n".join(said), failed)
            ui.navigate.reload()
            return
        r = await run.io_bound(api.push, ctx, capture=True)
        if r.returncode:
            ui.notify(r.stderr.strip() or f"git push exited {r.returncode}", type="negative",
                      multi_line=True, close_button="dismiss", timeout=0)
            return
        say_after_reload("pushed")
        ui.navigate.reload()

    with ui.button(icon="cloud_upload", on_click=_do).props("flat dense no-caps color=muted") \
            .classes("px-2 sb-push").mark("push-button") \
            .tooltip(("pull from origin, then push" if shared else "git push the store")
                     + f": {summ._count(n, 'commit')} not on its remote"):
        ui.label("push").classes("sb-btn-word")
        ui.label(str(n))
