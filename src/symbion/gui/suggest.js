// Free text, with the values already known offered as you type. Quasar's
// "text autocomplete" recipe: the model IS the typed text, so a new value
// survives a blur, and a pick from the list replaces it. A browser datalist
// could not be styled; this menu is the other selects'.
// With `tokens` the text is a list, comma- or space-separated as a tags field
// is, and the menu completes its last word. Each option is the whole text
// with that word completed, since the arrow keys and a pick both write the
// option into the field; the menu shows only the word. A value already in
// the list is not offered.
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
      @filter="filter"
      @input-value="set"
      @update:model-value="set"
    >
      <template v-if="tokens" v-slot:option="scope">
        <q-item v-bind="scope.itemProps">
          <q-item-section>{{ last(scope.opt) }}</q-item-section>
        </q-item>
      </template>
    </q-select>
  `,
  props: { value: String, options: Array, tokens: Boolean },
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
      const hits = this.options.filter((o) => o.toLowerCase().includes(this.needle));
      if (!this.tokens) return hits;
      const head = this.text.replace(/[^,\s]*$/, "");
      const have = head.split(SEP).map(word);
      return hits.filter((o) => !have.includes(o)).map((o) => head + o);
    },
  },
  methods: {
    last,
    set(v) {
      this.text = v ?? "";
      this.$emit("update:value", this.text);
    },
    filter(val, update) {
      update(() => (this.needle = (this.tokens ? last(val) : val).toLowerCase()));
    },
  },
};
