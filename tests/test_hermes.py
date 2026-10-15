"""`init --agent hermes`: the skill link and no hook, in a temporary HOME."""
from pathlib import Path

import pytest
from symbion import cli

SKILL = Path(__file__).resolve().parents[1] / "src/symbion/data/skill"


@pytest.fixture(autouse=True)
def default_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)


def test_install_links_the_skill_into_hermes_home_and_writes_no_config(tmp_path, capsys):
    """Hermes searches its own skills/ first and reads a link there; the
    ~/.agents/skills link needed an edit to config.yaml, which cost a tester
    most of their setup (2026-10-13)."""
    notes = tmp_path / "notes"
    args = ["--dir", str(notes), "init", "--agent", "hermes"]
    home = Path.home()
    assert cli.main(args) == 1
    assert not (home / ".hermes").exists()
    capsys.readouterr()
    assert cli.main([*args, "--yes"]) == 0
    out = capsys.readouterr().out
    assert (home / ".hermes/skills/symbion").resolve() == SKILL
    assert "external_dirs" not in out, out
    # The AGENTS.md line on a line of its own, so it copies whole.
    assert "\n- At the start of a session, run `symbion summary` unless a hook already " \
           "printed it.\n" in out, out
    assert [p.name for p in (home / ".hermes").iterdir()] == ["skills"], "no config written"
    for path in (".agents", ".codex", ".claude"):
        assert not (home / path).exists(), path
    assert cli.main([*args, "--yes"]) == 0
    out = capsys.readouterr().out
    # A re-run still names the AGENTS.md line: init cannot see it was added.
    assert "linked" not in out and "symbion summary" in out, out


def test_the_link_follows_hermes_home(tmp_path, monkeypatch, capsys):
    """A named profile has its own HERMES_HOME, and its own skills/."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    assert cli.main(["--dir", str(tmp_path / "notes"), "init", "--agent", "hermes", "--yes"]) == 0
    assert (tmp_path / "profile/skills/symbion").resolve() == SKILL
    assert f"the Hermes profile at {tmp_path / 'profile'}" in capsys.readouterr().out


def test_summary_names_skill_view_under_hermes(tmp_path, monkeypatch, capsys):
    """A Hermes model read the skill's path as where it loads from, and
    burned a turn (a tester, 2026-10-13): Hermes loads a skill by name.
    Both directions: outside Hermes, no link of Claude's or Codex's, no line."""
    notes = tmp_path / "notes"
    assert cli.main(["--dir", str(notes), "init", "--agent", "hermes", "--yes"]) == 0
    assert cli.main(["--dir", str(notes), "add", "note", "--target", "project",
                     "--body", "x", "--author", "t"]) == 0
    capsys.readouterr()
    assert cli.main(["--dir", str(notes), "summary"]) == 0
    assert "load the symbion skill" not in capsys.readouterr().out
    monkeypatch.setenv("HERMES_AGENT", "true")
    assert cli.main(["--dir", str(notes), "summary"]) == 0
    assert "before a write: load the symbion skill (skill_view symbion);" \
        in capsys.readouterr().out
