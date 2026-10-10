import os
import shlex
import sys
import time

import pytest
from symbion import catalog
from symbion.config import Config


def cfg(tmp_path, **catalogs):
    return Config(project_root=tmp_path, catalogs=catalogs)


def test_names_splits_stdout_lines_and_strips_blanks(tmp_path):
    c = cfg(tmp_path, thing="printf 'a\\n\\nb\\n'")
    assert catalog.names(c, "thing") == ["a", "b"]


def test_commands_run_in_the_project_root(tmp_path):
    """A catalog run from the wrong cwd returns a plausible non-empty wrong
    list — a wrong answer that reads as a right one."""
    (tmp_path / "marker.txt").write_text("")
    c = cfg(tmp_path, thing="ls marker.txt")
    assert catalog.names(c, "thing") == ["marker.txt"]


def test_empty_output_is_an_error_not_an_empty_catalog(tmp_path):
    """`git ls-files '*.py'` in a repo with no Python exits 0 and prints
    nothing; seeding over it would create 0 tasks and report success."""
    c = cfg(tmp_path, thing="true")
    with pytest.raises(catalog.CatalogError):
        catalog.names(c, "thing")


def test_nonzero_exit_is_an_error(tmp_path):
    c = cfg(tmp_path, thing="echo x; false")
    with pytest.raises(catalog.CatalogError):
        catalog.names(c, "thing")


def test_resolve_exact_match_wins():
    assert catalog.resolve("parser.py", ["src/parser.py", "parser.py"]) == "parser.py"


def test_resolve_unique_substring_expands():
    assert catalog.resolve("parser", ["src/parser.py", "src/cli.py"]) == "src/parser.py"


def test_resolve_ambiguous_refuses_with_candidates():
    with pytest.raises(catalog.AmbiguousName) as e:
        catalog.resolve("test", ["a/test_one.py", "a/test_two.py"])
    assert len(e.value.candidates) == 2


def test_resolve_miss_falls_back_to_the_input():
    assert catalog.resolve("gone.py", ["src/parser.py"]) == "gone.py"


def test_resolve_a_unique_case_only_match_after_substring_misses():
    """`Balancer` reached `Load Balancer` as a substring while the closer
    `load balancer` missed (2026-10-03). Case-only twins stay a miss."""
    names = ["Load Balancer", "Load Shedding"]
    assert catalog.resolve("Balancer", names) == "Load Balancer"
    assert catalog.resolve("load balancer", names) == "Load Balancer"
    assert catalog.resolve("Readme.md", ["README.md", "readme.md"]) == "Readme.md"


def test_item_type_is_never_canonicalized(tmp_path):
    c = cfg(tmp_path, thing="echo a")
    assert catalog.canonical(c, "item", "  anything typed ") == "  anything typed "


def test_output_that_is_not_utf8_names_the_command(tmp_path):
    """A catalog printing Latin-1 ended in `error: 'utf-8' codec can't
    decode byte`, naming no command (2026-10-09 review)."""
    c = cfg(tmp_path, thing="printf 'caf\\351\\n'")
    with pytest.raises(catalog.CatalogError, match=r"not UTF-8 \(byte 3\): printf"):
        catalog.names(c, "thing")


def test_command_exceeding_timeout_raises_catalog_error(tmp_path):
    c = Config(project_root=tmp_path, catalogs={"thing": "sleep 5"}, command_timeout=1)
    with pytest.raises(catalog.CatalogError):
        catalog.names(c, "thing")


def test_a_timeout_kills_the_commands_children_too(tmp_path):
    """subprocess.run's timeout kills the shell, not what it forked: the child
    wrote its marker after symbion reported the command killed. The trailing
    `; true` makes the shell fork rather than exec the child; `sleep 5`
    above cannot see the difference."""
    marker = tmp_path / "marker"
    # as_posix: Git's sh hands `\\` to a native program as `\`, so a repr()'d
    # Windows path read as a \U escape and the child died at once; `; true`
    # then exited 0 inside the timeout.
    child = f"import time; time.sleep(0.5); open({marker.as_posix()!r}, 'w')"
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)}; true"
    c = Config(project_root=tmp_path, command_timeout=0.1)
    with pytest.raises(catalog.CatalogError, match="killed"):
        catalog.run_configured(c, cmd)
    time.sleep(0.8)
    assert not marker.exists(), "a child of the timed-out command ran on"
    # The control: given the time, the same command writes its marker, so the
    # pass above is the kill's, not a child that never ran.
    catalog.run_configured(Config(project_root=tmp_path, command_timeout=10), cmd)
    assert marker.exists(), "the child never ran, so the kill above proved nothing"

def test_command_under_timeout_still_succeeds(tmp_path):
    """A timeout that only ever fires is not a timeout: prove it does not
    swallow a normal run that finishes well inside the bound."""
    c = Config(project_root=tmp_path, catalogs={"thing": "echo a"}, command_timeout=1)
    assert catalog.names(c, "thing") == ["a"]


def test_stdin_is_isolated_so_a_reading_command_does_not_hang(tmp_path):
    """If the configured command inherited stdin instead of having it closed,
    `cat` would block reading from the still-open pipe held open below, and
    the 1s timeout would fire and raise CatalogError. Success here proves
    DEVNULL is doing the work, not the clock."""
    c = Config(project_root=tmp_path, catalogs={"thing": "cat; echo a"}, command_timeout=1)
    read_fd, write_fd = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(read_fd, 0)
        os.close(read_fd)
        assert catalog.names(c, "thing") == ["a"]
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(write_fd)


def test_names_keeps_a_name_verbatim_including_its_trailing_space(tmp_path):
    """The catalog protocol: every non-blank line is the name as emitted. A `.strip()` here
    silently renames a legal path, and the task it seeds then never matches
    the catalog again on any later reconcile."""
    c = cfg(tmp_path, thing="printf 'a .py\\n b.py\\n   \\nc.py\\n'")
    assert catalog.names(c, "thing") == ["a .py", " b.py", "c.py"]


def test_a_missing_catalog_says_where_catalogs_are_configured(tmp_path):
    """The message a fresh checkout hits first. `no catalog command for type
    'thing'` is true and unactionable: nothing in it says catalogs live under
    [catalogs] in symbion.toml, so the reader has no next move. Other
    direction: a configured scope still returns its names -- see
    test_names_splits_stdout_lines_and_strips_blanks."""
    with pytest.raises(catalog.CatalogError, match=r"\[catalogs\].*symbion\.toml"):
        catalog.names(cfg(tmp_path), "thing")


# ---- resolvers ----
def rcfg(tmp_path, cat, res, **kw):
    return Config(project_root=tmp_path, catalogs={"t": cat}, resolvers={"t": res}, **kw)


def test_run_configured_input_reaches_stdin(tmp_path):
    c = Config(project_root=tmp_path)
    assert catalog.run_configured(c, "cat", input="x\ny\n").stdout == "x\ny\n"
    # Bytes, not text read back: text mode turns a CR the command got into
    # nothing on the way out. Windows sent CRLF.
    assert catalog.run_configured(c, "wc -c", input="x\ny\n").stdout.strip() == "4"


def test_run_configured_reads_crlf_and_a_lone_cr_as_newlines(tmp_path):
    """Its output is read as bytes, so text mode no longer does this."""
    c = Config(project_root=tmp_path)
    assert catalog.run_configured(c, "printf 'a\\r\\nb\\rc\\n'").stdout == "a\nb\nc\n"


def test_resolver_exit_0_stores_the_printed_name_even_outside_the_catalog(tmp_path):
    """A miss is the resolver's business: printing a name not in the list is
    how a first sighting mints its canonical form."""
    c = rcfg(tmp_path, "echo a", "echo minted")
    assert catalog.canonical(c, "t", "anything") == "minted"


def test_resolver_first_nonblank_line_wins_and_the_rest_is_ignored(tmp_path):
    c = rcfg(tmp_path, "echo a", "printf '\\nfirst \\nsecond\\n'")
    assert catalog.canonical(c, "t", "q") == "first "


def test_resolver_exit_2_is_ambiguous_with_the_printed_candidates(tmp_path):
    c = rcfg(tmp_path, "echo a", "printf 'x\\ny\\n'; exit 2")
    with pytest.raises(catalog.AmbiguousName) as e:
        catalog.canonical(c, "t", "q")
    assert e.value.query == "q" and e.value.candidates == ["x", "y"]


def test_resolver_exit_2_with_no_candidates_is_an_error_that_keeps_stderr(tmp_path):
    """Python exits 2 on a script path it cannot open, so a wrong path landed
    in the ambiguous branch, and that branch dropped the stderr that named it."""
    c = rcfg(tmp_path, "echo a", f"{shlex.quote(sys.executable)} tools/no_such_resolver.py")
    with pytest.raises(catalog.CatalogError) as e:
        catalog.canonical(c, "t", "q")
    assert "printed no candidates" in str(e.value) and "No such file" in str(e.value)


@pytest.mark.parametrize("res", ["exit 1", "true", "printf '\\n\\n'", "sleep 5"])
def test_resolver_failure_never_falls_through(tmp_path, res):
    """The built-in rule would expand `parser` to `src/parser.py`; a raise
    proves neither that nor the input was stored. The spec's whole reason."""
    c = rcfg(tmp_path, "echo src/parser.py", res, command_timeout=1)
    with pytest.raises(catalog.CatalogError) as e:
        catalog.canonical(c, "t", "parser")
    assert res.split()[0] in str(e.value) or "timeout" in str(e.value)


def test_resolver_reads_the_query_then_the_candidates_verbatim(tmp_path):
    """Line 1 is the query, line 2 the first catalog name, trailing space kept."""
    c = rcfg(tmp_path, "printf 'a.py \\nb\\n'", "sed -n 2p")
    assert catalog.canonical(c, "t", "q") == "a.py "
    c = rcfg(tmp_path, "printf 'a\\nb\\n'", "sed -n 1p")
    assert catalog.canonical(c, "t", "q") == "q"


def test_a_query_with_a_newline_is_refused_not_truncated(tmp_path):
    """The query is stdin line 1: a query containing a newline bleeds into the
    candidate lines, and a `head -1`-style resolver would store a silent
    truncation instead of the whole query. Both directions: the newline
    raises, and the same query with the newline replaced by a space resolves."""
    c = rcfg(tmp_path, "echo a", "head -1")
    with pytest.raises(catalog.CatalogError):
        catalog.canonical(c, "t", "12.9\n13.000000")
    assert catalog.canonical(c, "t", "12.9 13.000000") == "12.9 13.000000"


def test_empty_catalog_with_a_resolver_resolves_with_zero_candidates(tmp_path):
    """A store-derived catalog is empty until the first row: the resolver sees
    a one-line stdin (the query) and mints. Without a resolver the guard stays."""
    c = rcfg(tmp_path, "true", "wc -l | tr -d ' '")
    assert catalog.canonical(c, "t", "q") == "1"
    with pytest.raises(catalog.CatalogError):
        catalog.canonical(Config(project_root=tmp_path, catalogs={"t": "true"}), "t", "q")


def test_failing_catalog_is_an_error_even_when_empty_would_be_allowed(tmp_path):
    """The exit check runs before the emptiness check: a producer that fails
    must not read as an empty catalog and hand the resolver a first sighting."""
    c = rcfg(tmp_path, "false", "head -1")
    with pytest.raises(catalog.CatalogError):
        catalog.canonical(c, "t", "q")


def test_a_sweep_still_refuses_an_empty_catalog_with_a_resolver_declared(tmp_path):
    c = rcfg(tmp_path, "true", "head -1")
    with pytest.raises(catalog.CatalogError):
        catalog.names(c, "t")
    assert catalog.pool(c, "t") == []


def test_an_empty_catalog_with_no_resolver_names_the_deadlock(tmp_path):
    """A store-derived catalog is empty until its first row exists, and that
    row cannot be written while it is empty. Blaming the producer sends the
    reader to debug a command that worked."""
    c = cfg(tmp_path, t="true")
    with pytest.raises(catalog.CatalogError) as e:
        catalog.pool(c, "t")
    assert "resolvers" in str(e.value) and "minted" in str(e.value)


def test_a_failing_producer_is_not_re_diagnosed_as_a_missing_resolver(tmp_path):
    """The direction that makes the test above non-inert. A non-zero exit must
    keep its own diagnosis: a broken command and an empty store are different
    faults, and advising a resolver for the first one is a wrong answer that
    reads as a helpful one."""
    c = cfg(tmp_path, t="echo boom >&2; false")
    with pytest.raises(catalog.CatalogError) as e:
        catalog.pool(c, "t")
    assert "exited 1" in str(e.value)
    assert "resolvers" not in str(e.value)


def test_a_populated_catalog_with_no_resolver_still_pools(tmp_path):
    """The ordinary case must be untouched: `file` has no resolver anywhere."""
    assert catalog.pool(cfg(tmp_path, t="printf 'a\\nb\\n'"), "t") == ["a", "b"]


def test_pending_is_unioned_with_the_catalog_not_appended(tmp_path):
    """An answer already in the catalog must not appear twice, or a second
    identical query hits two candidates and is refused as ambiguous."""
    c = Config(project_root=tmp_path, catalogs={"t": "echo src/parser.py"})
    assert catalog.canonical(c, "t", "parser", pending=["src/parser.py"]) == "src/parser.py"
    counting = rcfg(tmp_path, "echo src/parser.py", "tail -n +2 | wc -l | tr -d ' '")
    assert catalog.canonical(counting, "t", "q", pending=["src/parser.py"]) == "1"


def test_pending_names_are_candidates(tmp_path):
    c = Config(project_root=tmp_path, catalogs={"t": "echo other"})
    assert catalog.canonical(c, "t", "pars", pending=["src/parser.py"]) == "src/parser.py"


def test_canonical_candidates_override_skips_running_the_catalog(tmp_path):
    """`candidates=` is for a write that already ran the catalog once for
    this type: the pool is passed in, not re-fetched. `false` proves it both
    ways -- a catalog command that would fail is never run when `candidates=`
    is given, and still raises CatalogError when it is not."""
    c = Config(project_root=tmp_path, catalogs={"t": "false"})
    assert catalog.canonical(c, "t", "parser", candidates=["src/parser.py"]) == "src/parser.py"
    with pytest.raises(catalog.CatalogError):
        catalog.canonical(c, "t", "parser")


def test_match_without_a_resolver_is_the_builtin_rule(tmp_path):
    c = Config(project_root=tmp_path, catalogs={"t": "echo a"})
    assert catalog.match(c, "t", "pars", ["src/parser.py"]) == "src/parser.py"
    with pytest.raises(catalog.AmbiguousName):
        catalog.match(c, "t", "p", ["src/parser.py", "src/pipe.py"])
    assert catalog.match(c, "t", "nothing", ["src/parser.py"]) == "nothing"


def test_configured_command_gets_the_store_in_use_as_symbion_dir(tmp_path, monkeypatch):
    """A store-derived catalog (`symbion list --json | ...`) spawns a NESTED
    symbion, which resolves its own store from the environment -- `--dir` is
    argv and cannot be inherited. Without SYMBION_DIR pointed at the store in
    use, a command run under `--dir` reads the DEFAULT store's names and
    answers confidently from the wrong ones. The ambient value here is the
    wrong store, so the assertion fails if the child merely inherits it."""
    monkeypatch.setenv("SYMBION_DIR", str(tmp_path / "ambient-notes"))
    c = Config(project_root=tmp_path, store=tmp_path / "in-use-notes")
    assert catalog.run_configured(c, "echo $SYMBION_DIR").stdout.strip() == str(tmp_path / "in-use-notes")


def test_a_config_with_no_store_leaves_the_ambient_symbion_dir_alone(tmp_path, monkeypatch):
    """The other direction: a hand-built Config (every test fixture, the GUI's
    direct construction) names no store, and must not blank the caller's."""
    monkeypatch.setenv("SYMBION_DIR", str(tmp_path / "ambient-notes"))
    c = Config(project_root=tmp_path)
    assert catalog.run_configured(c, "echo $SYMBION_DIR").stdout.strip() == str(tmp_path / "ambient-notes")


def test_match_announces_a_substring_pick_but_not_an_exact_one(tmp_path, capsys):
    """Only the miss spoke; a unique-substring pick landed silently on
    whatever it hit (2026-09-22). Exact is quiet; a non-exact resolution
    names the pick and its match kind, on the channel the miss already uses.
    stdout and the return value unchanged."""
    c = Config(project_root=tmp_path, catalogs={"t": "echo a"})
    assert catalog.match(c, "t", "src/parser.py", ["src/parser.py"]) == "src/parser.py"
    assert capsys.readouterr() == ("", "")
    assert catalog.match(c, "t", "pars", ["src/parser.py", "src/cli.py"]) == "src/parser.py"
    out, err = capsys.readouterr()
    assert out == "" and err == "note: 'pars' resolved to 'src/parser.py' (unique substring in the t catalog)\n", err
    assert catalog.match(c, "t", "SRC/CLI.PY", ["src/parser.py", "src/cli.py"]) == "src/cli.py"
    assert capsys.readouterr().err == \
        "note: 'SRC/CLI.PY' resolved to 'src/cli.py' (same name but for case in the t catalog)\n"
    catalog.match(c, "t", "nothing", ["src/parser.py"])
    assert "matches nothing in the t catalog; taken as typed" in capsys.readouterr().err
