import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from symbion import cli, store

# The console script this suite runs under (test_hook.py says why not `which`).
SYMBION = str(Path(sys.executable).parent / "symbion")


def tab(line, cwd):
    """{completion: description} for a TAB press at the end of `line`, as
    zsh asks for it: the shell runs `symbion` bare with the line in the
    environment, and argcomplete answers `value:description` per line. A
    colon inside a value comes back escaped."""
    fd, out = tempfile.mkstemp()
    os.close(fd)
    env = {**os.environ, "_ARGCOMPLETE": "1", "_ARGCOMPLETE_SHELL": "zsh",
           "_ARGCOMPLETE_SUPPRESS_SPACE": "1", "_ARGCOMPLETE_IFS": "\n",
           "COMP_LINE": line, "COMP_POINT": str(len(line)),
           "_ARGCOMPLETE_STDOUT_FILENAME": out}
    try:
        r = subprocess.run([SYMBION], cwd=cwd, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        text = Path(out).read_text()
    finally:
        os.unlink(out)
    pairs = (re.fullmatch(r"((?:\\.|[^\\:])*):(.*)", x).groups() for x in text.split("\n") if x)
    return {re.sub(r"\\(.)", r"\1", v): d for v, d in pairs}


def _store(tmp_path):
    d = tmp_path / "store"
    store.ensure_store(d)
    return d


def _main(d, *argv):
    assert cli.main(["--dir", str(d), *argv]) == 0


def _ids(d):
    return {n.body.split("\n")[0]: n.id for n in store.heads(store.load(d))}


def test_completion_prints_shell_code_outside_a_repo(tmp_path, monkeypatch, capsys):
    """An rc file runs it wherever the shell starts, which is rarely a repo."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["completion", "zsh"]) == 0
    assert capsys.readouterr().out.startswith("#compdef symbion")


def test_verbs_complete_outside_a_repo(tmp_path):
    assert tab("symbion ar", tmp_path) == {"arc": "campaigns: a checklist of rows across objects"}


def test_kinds_are_the_store_s_own_with_their_when(tmp_path):
    d = _store(tmp_path)
    (d / "symbion.toml").write_text(
        '[kinds]\nanomaly = { status = true, when = "off the curve" }\nnote = {}\n')
    assert tab(f"symbion --dir {d.as_posix()} add ", tmp_path) == {"anomaly": "off the curve", "note": ""}


def test_resolve_offers_open_rows_and_show_every_head(tmp_path):
    d = _store(tmp_path)
    for kind, body in (("bug", "open bug"), ("bug", "fixed bug"), ("note", "a note")):
        _main(d, "add", kind, "--target", "item:x", "--body", body)
    fixed = _ids(d)["fixed bug"]
    _main(d, "resolve", fixed, "--body", "done")
    ids = _ids(d)

    got = tab(f"symbion --dir {d.as_posix()} resolve ", tmp_path)
    assert got == {ids["open bug"]: "[bug] item:x  open bug"}
    assert set(tab(f"symbion --dir {d.as_posix()} show ", tmp_path)) == set(ids.values())
    assert fixed not in ids.values()                 # superseded by its resolution


def test_a_target_completes_its_type_then_names_from_targets_and_refs(tmp_path):
    d = _store(tmp_path)
    _main(d, "add", "note", "--target", "item:a b", "--ref", "item:c", "--body", "n")
    assert {"item:", "project"} <= set(tab(f"symbion --dir {d.as_posix()} context --target ", tmp_path))
    assert set(tab(f"symbion --dir {d.as_posix()} context --target item:", tmp_path)) == {"item:a b", "item:c"}


def test_tags_complete_with_their_counts(tmp_path):
    d = _store(tmp_path)
    for tags in (["ux"], ["ux", "cli"]):
        _main(d, "add", "note", "--target", "item:x", "--body", "n",
              *(a for t in tags for a in ("--tag", t)))
    assert tab(f"symbion --dir {d.as_posix()} list --tag ", tmp_path) == {"ux": "2 rows", "cli": "1 row"}


def test_dir_on_the_line_names_the_store_for_arcs(tmp_path):
    d = _store(tmp_path)
    _main(d, "arc", "create", "--name", "Tidy docs")
    assert tab(f"symbion --dir {d.as_posix()} arc todo ", tmp_path) == {"tidy-docs": "Tidy docs"}
