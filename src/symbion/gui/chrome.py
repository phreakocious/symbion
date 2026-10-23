"""Per-page shell: head CSS, the top bar, the author badge and the commit
button. The author badge is not decoration -- `serve` is launched from a shell
carrying CLAUDECODE=1, and showing the resolved identity is what makes a
misattribution visible BEFORE it is permanent in an append-only file."""
from __future__ import annotations

import json
import re
from importlib import metadata
from pathlib import Path

from nicegui import app, run, ui

from .. import api, gitref
from .. import store as S
from .. import summary as summ
from . import servers
from .filters import href
from .notes import (FILE_MD_EXTRAS, add_form, ago, demo_says, fenced, kind_class, peek_entry,
                    say_after_reload, show_said, undo_dialog, viewer)
from .theme import (DARK_CSS, FONTS_HTML, LOGO_SVG, NARROW, kind_css, quasar_colors,
                    root_vars_css)

# From anywhere but a field being typed in or an open dialog: `/` focuses the
# search box, `n` and `?` press the buttons they name. What is typed in the
# box hides the cards on the page that lack a word of it (`sbFilter`): every
# word, in any case, in the card's text or its id. When it hides them all, a
# pause in the typing searches the store, as Enter does: a superseded id, or a
# row on no card of this page, read as gone (the user, 2026-10-05).
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
  for (const card of document.querySelectorAll('.sb-main .sb-note')) {
    const hay = (card.textContent + ' ' + (card.dataset.id || ''))
      .toLowerCase().replace(/\\s+/g, ' ');
    card.classList.toggle('sb-filtered', !words.every(w => hay.includes(w)));
  }
  for (const board of document.querySelectorAll('.sb-board'))
    board.classList.toggle('sb-filtered',
      words.length > 0 && !board.querySelector('.sb-note:not(.sb-filtered)'));
  clearTimeout(sbExpandTimer);
  if (words.length && !document.querySelector('.sb-main .sb-note:not(.sb-filtered)'))
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
# page (`sbPeek`, which `_peek_panel` takes); a modified click keeps the
# browser's own new tab, on every card alike (the user, 2026-10-15). The other
# keys press the peek's own buttons, so each does what its button does. Open
# is read from where the drawer sits: it slides off the right edge when closed.
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
  emitEvent('sbPeek', {id: card.dataset.id});
}
function sbPeekOpen() {
  // shell sets sb-peeking from the drawer's value. Its left edge read open
  // when closed on a page that did not scroll, under scrollbar-gutter:
  // stable, and x resolved the entry the hidden peek still held (2026-10-16).
  return !!document.querySelector('.q-layout.sb-peeking');
}
document.addEventListener('click', e => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const t = e.target.closest ? e.target : null;
  const card = t && t.closest('.sb-main .sb-note[data-id]');
  if (!card || getSelection().toString()) return;      // selecting words, not picking
  const control = t.closest('a, button, input, textarea, label, .q-btn');
  if (control && !control.matches('a.sb-note-open')) return;   // a link or control of its own
  if (!t.closest('a.sb-note-open, .sb-note-text, .nicegui-markdown, .sb-verdict')) return;
  e.preventDefault();
  sbPeek(card);
});
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

# NiceGUI's page template hides all that follows its ES-module notice with
# `#esm-fallback ~ *`, and the rule stays after the notice is gone. Every
# element tests it against each of its earlier siblings: cli.py in the file
# viewer, 27,000 spans in one <code>, took 4.5 s a style pass and 24 s to
# paint in headless Chrome; with the rule deleted, a pass took 90 ms
# (2026-10-16). A browser with no ES modules runs none of the page anyway.
_ESM_JS = """<script>
document.addEventListener('DOMContentLoaded', () => {
  for (const s of document.styleSheets) {
    let rules;
    try { rules = s.cssRules; } catch { continue; }        // another origin's sheet
    for (let i = rules.length - 1; i >= 0; i--)
      if (rules[i].selectorText === '#esm-fallback ~ *') s.deleteRule(i);
  }
});
</script>"""

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
  box.setAttribute('aria-label', 'go to, run or find');
  box.setAttribute('aria-controls', 'sb-pal-list');
  box.autocomplete = 'off';
  box.spellcheck = false;
  label.append(sbIcon('search'), box, sbEl('span', 'sb-chip sb-mono', 'esc'));
  const list = sbEl('div', 'sb-pal-list');
  list.id = 'sb-pal-list';
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
  sbPalRows = [];
  sbPalShown = q;
  if (!q) sbPalRest = groups;
  for (const g of groups) {
    // A button the read named can be gone since: the top bar redraws on its own tick.
    const rows = g.rows.filter(r => !r.click || document.querySelector(r.click));
    if (g.rows.length && !rows.length) continue;
    const head = sbEl('div', 'sb-pal-head');
    head.append(sbEl('span', 'sb-nav-head', g.head));
    if (g.aside) head.append(sbEl('span', 'sb-pal-aside', g.aside));
    list.append(head);
    for (const r of rows) list.append(sbPalRow(r, here));
  }
  if (!list.childElementCount)
    list.append(sbEl('div', 'sb-pal-none', `nothing here or in another running store says “${q}”`));
  sbPalPick(0);
}
function sbPalRow(r, here) {
  const row = sbEl(r.href ? 'a' : 'button', 'sb-pal-row' + (r.kind ? ' sb-pal-entry ' + r.kind : '')
                   + (r.done ? ' sb-pal-done' : ''));
  if (r.href) row.href = r.href; else row.type = 'button';
  row.id = 'sb-pal-' + sbPalRows.length;
  row.tabIndex = -1;
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
  sbPalAt = Math.max(0, Math.min(sbPalRows.length - 1, i));
  const row = sbPalRows[sbPalAt], r = row.sbRow;
  row.classList.add('sb-pal-on');
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


def shell(ctx, author: str, crumbs, *, q: str = "", keep: dict | None = None,
          here: str = "", new=("project", None, None), kind: str | None = None) -> None:
    """`crumbs` end in the page's title, which the top bar shows: a title of
    its own under the bar left a band of nothing above it (the user,
    2026-10-01). A parent comes before it only on a page one level down (an
    arc); a "Notebook" before every title said nothing the sidebar does not
    (the user, 2026-10-13). `q` is the search being shown, `keep` the filters a new
    search keeps: on /notes a search narrows the list in view, elsewhere it
    starts one. `here` is the sidebar place this page is (`/`, `/notes`,
    `/tags`, `/arcs`), `new` the (type, name, arc) a new note starts on, and
    `kind` the kind it starts as."""
    ui.dark_mode().enable()
    name = store_name(ctx)
    # The project, then the page: the tab strip truncates from the right, and
    # tabs of several stores share a window. "<store> · symbion" on every
    # page left the tabs of the installed app alike; its window is one store's
    # and names it, so there the tab is the page alone (the user, 2026-10-15).
    page = crumbs[-1][0]
    ui.page_title(f"{name} · {page}")
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
    # script takes it off first.
    ui.add_head_html('<meta name="darkreader-lock"><script>'
                     'document.querySelectorAll(".darkreader--fallback").forEach(e => e.remove())'
                     '</script>')
    ui.add_head_html(f"<style>{DARK_CSS}{kind_css(ctx.kinds)}</style>")
    ui.add_head_html(_KEYS_JS)
    ui.add_head_html(_DROP_JS)
    ui.add_head_html(_LINKS_JS)
    ui.add_head_html(_APP_JS)
    ui.add_head_html(_BADGE_JS)
    ui.add_head_html(_PEEK_JS)
    ui.add_head_html(_PALETTE_JS)
    ui.add_head_html(_ESM_JS)
    # Scripts off, as a blocker leaves a page: nothing is drawn, and NiceGUI's
    # notice blames the browser's ES modules. This says what is missing. The
    # template styles that notice after this HTML: only !important hides it.
    ui.add_body_html('<noscript><style>#esm-fallback{display:none!important}</style>'
                     '<p style="margin:30vh auto 0;max-width:40em;padding:0 16px;'
                     'text-align:center;color:var(--q-body)">symbion\'s GUI needs '
                     'JavaScript. Allow scripts for this site, then reload.</p></noscript>')
    heads = S.heads(S.load(ctx.store_dir))
    # value=None: open beside the page on a wide window, a slide-over behind
    # the menu button on a narrow one (open by default, it covered a phone).
    with ui.left_drawer(value=None, fixed=True, bordered=False) \
            .props(f"width=248 breakpoint={NARROW}").classes("sb-side") as drawer:
        _sidebar(ctx, author, name, heads, here)
    with ui.header(elevated=False).classes("sb-header"):
        with ui.row(wrap=False).classes("items-center gap-3 w-full sb-topbar") \
                .style("min-height:56px"):
            ui.button(icon="menu", on_click=drawer.toggle, color=None) \
                .props('flat dense round aria-label="menu"').classes("sb-narrow text-body") \
                .mark("menu")
            with ui.row(wrap=False).classes("items-center gap-2 sb-crumbs").mark("crumbs"):
                for i, (label, to) in enumerate(crumbs):
                    if i:
                        ui.label("›").classes("sb-sep")
                    last = ui.link(label, to) if to else ui.label(label)
                last.classes("sb-here").mark("page-title")
            ui.space()
            if S.read_only():
                # The public demo: a visitor from a link learns what they see,
                # and where to get it (the spec, 2026-10-22).
                with ui.row(wrap=False).classes("items-center gap-1 text-body sb-demo") \
                        .mark("demo-banner"):
                    ui.label("read-only demo of")
                    ui.link(f"symbion {metadata.version('symbion')}", SOURCE + "#readme",
                            new_tab=True).mark("demo-link")
            # Left of New entry, so that button stays put as these come and go
            # (the user, 2026-10-02).
            _git_buttons(ctx, author, heads)
            _new_note_button(ctx, author, *new, kind=kind)
            _search_box(q, keep or {})
            with ui.element("div").classes("sb-who-top sb-narrow") \
                    .tooltip(f"viewing as {author}: a read-only demo, which writes nothing"
                             if S.read_only() else
                             f"writing as {author}: identity stamped on every write made here"):
                ui.label((author or "?")[:1]).classes("sb-avatar")
                ui.label(author).classes("sb-badge").mark("author-badge-top")
    # Beside the page from PEEK_WIDE up, over it below, where a panel beside
    # it left the cards too narrow to read.
    with ui.right_drawer(value=False, fixed=True, bordered=False) \
            .props(f"width={PEEK_WIDTH} breakpoint={PEEK_WIDE - 1}").classes("sb-peek") as peek:
        _peek_panel(ctx, author, peek)
    # The pick is lit while the peek is open: it stayed lit after the panel
    # was gone (the user, 2026-10-16). The card keeps `sb-picked`, j/k's
    # place; every way to close the peek sets the drawer's value.
    layout = ui.context.client.layout
    peek.on_value_change(lambda e: layout.classes(add="sb-peeking") if e.value
                         else layout.classes(remove="sb-peeking"))
    # The sidebar runs the full height; the top bar sits beside it. After
    # all three: creating a header or drawer rewrites the layout's view.
    ui.context.client.layout.props('view="lHh LpR lFf"')
    show_said()


# The peek's width, and the narrowest window it sits beside the page in.
PEEK_WIDTH = 440
PEEK_WIDE = 1280


def _peek_panel(ctx, author: str, drawer) -> None:
    """The entry `_PEEK_JS` names in an `sbPeek` event, as its own page shows
    it. A write in it reloads the page, as a card's write does, and the page
    opens the peek on the entry again: redrawn alone, the peek left an
    answered question on the board (the user, 2026-10-16)."""
    shown = [None]

    def wrote() -> None:
        app.storage.tab[_PEEKED] = shown[0]
        ui.navigate.reload()

    @ui.refreshable
    def body():
        if shown[0]:
            peek_entry(ctx, shown[0], author, wrote)

    with ui.element("div").classes("sb-peek-inner").mark("peek"):
        with ui.row(wrap=False).classes("items-center w-full sb-peek-head"):
            ui.space()
            ui.button(icon="open_in_new", color=None,
                      on_click=lambda: shown[0] and ui.navigate.to(href(id=shown[0]))) \
                .props('flat dense round aria-label="open its page"').classes("sb-peek-page") \
                .tooltip("open its page (o)")
            ui.button(icon="close", color=None, on_click=drawer.hide) \
                .props('flat dense round aria-label="close the peek"').classes("sb-peek-close") \
                .tooltip("close (Esc)").mark("peek-close")
        body()

    def show(nid) -> None:
        shown[0] = nid
        body.refresh()
        drawer.show()
    ui.on("sbPeek", lambda e: show((e.args or {}).get("id")))

    client = ui.context.client

    def again() -> None:
        with client:
            if nid := app.storage.tab.pop(_PEEKED, None):
                # its card, still on the board, keeps j/k's place
                ui.run_javascript(f"sbCards().find(c => c.dataset.id === {json.dumps(nid)})"
                                  "?.classList.add('sb-picked')")
                show(nid)
    client.on_connect(again)


_PEEKED = "sb-peeked"


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
    busy = bool(made) and (made["dialog"].value or made["form"].busy())
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
        field.on("keydown.enter", save)

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
                                    start_kind=kind)
        dialog.on_value_change(lambda e: added and not e.value and ui.navigate.reload())
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

# The sidebar's places: (label, path, Material icon).
_PLACES = (("Summary", "/", "dashboard"), ("All entries", "/notes", "notes"),
           ("Targets", "/targets", "my_location"), ("Tags", "/tags", "tag"),
           ("Arcs", "/arcs", "linear_scale"), ("Docs", "/docs", "description"))


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
        ui.link(f"symbion {metadata.version('symbion')}", SOURCE, new_tab=True) \
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
    if not others:
        return
    with ui.element("div").classes("sb-nav"):
        ui.label("other stores").classes("sb-nav-head")
        for path, r in sorted(others.items()):
            with ui.link(target=r["url"]).classes("sb-nav-item").mark("store-item") \
                    .tooltip(f"{path}\n{r['url']}"):
                ui.icon("swap_horiz")
                ui.label(servers.name(path))


def _search_box(q: str, keep: dict) -> None:
    """Typing filters the cards on this page, in the browser; Enter searches
    the whole store (the user, 2026-10-01), and so does a pause once the
    filter hides every card (2026-10-05)."""
    box = ui.input(placeholder="Filter · Enter searches all", value=q) \
        .props('dense outlined clearable aria-label="search"').classes("sb-search") \
        .mark("search")
    with box.add_slot("prepend"):
        ui.icon("search", size="xs")
    with box.add_slot("append"):
        ui.label("/").classes("sb-chip sb-mono").style("line-height:16px;padding:0 5px")

    def _go(e):
        # The field's own text: the key can reach the server before the value.
        text = e.args if isinstance(e.args, str) else box.value
        ui.navigate.to(href(**{**keep, "q": (text or "").strip() or None}))

    def _expand(e):
        # The page that shows this search can hide all its cards: an id hit's
        # card lacks the id's text. Reloading it would change nothing.
        if (e.args if isinstance(e.args, str) else "").split() != q.split():
            _go(e)

    box.on("update:value", _expand, js_handler="(v) => sbFilter(v, emit)")
    box.on("keydown.enter", _go, js_handler="(e) => emit(e.target.value)")
    box.tooltip("typing filters this page, and searches the store when nothing here "
                "matches; Enter searches the store: every word, in any case, in body, "
                'target, checked, result, refs or tags; a "quoted phrase" as one word; '
                "or an entry id")


# Seconds between reads of the store's git state for the two buttons.
_GIT_EVERY = 5

# (store, writer) -> {id: entry} of the entries the writer's latest page
# drew. The app badge counts what others wrote since. ponytail: one serve's
# memory; after a restart it counts from the next page drawn.
_DRAWN: dict = {}


def _drift(drawn: dict, now) -> tuple[int, int, list]:
    """(new, changed, by) between the entries a page drew, {id: entry}, and
    the heads `now`: entries it lacks, entries edited or taken back since,
    and who wrote them. Whole entries compare: an edit of the writer's own
    uncommitted entry rewrites its line, and keeps its id and its time."""
    new = [n for n in now if n.id not in drawn]
    changed = [n for n in now if n.id in drawn and drawn[n.id] != n]
    gone = len(drawn.keys() - {n.id for n in now})
    return len(new), len(changed) + gone, sorted({n.author for n in new + changed})


def git_state(ctx, author: str) -> tuple:
    """(the writer's pending entries, other writers', a changed registry,
    commits not on the remote, the undo level): what the top bar's undo,
    commit and push buttons show, and the palette's actions."""
    rows = S.pending_rows(ctx.store_dir)
    keep = S.with_bases(rows, lambda r: r.get("author") == author)
    mine = len(S.entries([r for r in rows if keep(r)]))
    return (mine, len(S.entries([r for r in rows if not keep(r)])),
            gitref.registry_modified(ctx.store_dir),
            gitref.unpushed(ctx.store_dir), S.undoable(ctx.store_dir))


def _git_buttons(ctx, author: str, heads) -> None:
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
        return _drift(drawn, now), badge

    def read():
        return (*git_state(ctx, author), *store_state())

    @ui.refreshable
    def buttons(state):
        if state[5][:2] != (0, 0):
            _stale_button(*state[5])
        if state[4]:
            # No label to compute: the dialog says what the undo takes back.
            ui.button(icon="undo", on_click=lambda: undo_dialog(ctx, author)) \
                .props('flat dense round aria-label="undo your last write"') \
                .classes("sb-undo").mark("undo-button").tooltip("undo your last write")
        elif state[0]:
            # The person's write is uncommitted, but the one level is another
            # writer's, or a commit ended it: the icon had gone, and the user
            # looked for it (2026-10-16). A click shows undo's refusal, which
            # names the write that holds the level.
            ui.button(icon="undo", on_click=lambda: undo_dialog(ctx, author)) \
                .props('flat dense round aria-label="why your last write cannot be undone"') \
                .classes("sb-undo sb-undo-gone").mark("undo-gone") \
                .tooltip("your last write can no longer be undone: click to see why")
        _commit_button(ctx, author, *state[:3])
        _push_button(ctx, state[3])

    shown = [read()]
    buttons(shown[0])
    sent = [None]                             # the first tick sends the badge

    async def tick():
        state = await run.io_bound(read)      # git, off the event loop
        if state is None:                     # the app is stopping
            return
        if state[:6] != shown[0][:6]:
            buttons.refresh(state)
        shown[0] = state
        if state[6] != sent[0]:
            sent[0] = state[6]
            ui.run_javascript(f"sbBadge({state[6]})")
    if not S.read_only():           # nothing changes there: only anonymous load
        ui.timer(_GIT_EVERY, tick, immediate=False).mark("git-poll")    # the page has just read it


def _stale_button(new: int, changed: int, by: list) -> None:
    """The store changed since this page drew it: another writer, a terminal,
    another tab. The page does not redraw by itself: the cards would move
    under the reader."""
    what = " and ".join([*([summ._count(new, "new entry")] if new else []),
                         *([summ._count(changed, "changed entry")] if changed else [])])
    with ui.button(icon="refresh", on_click=lambda: ui.navigate.reload()) \
            .props("flat dense no-caps color=notable").classes("px-2 sb-stale") \
            .mark("stale-button") \
            .tooltip(f"reload this page: {what} since it was drawn"
                     + (f", by {', '.join(by)}" if by else "")):
        ui.label(str(new + changed))
        ui.label("changed" if not new else "new or changed" if changed else "new") \
            .classes("sb-btn-word")


def _commit_button(ctx, author: str, n: int, others: int, registry: bool) -> None:
    """The store is a sibling git repo. A GUI that writes but cannot commit
    only grows the number the SessionStart hook nags about."""
    if not n and not registry:
        return

    def _do():
        if demo_says("commits the store"):
            return
        try:
            ok = api.commit(ctx, "entries: via symbion serve", author=author)
        except RuntimeError as e:     # a hook refused: no reload, so it stays read
            ui.notify(str(e), type="negative", multi_line=True,
                      close_button="dismiss", timeout=0)
            return
        say_after_reload(gitref.committed(ctx.store_dir) if ok else "nothing to commit")
        ui.navigate.reload()

    # On a phone the word goes and the count stays: the icon says commit.
    with ui.button(icon="save", on_click=_do).props("outline dense no-caps color=notable") \
            .classes("px-2 sb-commit").mark("commit-button") \
            .tooltip(f"git commit {author}'s entries" + (
                f"; {summ._count(others, 'entry')} by other writers stay pending for them"
                if others else "")):
        ui.label("commit").classes("sb-btn-word")
        ui.label(str(n) if n else "registry")


def _push_button(ctx, n: int | None) -> None:
    """Committed is not off this disk. Quieter than commit: nothing here is
    at risk of being lost by a crash, only by the disk."""
    if not n:                       # None: no remote, nowhere to push
        return

    async def _do():
        # A thread: a push waits on the network, and the event loop serves
        # every open page.
        if demo_says("pushes the store"):
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
            .tooltip(f"git push the store: {summ._count(n, 'commit')} not on its remote"):
        ui.label("push").classes("sb-btn-word")
        ui.label(str(n))
