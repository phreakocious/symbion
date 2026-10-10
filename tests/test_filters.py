from symbion.gui import filters


def test_maps_url_params_onto_query_kwargs():
    got = filters.from_params({"tag": "priority", "kind": "check"})
    assert got == {"tag": "priority", "kind": "check"}


def test_type_and_name_become_target_kwargs():
    """The URL says type/name; store.query says target_type/target_name."""
    got = filters.from_params({"type": "file", "name": "src/x.py"})
    assert got == {"target_type": "file", "target_name": "src/x.py"}


def test_unknown_params_are_ignored_not_raised():
    """A stale bookmark or a hand-edited URL must render a page, not a 500."""
    assert filters.from_params({"tag": "a", "utm_source": "x", "": "y"}) == {"tag": "a"}


def test_empty_values_are_dropped():
    """?tag= from a cleared form means no filter, not tag=''."""
    assert filters.from_params({"tag": "", "kind": "note"}) == {"kind": "note"}


def test_structured_parses_as_a_bool_predicate():
    assert filters.from_params({"structured": "true"}) == {"structured": True}
    assert filters.from_params({"structured": "false"}) == {"structured": False}
    assert filters.from_params({"structured": "maybe"}) == {}


def test_href_round_trips_through_from_params():
    url = filters.href(tag="pri ority", kind="check")
    assert url.startswith("/notes?")
    from urllib.parse import parse_qs, urlparse
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert filters.from_params(q) == {"tag": "pri ority", "kind": "check"}


def test_href_drops_none_so_a_chip_can_clear_a_filter():
    assert filters.href(tag="a", kind=None) == "/notes?tag=a"


def test_every_filter_key_is_a_real_store_query_kwarg():
    """FILTER_KEYS is only useful if store.query actually accepts what it
    names. A rename on either side otherwise shows up as a filter that
    silently does nothing -- from_params emits the kwarg, query ignores it."""
    import inspect

    from symbion import store
    accepted = set(inspect.signature(store.query).parameters) - {"notes"}
    params = dict.fromkeys(filters.FILTER_KEYS, "x") | {"structured": "true"}
    emitted = set(filters.from_params(params))
    assert len(emitted) == len(filters.FILTER_KEYS), \
        f"a FILTER_KEY produced no kwarg: {emitted}"
    assert emitted <= accepted, f"not store.query kwargs: {sorted(emitted - accepted)}"


def test_describe_names_filters_in_url_words_not_kwarg_words():
    """The heading has to match the URL the reader is looking at."""
    assert filters.describe({}) == "all notes"
    assert filters.describe({"target_type": "file"}) == "type=file"
    assert filters.describe({"kind": "check", "tag": "ci"}) == "kind=check · tag=ci"


def test_id_maps_through_so_a_single_note_has_a_url():
    """/notes?id=X is the per-note view. Without it the GUI could link to an
    object or a filter but never to a note, and a project-target note -- which
    has no object page -- was unreachable from the arc it was filed under."""
    assert filters.from_params({"id": "20260908-1-abc"}) == {"id": "20260908-1-abc"}


def test_search_needs_every_word_in_any_order_and_case():
    """A person types words, not a phrase: `reload serve` read as a phrase
    missed a row holding both."""
    p = filters.search_pattern("Serve reload")
    assert p.search("the reload flag of serve")
    assert not p.search("the reload flag")          # one word is not enough


def test_search_takes_regex_characters_literally():
    """`$HOME` as a regex is an end-of-line anchor and matched nothing."""
    p = filters.search_pattern("$HOME")
    assert p.search("runs from $HOME/.claude")
    assert not p.search("runs from HOME")


def test_a_blank_search_is_no_filter():
    assert filters.search_pattern("  ") is None
    assert filters.from_params({"q": " "}) == {}
    assert "grep" in filters.from_params({"q": "x"})


def test_an_id_or_its_printed_tail_is_a_hit():
    """`-a1b` too: symbion prints "rows end in -a1b", and a search for one
    found nothing (the owner, 2026-10-09)."""
    nid = "20260101-120000-123456-a1b"
    for q in (nid, "a1b", "…a1b", "...a1b", "123456-a1b", "-a1b", "-123456-a1b"):
        assert filters.id_hit(nid, q), q
    for q in ("1b", "a1", "", "a1b x", "120000"):
        assert not filters.id_hit(nid, q), q


def test_describe_names_a_search_by_what_was_typed():
    kw = filters.from_params({"q": "a  b", "kind": "bug"})
    assert filters.describe(kw, "a  b") == 'search "a b" · kind=bug'
