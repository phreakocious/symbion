"""`init --agent hermes`: the skill link and no hook, in a temporary HOME."""
from pathlib import Path

import pytest
from symbion import cli

SKILL = Path(__file__).resolve().parents[1] / "src/symbion/data/skill"


@pytest.fixture(autouse=True)
def default_hermes_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)


def test_install_links_the_shared_skill_and_writes_no_config(tmp_path, capsys):
    notes = tmp_path / "notes"
    args = ["--dir", str(notes), "init", "--agent", "hermes"]
    home = Path.home()
    assert cli.main(args) == 1
    assert not (home / ".agents").exists()
    capsys.readouterr()
    assert cli.main([*args, "--yes"]) == 0
    out = capsys.readouterr().out
    assert (home / ".agents/skills/symbion").resolve() == SKILL
    # Hermes's config is YAML, which init cannot read: it names the step.
    assert f"skills.external_dirs in {home / '.hermes/config.yaml'}" in out, out
    # The AGENTS.md line on a line of its own, so it copies whole.
    assert "\n- At the start of a session, run `symbion summary` unless a hook already " \
           "printed it.\n" in out, out
    for path in (".hermes", ".codex", ".claude"):
        assert not (home / path).exists(), path
    assert cli.main([*args, "--yes"]) == 0
    out = capsys.readouterr().out
    # A re-run still names both steps: init cannot see whether they were taken.
    assert "linked" not in out and "skills.external_dirs" in out and "symbion summary" in out, out


def test_the_config_named_follows_hermes_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli.main(["--dir", str(tmp_path / "notes"), "init", "--agent", "hermes"])
    assert f"skills.external_dirs in {tmp_path / 'profile/config.yaml'}" in capsys.readouterr().out
