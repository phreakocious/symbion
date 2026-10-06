"""Native Windows hook behavior, without Git Bash or a PowerShell profile."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from symbion import cli, store

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows hook")
HOOK = Path(__file__).resolve().parents[1] / "src/symbion/data/skill/session_start.ps1"
POWERSHELL = shutil.which("powershell.exe")


@pytest.fixture(autouse=True)
def default_codex_home(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)


def run_hook(repo, **overrides):
    env = {**os.environ, "PATH": os.pathsep.join([
        str(Path(sys.executable).parent), str(Path(shutil.which("git")).parent)]),
        "SYMBION_DIR": "", "SYMBION_AUTHOR": "", **overrides}
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive",
                           "-ExecutionPolicy", "Bypass", "-File", str(HOOK)],
                          cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8")


@pytest.mark.parametrize("mode", ["absent", "sibling", "relative", "absolute", "blank",
                                  "blank-alone", "missing", "directory", "broken-config", "env"])
def test_native_hook_adoption_and_errors(repo, tmp_path, mode):
    notes = repo.parent / (repo.name + "-notes") if mode in {"sibling", "blank"} else tmp_path / "notes with spaces"
    if mode not in {"absent", "blank-alone", "missing", "directory"}:
        store.ensure_store(notes)
        store.add(notes, kind="task", target={"type": "project", "name": None},
                  author="human", body="owner request: → λ", status="open")
    pointer = repo / ".symbion"
    if mode in {"relative", "missing", "broken-config"}:
        pointer.write_bytes(b" \r\n../notes with spaces\r\n")
    elif mode == "env":
        pointer.write_text("../missing-pointer-store")
    elif mode == "absolute":
        pointer.write_text(str(notes), encoding="utf-8")
    elif mode in {"blank", "blank-alone"}:   # a blank pointer adopts nothing
        pointer.write_bytes(b" \r\n")
    elif mode == "directory":
        pointer.mkdir()
    if mode == "broken-config":
        (notes / "symbion.toml").write_text("[invalid", encoding="utf-8")
    result = run_hook(repo, **({"SYMBION_DIR": str(notes)} if mode == "env" else {}))
    assert result.returncode == 0 and result.stderr == "", result
    if mode in {"absent", "blank-alone"}:
        assert result.stdout == ""
    elif mode == "missing":
        assert "no store at" in result.stdout
    elif mode == "directory":
        assert "cannot read" in result.stdout
    elif mode == "broken-config":
        assert "could not parse" in result.stdout
    else:
        assert "owner request: → λ" in result.stdout


def test_native_hook_names_missing_executable_only_in_adopted_projects(repo):
    result = run_hook(repo, PATH="")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    (repo / ".symbion").write_text("../notes")
    result = run_hook(repo, PATH="")
    assert result.returncode == 0 and result.stderr == ""
    assert "'symbion' is not on PATH" in result.stdout


def test_native_hook_preserves_an_explicit_reader(repo):
    notes = repo.parent / (repo.name + "-notes")
    store.ensure_store(notes)
    for author in ("codex", "human"):
        store.add(notes, kind="idea", target={"type": "project", "name": None},
                  author=author, body=f"parked by {author}", status="open")
    result = run_hook(repo, SYMBION_AUTHOR="human")
    assert result.returncode == 0 and result.stderr == ""
    assert "parked by codex" in result.stdout and "parked by human" not in result.stdout


def test_native_hook_shares_the_main_worktrees_store(repo, tmp_path):
    notes = repo.parent / (repo.name + "-notes")
    store.ensure_store(notes)
    store.add(notes, kind="task", target={"type": "project", "name": None},
              author="human", body="shared worktree request", status="open")
    linked = tmp_path / "linked with spaces"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked)],
                   check=True, capture_output=True)
    subdir = linked / "subdir"
    subdir.mkdir()
    result = run_hook(subdir)
    assert result.returncode == 0 and result.stderr == "", result
    assert "shared worktree request" in result.stdout


def test_native_only_registration_is_not_duplicated(tmp_path, capsys):
    settings = Path.home() / ".codex/hooks.json"
    settings.parent.mkdir()
    entry = cli._codex_hook_entry(Path.home() / ".agents/skills/symbion")
    handler = entry["hooks"][0]
    handler["command"] = handler.pop("commandWindows")
    contents = json.dumps({"hooks": {"SessionStart": [None, entry]}})
    settings.write_text(contents)
    assert cli.main(["--dir", str(tmp_path / "notes"), "init", "--agent", "codex", "--yes"]) == 0
    assert settings.read_text() == contents
    output = capsys.readouterr().out
    assert f"kept hook in {settings}" in output
    assert "append this entry" not in output
