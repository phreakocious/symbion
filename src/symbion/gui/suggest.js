// Free text, with the values already known offered as you type. Quasar's
// "text autocomplete" recipe: the model IS the typed text, so a new value
// survives a blur, and a pick from the list replaces it. A browser datalist
// could not be styled; this menu is the other selects'.
// With `tokens` the text is a list, comma- or space-separated as a tags field
// is, and the menu completes its last word. Each option is the whole text
// with that word completed, since the arrow keys and a pick both write the
// option into the field; the menu shows only the word. A value already in
// the list is not offered.
// A name may carry a caption, a description the menu shows under it; typing
// matches the caption too, and a pick writes the name. A long caption is cut
// with an ellipsis, in a menu no wider than `sb-pick-menu` allows: a
// 219-character one stretched it to the window's edge.
// `value`, not `model-value`, as NiceGUI's input: with loopback off,
// NiceGUI writes an emitted `update:modelValue`'s whole argument list into
// the prop, and the select then read an array as its model.
const SEP = /[,\s]+/;
const word = (w) => w.replace(/^#+/, "");
const last = (text) => word(text.split(SEP).pop());

export default {
  template: `
    <q-select
      :model-value="text"
      :options="shown"
      use-input
      hide-selected
      fill-input
      input-debounce="0"
      popup-content-class="sb-pick-menu"
      @filter="filter"
      @input-value="set"
      @update:model-value="set"
    >
      <template v-slot:option="scope">
        <q-item v-bind="scope.itemProps">
          <q-item-section>
            <q-item-label>{{ tokens ? last(scope.opt) : scope.opt }}</q-item-label>
            <q-item-label v-if="cap(scope.opt)" caption lines="1">{{ cap(scope.opt) }}</q-item-label>
          </q-item-section>
        </q-item>
      </template>
    </q-select>
  `,
  props: {
    value: String,
    options: Array,
    tokens: Boolean,
    captions: { type: Object, default: () => ({}) },
  },
  emits: ["update:value"],
  data() {
    return { text: this.value, needle: "" };
  },
  watch: {
    value(v) {
      this.text = v;
    },
  },
  computed: {
    shown() {
      const hits = this.options.filter(
        (o) =>
          o.toLowerCase().includes(this.needle) ||
          this.cap(o).toLowerCase().includes(this.needle),
      );
      if (!this.tokens) return hits;
      const head = this.text.replace(/[^,\s]*$/, "");
      const have = head.split(SEP).map(word);
      return hits.filter((o) => !have.includes(o)).map((o) => head + o);
    },
  },
  methods: {
    last,
    // Own keys only: `captions.constructor` is every object's, and an
    // option named `constructor` read it as its caption.
    cap(o) {
      return Object.hasOwn(this.captions, o) ? this.captions[o] : "";
    },
    set(v) {
      this.text = v ?? "";
      this.$emit("update:value", this.text);
    },
    filter(val, update) {
      update(() => (this.needle = (this.tokens ? last(val) : val).toLowerCase()));
    },
  },
};
