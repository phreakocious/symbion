// One entry's card, drawn here from one prop (notes.render_note builds it). A
// card of server elements was about 32 of them, most of a page's cost: this
// one is a single element (measured 2026-10-29). The DOM and its
// classes are the old card's, so theme.py and the page's scripts (the peek,
// j/k, the filter) read it as before.
// `card.main` is the text, in order; `card.foot` the chips; a part's `m` is
// the marker the old element carried, for the tests' reader (tests/_cards.py).
// An arc's checklist box (arcs.checklist) is a card too: its headline is a
// foot chip, its by and age only once ticked, and its tick reads "reopen".
// An open box there has a `grip`, which chrome's _BOARD_JS drags it by.
// A tooltip is a `data-tip` attribute, which chrome's _TIPS_JS shows; one
// with a `key` and no text yet asks the server on the first hover, as the
// check badge's commits always did. A chip with `open` is a button that
// opens the viewer, as a view button does; one with `menu` opens its moves.
// A board box's `move` is its last line: arrows, then who moved it, then its id.
// Every click is one `act` event.
// Markdown goes in through setHTML (NiceGUI's DOMPurify, as ui.markdown);
// a snippet's and a verdict diff's HTML is escaped on the server, as
// ui.html(sanitize=False) took it. Code in a body needs the .codehilite sheet
// ui.markdown serves: a card that has some names it, and the first adds it.

export default {
  template: `
<div>
  <span v-if="card.grip" class="sb-grip" draggable="true" aria-hidden="true" data-tip="drag to move"><q-icon name="drag_indicator" /></span>
  <div class="sb-note-main">
    <template v-for="(p, i) in card.main" :key="i">
      <a v-if="p.k === 'head'" class="sb-note-open" :href="p.h"><div class="sb-note-text"><span class="sb-headline-type">{{ p.type }}</span>{{ p.t }}</div></a>
      <div v-else-if="p.k === 'changes'" class="sb-note-meta" :data-tip="p.tip">{{ p.t }}</div>
      <div v-else-if="p.k === 'verdict' && p.html" class="sb-verdict" v-html="p.html"></div>
      <div v-else-if="p.k === 'verdict'" class="sb-verdict">{{ p.t }}</div>
      <a v-else-if="p.k === 'text' && p.h" class="sb-note-open" :href="p.h">
        <div v-if="p.html" class="sb-note-text sb-snippet" v-html="p.html"></div>
        <div v-else class="sb-note-text">{{ p.t }}</div>
      </a>
      <div v-else-if="p.k === 'text' && p.html" class="sb-note-text sb-snippet" v-html="p.html"></div>
      <div v-else-if="p.k === 'text'" class="sb-note-text">{{ p.t }}</div>
      <div v-else-if="p.k === 'md'" :class="['nicegui-markdown', p.c]" :ref="el => md(el, p.html)"></div>
      <div v-else-if="p.k === 'seam'" class="sb-seam"><div :data-tip="p.tip">{{ p.t }}</div></div>
      <div v-else-if="p.k === 'meas'" class="nicegui-row row gap-3 flex-wrap"><div v-for="x in p.items" class="sb-note-meta">{{ x }}</div></div>
      <div v-else-if="p.k === 'answer'" class="sb-answer">
        <q-btn v-for="o in p.options" outline dense no-caps class="px-2" :label="o.t" :icon="o.icon || undefined" :data-tip="o.tip" @click="act('answer', o.t)" />
        <q-input :model-value="typed" @update:model-value="type" outlined dense class="sb-answer-field" placeholder="or answer in your words · Enter" aria-label="answer this question" @keydown.enter="answer" />
      </div>
      <div v-else-if="p.k === 'options'" class="sb-note-meta">{{ p.t }}</div>
    </template>
    <div class="sb-note-foot">
      <template v-for="(c, i) in card.foot" :key="i">
        <span v-if="c.pair" class="sb-pair">
          <a v-if="c.t" :class="c.c" :href="c.h" :data-tip="c.tip" :data-tip-key="c.key" @mouseenter="ask(c, i)" @focus="ask(c, i)">{{ c.t }}</a>
          <q-btn v-if="c.view" flat dense no-caps round size="xs" color="muted" :aria-label="c.view.tip" :icon="c.view.icon" :data-tip="c.view.tip" @click="e => act('view', i, e.shiftKey)" />
        </span>
        <a v-else-if="c.head" class="sb-note-open" :href="c.h"><div class="sb-note-text"><span class="sb-headline-type">{{ c.type }}</span>{{ c.t }}</div></a>
        <button v-else-if="c.menu" type="button" :class="[c.c, 'sb-chip-opens']" :data-tip="c.tip">{{ c.t }}<q-menu auto-close><q-list dense><q-item v-for="m in c.menu" clickable @click="act('move', m.k, m.t)"><q-item-section>{{ m.t }}</q-item-section></q-item></q-list></q-menu></button>
        <a v-else-if="c.h" :class="c.c" :href="c.h" :data-tip="c.tip">{{ c.t }}</a>
        <button v-else-if="c.open" type="button" :class="[c.c, 'sb-chip-opens']" :data-tip="c.tip" :data-tip-key="c.key" @mouseenter="ask(c, i)" @focus="ask(c, i)" @click="act('view', i)">{{ c.t }}</button>
        <div v-else :class="c.c" :data-tip="c.tip" :data-tip-key="c.key" @mouseenter="ask(c, i)">{{ c.t }}</div>
      </template>
      <template v-if="card.move">
        <div class="sb-move-break"></div>
        <span v-if="card.move.arrows.length" class="sb-move">
          <q-btn v-for="a in card.move.arrows" flat dense round size="xs" color="muted" :class="'sb-mv-' + a.d" :icon="arrows[a.d]" :disable="!a.ok" :aria-label="a.tip" :data-tip="a.tip" @click="act('move', a.k, a.p)" />
        </span>
        <span v-if="card.move.moved" class="sb-note-meta sb-moved" :data-tip="card.move.moved.tip">{{ card.move.moved.t }}</span>
      </template>
      <span class="sb-note-by">
        <a v-if="card.by && card.by.h" class="sb-note-meta" :href="card.by.h">{{ card.by.t }}</a>
        <span v-else-if="card.by" class="sb-note-meta">{{ card.by.t }}</span>
        <div v-if="card.age" class="sb-note-meta sb-age" :data-tip="card.age.tip">{{ card.age.t }}</div>
        <a class="sb-note-meta sb-id" :href="card.sid.h" :data-tip="card.sid.tip">{{ card.sid.t }}</a>
        <q-btn flat dense round size="xs" icon="content_copy" class="sb-copy" aria-label="copy id" data-tip="copy the full id" @click="act('copy')" />
      </span>
    </div>
  </div>
  <q-btn v-if="card.resolve === 'reopen'" flat round dense icon="check" class="sb-resolve sb-resolved" aria-label="reopen" data-tip="reopen" @click="act('resolve')" />
  <q-btn v-else-if="card.resolve" flat round dense icon="check" class="sb-resolve" aria-label="resolve" data-tip="resolve" @click="act('resolve')" />
  <div v-if="card.star !== null" class="sb-actions">
    <q-btn flat dense round size="sm" :icon="card.star ? 'star' : 'star_outline'" :color="card.star ? 'warning' : undefined" class="sb-star" aria-label="toggle priority" data-tip="toggle #priority" @click="act('star')" />
    <q-btn flat dense round size="sm" icon="edit" class="sb-edit" aria-label="edit" data-tip="supersede / edit" @click="act('edit')" />
  </div>
</div>`,
  props: {
    card: Object,
    dynamicResourcePath: String,
    resourceName: String,
  },
  data() {
    return { typed: this.card.typed || "", asked: {},
             arrows: { left: "arrow_back", up: "arrow_upward", down: "arrow_downward", right: "arrow_forward" } };
  },
  watch: {
    "card.typed"(v) {
      if (v && !this.typed) this.typed = v;
    },
  },
  methods: {
    md(el, html) {
      // the ref runs on every render: write only what changed
      if (!el || el._sbHTML === html) return;
      el._sbHTML = html;
      el.setHTML(html);
      if (!this.resourceName) return;
      const href = window.path_prefix + `${this.dynamicResourcePath}/${this.resourceName}`;
      if (document.querySelector(`link[href="${href}"]`)) return;
      const link = Object.assign(document.createElement("link"), { rel: "stylesheet", href });
      document.head.append(link);
    },
    type(v) {
      this.typed = v || "";
      this.act("typed", this.typed);
    },
    answer(e) {
      // Not the Enter that ends an IME word: it wrote a half-typed answer (the
      // GUI review, 2026-11-07). Safari sends it after compositionend, as 229.
      if (!e.isComposing && e.keyCode !== 229) this.act("answer", this.typed);
    },
    ask(c, i) {
      if (c.key && !c.tip && !this.asked[i]) {
        this.asked[i] = true;
        this.act("tip", i);
      }
    },
    act(...args) {
      this.$emit("act", args);
    },
  },
};
