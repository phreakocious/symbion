import tomllib

import pytest

from symbion import kinds as K


def test_defaults_are_the_seven_in_order():
    assert list(K.DEFAULT_KINDS) == ["note", "decision", "bug", "task",
                                     "question", "idea", "check"]
    assert K.DEFAULT_KINDS["idea"] == K.Kind(status=True, parked=True,
                                             when=K.DEFAULT_KINDS["idea"].when)
    assert K.DEFAULT_KINDS["check"].verdict and not K.DEFAULT_KINDS["check"].status
    assert all(k.when for k in K.DEFAULT_KINDS.values()), "every default carries its moment"


def test_absent_section_means_the_defaults():
    assert K.parse_kinds(None) == K.DEFAULT_KINDS


def test_a_present_table_replaces_the_defaults_entirely():
    out = K.parse_kinds({"anomaly": {"status": True}})
    assert out == {"anomaly": K.Kind(status=True)}
    assert "bug" not in out


def test_status_plus_verdict_is_legal():
    out = K.parse_kinds({"prediction": {"status": True, "verdict": True,
                                        "when": "a pre-registration"}})
    assert out["prediction"] == K.Kind(status=True, verdict=True, when="a pre-registration")


def test_parked_requires_status():
    with pytest.raises(ValueError, match="parked requires status"):
        K.parse_kinds({"shelf": {"parked": True}})
    K.parse_kinds({"shelf": {"parked": True, "status": True}})   # the other direction


@pytest.mark.parametrize("label", ["Bug", "9x", "a b", "", "bug.py"])
def test_a_bad_label_is_refused_by_name(label):
    with pytest.raises(ValueError, match=repr(label)):
        K.parse_kinds({label: {}})


def test_an_unknown_key_is_refused_by_name():
    with pytest.raises(ValueError, match="colour"):
        K.parse_kinds({"bug": {"status": True, "colour": "red"}})


def test_a_non_boolean_bit_is_refused():
    with pytest.raises(ValueError, match="status"):
        K.parse_kinds({"bug": {"status": "yes"}})


def test_an_empty_table_is_refused():
    with pytest.raises(ValueError, match="non-empty"):
        K.parse_kinds({})


def test_a_non_table_value_is_refused():
    with pytest.raises(ValueError, match="bug"):
        K.parse_kinds({"bug": True})


def test_read_kinds_with_no_toml_is_the_defaults(tmp_path):
    assert K.read_kinds(tmp_path) == K.DEFAULT_KINDS
    assert K.is_declared(tmp_path) is False


def test_read_kinds_with_a_toml_lacking_the_table_is_the_defaults(tmp_path):
    (tmp_path / "symbion.toml").write_text('default_branch = "main"\n')
    assert K.read_kinds(tmp_path) == K.DEFAULT_KINDS
    assert K.is_declared(tmp_path) is False


def test_read_kinds_with_a_declared_table(tmp_path):
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nanomaly = { status = true, when = "off the curve" }\nnote = {}\n')
    assert K.read_kinds(tmp_path) == {"anomaly": K.Kind(status=True, when="off the curve"),
                                      "note": K.Kind()}
    assert K.is_declared(tmp_path) is True


def test_render_toml_round_trips_the_defaults():
    text = K.render_toml(K.DEFAULT_KINDS)
    assert text.startswith("[kinds]\n")
    assert K.parse_kinds(tomllib.loads(text)["kinds"]) == K.DEFAULT_KINDS
