import argparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from symbion import api, cli, gitref, store
from symbion import summary as summ


_WRITES = {"add", "resolve", "supersede", "commit", "rename"}
_ARC_WRITES = {("arc", v) for v in ("create", "seed", "rename", "archive")}


def _commit(store_dir, capsys):
    """Commit the store, every writer's rows, so the edits after it append:
    before a commit, an edit by the row's own author rewrites the row
    (store.rewritable)."""
    assert run("commit", "--all", store_dir=store_dir) == 0
    capsys.readouterr()


def _earlier(store_dir, capsys, nid):
    """The id an edit gave the version it replaced: the row keeps `nid`."""
    assert run("show", nid, "--json", store_dir=store_dir) == 0
    return json.loads(capsys.readouterr().out)[0]["supersedes"]


def run(*args, store_dir):
    """A write refuses a store `init` never made, so a write verb here first
    makes `store_dir` one, as the first write itself once did: a read of a
    fresh dir still finds no store. A test of the refusal calls cli.main."""
    if set(args[:1]) & _WRITES or tuple(args[:2]) in _ARC_WRITES:
        store.ensure_store(store_dir)
    return cli.main(["--dir", str(store_dir), *args])


def _sans_pending(err: str) -> str:
    """stderr without the `N rows not committed` line every write ends with."""
    return re.sub(r"(?m)^note: \d+ (?:entry|entries) not committed .*\n", "", err)


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo, chdir'd into so config.project_root() resolves here
    (it shells to `git worktree list --porcelain` in the real cwd)."""
    r = tmp_path / "p"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c0")
    monkeypatch.chdir(r)
    return r


def _seeded_reconcile_arc(tmp_path):
    """An arc with one live task (thing:live1) and one stale
    task (thing:stale1) -- 'thing' catalogs to exactly ["live1"], and
    [renames] is left unconfigured so stale1 has no rename evidence."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nthing = "echo live1"\n')
    run("arc", "create", "--name", "x", "--scope", "thing", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "thing", "--name", "live1", "--name", "stale1",
        store_dir=tmp_path)
    return aid


def test_author_default_precedence_chain(monkeypatch):
    """Four branches -- --author > SYMBION_AUTHOR > "claude" if CLAUDECODE >
    git user.name > "user" -- with no regression lock before this; it cost
    two commits and a spec amendment to get right."""
    monkeypatch.delenv("SYMBION_AUTHOR", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)

    # 1: --author wins outright, consulting nothing else.
    assert cli._resolved_author(argparse.Namespace(author="explicit")) == "explicit"

    # 2: SYMBION_AUTHOR wins over CLAUDECODE when both are set.
    monkeypatch.setenv("SYMBION_AUTHOR", "envperson")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert api.author_default() == "envperson"
    monkeypatch.delenv("SYMBION_AUTHOR")

    # 3: CLAUDECODE, with SYMBION_AUTHOR absent.
    assert api.author_default() == "claude"
    monkeypatch.delenv("CLAUDECODE")

    # 4: neither set -- falls through to `git config user.name`.
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="gitperson\n"))
    assert api.author_default() == "gitperson"

    # 5: git config empty too -- the final fallback.
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=""))
    assert api.author_default() == "user"


def test_add_echoes_the_new_id(tmp_path, capsys):
    assert run("add", "--kind", "decision", "--type", "project",
               "--body", "b", store_dir=tmp_path) == 0
    assert capsys.readouterr().out.strip()


def test_list_json_is_parseable(tmp_path, capsys):
    run("add", "--kind", "note", "--type", "item", "--name", "a b c",
        store_dir=tmp_path)
    capsys.readouterr()  # discard the `add` id; capsys accumulates across
                         # calls in one test, and it would otherwise leak
                         # into the --json output as leading non-JSON text
    run("list", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["target"]["name"] == "a b c", \
        "--json is what removes whitespace-token parsing"


def test_add_takes_the_kind_positionally_and_the_target_as_context_spells_it(tmp_path, capsys):
    """`add decision --target file:CLAUDE.md` was the shape agents guessed
    for add; `context --target` spells a target that way, so the guess was
    reasonable. Both spellings
    reach the same row, and a project target needs no name."""
    assert run("add", "decision", "--target", "item:a b", "--body", "x", store_dir=tmp_path) == 0
    assert run("add", "note", "--target", "project:", "--body", "1", store_dir=tmp_path) == 0
    assert run("add", "note", "--target", "project", "--body", "2", store_dir=tmp_path) == 0
    capsys.readouterr()
    run("list", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert sorted((r["kind"], r["target"]["type"], r["target"]["name"]) for r in rows) == [
        ("decision", "item", "a b"), ("note", "project", None), ("note", "project", None)]


@pytest.mark.parametrize("argv", [
    ("add", "note", "--kind", "task", "--type", "project"),
    ("add", "--kind", "note", "--target", "item:a", "--type", "item"),
    ("add", "--kind", "note", "--target", "item:a", "--name", "a"),
])
def test_add_refuses_two_spellings_of_one_field(tmp_path, capsys, argv):
    assert run(*argv, store_dir=tmp_path) == 2
    assert "not both" in capsys.readouterr().err
    assert not store.load(tmp_path)


@pytest.mark.parametrize("kind,argv,fix", [
    # Each of these refusals was followed by `resolve --help` and a retry;
    # the message named the rule, not the fix.
    ("task", ("--result", "r"), "--body"),
    ("prediction", (), '--result "'),
    # `supersede <id>` alone sent a newcomer to `supersede --status resolved`,
    # which printed the same error (2026-09-26): name the plain row's exit.
    ("note", (), "`supersede <id> --add-tag retired`"),
])
def test_a_resolve_refusal_names_the_command_that_works(tmp_path, capsys, kind, argv, fix):
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\ntask = { status = true }\nnote = {}\n'
        'prediction = { status = true, verdict = true }\n')
    run("add", kind, "--target", "project", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("resolve", nid, *argv, store_dir=tmp_path) != 0
    assert fix in capsys.readouterr().err


def test_list_grep_searches_whole_bodies_and_names_what_it_hid(tmp_path, capsys):
    """`list --all | grep X` read the 25-row text page, dropped its header,
    returned nothing, and the next row claimed the store held nothing on X,
    beside a prediction about exactly that.
    --grep reads every head's body, target name, checked and result, takes
    `a|b`, ignores case, and keeps the `N of M match` header."""
    for i in range(30):                                   # push the hit off a 25-row page
        run("add", "note", "--target", f"item:filler{i}", "--body", "nothing here", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--body", "cache_hits will sit EMPTY", store_dir=tmp_path)
    run("add", "note", "--target", "item:config.ini parse line", store_dir=tmp_path)
    old = capsys.readouterr().out.split()[-1]
    run("add", "note", "--target", "project", "--body", "SMTP host in a body", store_dir=tmp_path)
    hid = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("supersede", hid, "--body", "gone", store_dir=tmp_path)
    capsys.readouterr()

    run("list", "--grep", "cache|parse", "--json", store_dir=tmp_path)
    got = json.loads(capsys.readouterr().out)
    assert sorted(r["body"] or r["target"]["name"] for r in got) == [
        "cache_hits will sit EMPTY", "config.ini parse line"], got
    assert old in {r["id"] for r in got}

    run("list", "--grep", "smtp", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head.startswith("0 of 33 match --grep smtp") and "+1 superseded (--all)" in head, head

    assert run("list", "--grep", "(", store_dir=tmp_path) == 2
    assert "--grep" in capsys.readouterr().err


def test_list_grep_reads_tags(tmp_path, capsys):
    """`--grep gui` missed a row tagged gui whose text never said gui (the
    user, 2026-10-02): a writer tags a subject the text does not name."""
    run("add", "note", "--target", "project", "--body", "the drawer slides over",
        "--tag", "gui", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--body", "nothing", store_dir=tmp_path)
    capsys.readouterr()
    for pattern in (["gui"], ["#gui", "-F"]):
        run("list", "--grep", *pattern, "--json", store_dir=tmp_path)
        got = json.loads(capsys.readouterr().out)
        assert [r["body"] for r in got] == ["the drawer slides over"], pattern


def test_list_grep_reads_measurements(tmp_path, capsys):
    """A measurement one day wrong was found only by accident: `--grep` over
    its name and its value read 0 entries (2026-10-14). Each one is searched
    as `name=value`, the way `list` prints it."""
    run("add", "measurement", "--target", "project", "--body", "deploy",
        "--measure", "t_epoch=1700000000", "--measure", "ratio=0.25", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--body", "nothing", store_dir=tmp_path)
    capsys.readouterr()
    for pattern in (["t_epoch"], ["1700000000"], ["^ratio=0.25$"], ["t_epoch=17", "-F"]):
        run("list", "--grep", *pattern, "--json", store_dir=tmp_path)
        got = json.loads(capsys.readouterr().out)
        assert [r["body"] for r in got] == ["deploy"], pattern


def test_supersede_target_moves_one_entry(tmp_path, capsys):
    """`--target` moves one entry and keeps its id; `rename` moves every
    entry on a name. Beside `--add-tag`, it is not dropped by the tag-only
    fast path, which takes no field but the tags."""
    run("add", "note", "--target", "item:a", "--body", "one", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("add", "note", "--target", "item:a", "--body", "two", store_dir=tmp_path)
    capsys.readouterr()
    assert run("supersede", nid, "--target", "item:b", "--add-tag", "t", store_dir=tmp_path) == 0
    assert capsys.readouterr().out.strip() == nid
    heads = {n.body: n for n in store.heads(store.load(tmp_path))}
    assert (heads["one"].target.name, list(heads["one"].tags)) == ("b", ["t"])
    assert heads["two"].target.name == "a"


def test_list_grep_takes_greps_dash_i_as_a_no_op(tmp_path, capsys):
    """Agents carried grep's `-i` over, and `--grep -i PATTERN` refused with
    `expected one argument` (repeatedly, 2026-10-14)."""
    run("add", "note", "--target", "project", "--body", "Drawer", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--body", "nothing", store_dir=tmp_path)
    capsys.readouterr()
    for argv in (["--grep", "-i", "drawer"], ["-i", "--grep", "drawer"], ["--grep", "drawer", "-i"]):
        assert run("list", *argv, "--json", store_dir=tmp_path) == 0, argv
        got = json.loads(capsys.readouterr().out)
        assert [r["body"] for r in got] == ["Drawer"], argv


def test_list_grep_finds_a_pasted_id_as_the_gui_search_does(tmp_path, capsys):
    """`--grep 412907-3be`, a row's id tail as handoffs cite it, read `0 of
    N match` while `show` found the row (2026-10-07): the pattern reads
    text, not ids. A pasted id or tail joins the hits by the GUI search's
    rule, and a superseded one brings its current row. Digits inside an id
    are no tail: `307` would hit unrelated ids. Each branch of an
    alternation is tested as a tail: `tailA|tailB` returned neither row,
    only a row whose body cited one, and read as a hit (2026-10-11)."""
    run("add", "task", "--target", "project", "--body", "x", store_dir=tmp_path)
    old = capsys.readouterr().out.strip()
    run("add", "note", "--target", "project", "--body", "y", store_dir=tmp_path)
    other = capsys.readouterr().out.strip()
    run("resolve", old, store_dir=tmp_path)
    new = capsys.readouterr().out.strip()
    assert new == old, "a resolve keeps the entry's id"
    old = _earlier(tmp_path, capsys, new)

    def ids(*argv):
        run("list", *argv, "--json", store_dir=tmp_path)
        return sorted(r["id"] for r in json.loads(capsys.readouterr().out))

    assert ids("--grep", new[-10:]) == [new]
    assert ids("--grep", new, "-F") == [new]
    assert ids("--grep", old[-10:]) == [new], "a superseded id brings its current entry"
    assert ids("--grep", old[-10:], "--all") == sorted([old, new])
    assert ids("--grep", new[-10:], "--kind", "note") == [], "the other filters still apply"
    assert ids("--grep", new[-10:-4]) == []
    both = f"{new[-10:]}|{other[-10:]}"
    assert ids("--grep", both) == sorted([new, other])
    assert ids("--grep", both, "-F") == [], "-F reads the | as a character"


def test_list_grep_takes_a_literal_and_names_a_regex_zero(tmp_path, capsys):
    """`--grep '$HOME'` read 0 against 1: `$` is an anchor, so a literal
    pasted from a row could never match, and the 0 read as absence. -F takes
    the pattern literally. A regex holding operators that matches nothing
    says so on stderr when the literal would match; a plain word that
    matches nothing, a regex that matches, and a deliberate regex that
    matches nothing either way stay quiet. ^ and $ anchor a line, as in grep."""
    run("add", "note", "--target", "project", "--body", "set $HOME first\nthen [x]",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("add", "note", "--target", "project", "--body", "unrelated", store_dir=tmp_path)
    capsys.readouterr()

    def ids(*argv):
        run("list", *argv, "--json", store_dir=tmp_path)
        c = capsys.readouterr()
        return [r["id"] for r in json.loads(c.out)], c.err

    got, err = ids("--grep", "$HOME")
    assert got == [] and "as a literal (-F) it matches 1" in err and "$" in err, err
    assert ids("--grep", "absent|gone") == ([], ""), "a deliberate regex's miss is a miss"
    assert ids("--grep", "$HOME", "-F") == ([nid], "")
    assert ids("-F", "--grep", "then [x]") == ([nid], "")
    assert ids("--grep", "^then") == ([nid], "")
    assert ids("--grep", "absent") == ([], "")

    run("list", "--grep", "$HOME", "-F", store_dir=tmp_path)
    assert capsys.readouterr().out.startswith("1 of 2 match --grep '$HOME' -F")
    assert run("list", "-F", store_dir=tmp_path) == 2
    assert "--grep" in capsys.readouterr().err


def test_list_fixed_strings_names_a_regex_that_would_match(tmp_path, capsys):
    """The reverse of the hint above, and the one -F direction it missed.
    `-F` takes `|` literally too, so an alternation given to it read 0 and
    looked like "nothing filed yet", while the same pattern as a regex
    matched 17 (an adopter, 2026-09-28). Only `|` earns the hint: `.`, `$`
    and `(` are what -F exists FOR, so a hint on them would fire on the
    ordinary reads. Quiet when the literal matches, quiet when the regex
    matches nothing either, quiet with no `|`."""
    run("add", "note", "--target", "project", "--body", "set $HOME first\nthen [x]",
        store_dir=tmp_path)
    capsys.readouterr()
    run("add", "note", "--target", "project", "--body", "a|b stays literal",
        store_dir=tmp_path)
    pipe = capsys.readouterr().out.strip()

    def ids(*argv):
        run("list", *argv, "--json", store_dir=tmp_path)
        c = capsys.readouterr()
        return [r["id"] for r in json.loads(c.out)], c.err

    got, err = ids("--grep", "set|then", "-F")
    assert got == [] and "as a regex it matches 1" in err and "'|'" in err, err
    assert ids("--grep", "a|b", "-F") == ([pipe], ""), "a literal that matches is quiet"
    assert ids("--grep", "absent|gone", "-F") == ([], ""), "0 either way is a plain miss"
    assert ids("--grep", "nowhere", "-F") == ([], ""), "no | in the pattern, no hint"


def test_list_grep_searches_refs(tmp_path, capsys):
    """SKILL.md sends the second object to `--ref` "instead of naming it in
    prose", then sends readers to `--grep` before they say the store holds
    nothing on it. A ref the grep could not see made those two rules a false
    "no row about this". One case per ref shape: a named ref, and a project
    ref, whose name is None."""
    run("add", "note", "--target", "item:x", "--ref", "item:lib/widget.ts",
        "--body", "the body never names it", store_dir=tmp_path)
    named = capsys.readouterr().out.strip()
    run("add", "note", "--target", "item:y", "--ref", "project", store_dir=tmp_path)
    proj = capsys.readouterr().out.strip()
    run("add", "note", "--target", "item:z", "--body", "no ref", store_dir=tmp_path)
    capsys.readouterr()

    for pat, want in (("widget", [named]), ("item:lib/", [named]),
                      ("^project:$", [proj])):
        run("list", "--grep", pat, "--json", store_dir=tmp_path)
        assert [r["id"] for r in json.loads(capsys.readouterr().out)] == want, pat


def test_add_names_the_open_heads_already_on_its_target(tmp_path, capsys):
    """Revisions landed as new rows beside the head they revised, and the
    revised head stayed open. add names what is already open there, so the
    choice is made knowingly. project is everyone's target and says nothing;
    a parked row is not open work; a resolved one is done."""
    run("add", "task", "--target", "item:x", store_dir=tmp_path)
    task = capsys.readouterr().out.strip()
    run("add", "idea", "--target", "item:x", store_dir=tmp_path)
    run("add", "bug", "--target", "item:x", store_dir=tmp_path)
    bug = capsys.readouterr().out.split()[-1]
    run("resolve", bug, store_dir=tmp_path)
    run("add", "task", "--target", "project", store_dir=tmp_path)
    capsys.readouterr()

    run("add", "note", "--target", "item:x", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert "1 open on item:x; " in err and "supersede" in err and f"\n  {task}\n" in err, err
    for target in ("item:fresh", "project"):
        run("add", "task", "--target", target, store_dir=tmp_path)
        assert "open on" not in capsys.readouterr().err, target


def test_the_open_rows_note_lists_a_target_once_a_session(tmp_path, capsys, monkeypatch):
    """On a busy file every add listed the same unrelated rows (2026-10-15).
    In an agent session a target's rows are listed
    at its first add there, and a later add prints the count. Session start
    lists them again: a compacted session has lost them."""
    bodies = (f"b{i}" for i in range(20))

    def add(target):
        run("add", "task", "--target", target, "--body", next(bodies), store_dir=tmp_path)
        c = capsys.readouterr()
        return c.out.strip(), c.err
    first, _ = add("item:x")
    for _ in range(2):
        assert "if this entry settles or revises one" in add("item:x")[1], "no session: each time"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert f"\n  {first}  b0" in add("item:x")[1]
    err = add("item:x")[1]
    assert "note: 4 open on item:x, listed at this session's first add there " \
           "(context --target item:x)\n" in err and first not in err, err
    add("item:y")
    assert "if this entry settles" in add("item:y")[1], "each target its own list"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "s1"}')))
    run("summary", "--hook", store_dir=tmp_path)
    capsys.readouterr()
    assert "if this entry settles" in add("item:x")[1], "session start lists them again"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    assert "if this entry settles" in add("item:y")[1], "another session gets its own list"


def test_the_clip_note_skips_a_pre_registration(tmp_path, capsys):
    """An adopter's pre-registrations open with their registration stamp by
    convention, the fact that makes them pre-registrations, and the clip
    note fired on all four in a session and never helped (dogfood,
    2026-09-27). Session start names the registration date itself."""
    _declare(tmp_path, PREREG)
    long = "PRE-REGISTERED 2026-09-27T13:30Z, BEFORE any contact. " + "detail " * 20
    # exit 0 first: a refused write is quiet too, and would pass the first check
    assert run("add", "prediction", "--target", "item:p", "--checked", "c", "--body", long,
               store_dir=tmp_path) == 0
    assert "first 100 chars" not in capsys.readouterr().err
    assert run("add", "task", "--target", "item:t", "--body", long, store_dir=tmp_path) == 0
    assert "first 100 chars" in capsys.readouterr().err, "a status-only kind still gets it"


def test_the_clip_note_comes_once_a_session_then_for_a_dated_opening(tmp_path, capsys,
                                                                     monkeypatch):
    """Each `add` is its own process, so one session's batches printed the
    clip note 23 times, each echoing text just written (2026-10-11). In an
    agent session it prints once; after that, only for a lead whose short
    opening clause holds a date, which is provenance, not the claim."""
    def err(body, target):
        assert run("add", "task", "--target", target, "--body", body, store_dir=tmp_path) == 0
        return capsys.readouterr().err
    plain, dated = ("Cut the cache. " + "context " * 20,
                    "IDEA (user, 2026-09-26): cut the cache. " + "context " * 20)
    for i in range(2):
        assert "first 100 chars" in err(plain, f"item:none{i}"), "no session: every time"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert "check that they carry" in err(plain, "item:a")
    assert "first 100 chars" not in err(plain, "item:b"), "once a session"
    said = err(dated, "item:c")
    assert "opens on a date" in said and "IDEA (user" in said, said
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    assert "check that they carry" in err(plain, "item:d"), "another session hears it"


def test_a_write_whose_text_runs_past_500_chars_says_so(tmp_path, capsys, monkeypatch):
    """SKILL.md asks each write to an entry, a new one or an addition, for
    about 500 characters; per write, not per entry (the user, 2026-10-14)."""
    def err(*args):
        assert run(*args, store_dir=tmp_path) == 0
        return capsys.readouterr().err
    long, short = "x" * 501, "y" * 500
    said = "an entry, or an addition to one, is about 500"
    assert "this write's text runs 501 chars; " + said in err(
        "add", "note", "--target", "item:a", "--body", long)
    nid = store.load(tmp_path)[0].id
    assert said not in err("add", "note", "--target", "item:b", "--body", short)
    assert said in err("supersede", nid, "--append", "--body", "z" * 600)
    assert said not in err("supersede", nid, "--append", "--body", "a short addition")
    assert said not in err("supersede", nid, "--add-tag", "t"), "no text, however long the body"
    run("add", "task", "--target", "item:c", "--body", "c", store_dir=tmp_path)
    task = capsys.readouterr().out.strip()
    assert said in err("resolve", task, "--body", "w" * 700)
    rows = "".join(json.dumps({"kind": "note", "target": {"type": "item", "name": f"i{k}"},
                               "body": b}) + "\n" for k, b in enumerate(["s", "l" * 900, "m"]))
    monkeypatch.setattr("sys.stdin", io.StringIO(rows))
    got = err("add", "--from-json", "-")
    assert re.search(r"note: 1 of 3 entries run long: \d{6}-[0-9a-f]{3} \(900 chars\); " + said,
                     got), got


def test_a_status_write_shows_the_head_session_start_will_print(tmp_path, capsys):
    """Session start clips an open row to HEAD_CHARS, and the first 100 chars
    of a status row were usually context, not the claim or the ask
    (2026-09-26). add, and supersede with a body, show the writer that cut on
    stderr. Quiet when the body fits, on a plain or parked kind, in an active
    arc, on a tags-only supersede, and on a resolve."""
    long = "From elsewhere, 2026-09-26. " + "context " * 20 + "THE ASK"
    head = summ.clip(long, summ.HEAD_CHARS)
    run("arc", "create", "--name", "a", "--scope", "item", store_dir=tmp_path)
    capsys.readouterr()

    run("add", "question", "--target", "item:x", "--body", long, store_dir=tmp_path)
    c = capsys.readouterr()
    nid = c.out.strip()
    assert f"{nid}  {head}" in c.err and "claim" in c.err, c.err
    assert "THE ASK" not in c.err

    run("add", "question", "--target", "item:y", "--body", "short ask", store_dir=tmp_path)
    short = capsys.readouterr().out.strip()
    assert run("supersede", short, "--body", long, store_dir=tmp_path) == 0
    c = capsys.readouterr()
    assert f"{c.out.strip()}  {head}" in c.err, c.err
    quiet = (("add", "question", "--target", "item:z", "--body", "short ask"),
             ("add", "note", "--target", "item:z", "--body", long),
             ("add", "idea", "--target", "item:z", "--body", long),
             ("add", "task", "--target", "item:z", "--arc-id", "a", "--body", long),
             ("supersede", c.out.strip(), "--add-tag", "t"),
             # --append never moves a lead it keeps (fired 3 times, 2026-10-01)
             ("supersede", c.out.strip(), "--append", "--body", long),
             ("resolve", nid, "--body", long))
    for argv in quiet:
        assert run(*argv, store_dir=tmp_path) == 0, argv
        assert "session start shows" not in capsys.readouterr().err, argv
    run("add", "task", "--target", "item:bare", store_dir=tmp_path)
    bare = capsys.readouterr().out.strip()
    assert run("supersede", bare, "--append", "--body", long, store_dir=tmp_path) == 0
    assert head in capsys.readouterr().err, "appended to no body, the text is the lead"

    rows = tmp_path / "entries.jsonl"
    rows.write_text("".join(json.dumps({"kind": "task", "body": long,
                                        "target": {"type": "item", "name": f"b{i}"}}) + "\n"
                            for i in range(5)))
    run("add", "--from-json", str(rows), store_dir=tmp_path)
    err = capsys.readouterr().err
    assert err.count(head) == 5 and "more" not in err, err


def test_a_gotcha_write_shows_the_cut_the_hook_will_print(tmp_path, capsys):
    """The gotcha hook gives a session a row's first GOTCHA_CHARS, and every
    gotcha written in its first days ran longer, the median three times as
    long (2026-10-14). The trap and its fix go in a first paragraph that fits;
    evidence may follow below it (the user, 2026-10-14). add and supersede
    show the writer the cut whenever a write leaves a gotcha the hook prints
    with a longer first paragraph."""
    long = "The trap and its fix. " + "evidence " * 30 + "SECOND TRAP"
    cut = summ.clip(long, summ.GOTCHA_CHARS)
    _declare(tmp_path, '[catalogs]\nfile = "echo a.py; echo b.py; echo c.py"\n')

    def write(*argv):
        assert run(*argv, store_dir=tmp_path) == 0, argv
        c = capsys.readouterr()
        return c.out.strip(), c.err

    nid, err = write("add", "note", "--target", "file:a.py", "--tag", "gotcha", "--body", long)
    assert f"{nid}  first paragraph {len(long)} chars: {cut}" in err and "SECOND TRAP" not in err, err
    short, err = write("add", "note", "--target", "file:b.py", "--tag", "gotcha",
                       "--body", "The trap and its fix.\n\n" + "evidence " * 30)
    assert "gotcha hook shows" not in err, "evidence below a first paragraph that fits"
    assert "gotcha hook shows" not in write("supersede", short, "--append", "--body", long)[1], \
        "an append goes below the first paragraph"
    assert "gotcha hook shows" in write("supersede", short, "--prepend", "--body", long)[1], \
        "a prepend is the new first paragraph"
    plain, err = write("add", "note", "--target", "file:c.py", "--body", long)
    assert "gotcha hook shows" not in err, "no tag"
    assert "gotcha hook shows" in write("supersede", plain, "--add-tag", "gotcha")[1], \
        "tagged, it is one"
    for argv in (("supersede", plain, "--add-tag", "retired"),
                 ("add", "note", "--target", "item:x", "--tag", "gotcha", "--body", long)):
        assert "gotcha hook shows" not in write(*argv)[1], "the hook prints neither"


def test_a_gotcha_no_hook_can_print_says_so(tmp_path, capsys):
    """`add --tag gotcha` on an item or the project wrote a row no hook ever
    prints, and said nothing (dogfood, 2026-10-14). The
    write says so and names the ref that puts it in reach."""
    _declare(tmp_path, '[catalogs]\nfile = "echo deploy.sh"\n')
    never = "is tagged gotcha, but the hook never prints it"
    for target in ("item:deploy", "project"):
        assert run("add", "note", "--target", target, "--tag", "gotcha", "--body", "t",
                   store_dir=tmp_path) == 0
        out, err = capsys.readouterr()
        assert f"{out.strip()} {never}" in err and "`--ref file:PATH` gives it one" in err, err
    nid = _add(tmp_path, capsys, "note", "--target", "item:deploy", "--body", "t")
    assert run("supersede", nid, "--add-tag", "gotcha", store_dir=tmp_path) == 0
    assert never in capsys.readouterr().err, "a tag added later"
    assert run("add", "note", "--target", "item:deploy", "--ref", "file:deploy.sh",
               "--tag", "gotcha", "--body", "t", store_dir=tmp_path) == 0
    assert never not in capsys.readouterr().err, "a file ref puts it in reach"


def test_list_shows_no_status_the_kind_no_longer_holds(tmp_path, capsys):
    """An agent filters `list --json` with `jq 'select(.status == "open")'`,
    so the JSON must carry the status the kind's bits allow, not the raw
    field; the text row must not print `[open]` either."""
    store.ensure_store(tmp_path)
    toml = tmp_path / "symbion.toml"
    toml.write_text('[kinds]\nmeasurement = { status = true, verdict = true }\n')
    run("add", "--kind", "measurement", "--type", "project", "--body", "b", store_dir=tmp_path)
    toml.write_text('[kinds]\nmeasurement = { verdict = true }\n')
    capsys.readouterr()

    run("list", "--json", store_dir=tmp_path)
    row, = json.loads(capsys.readouterr().out)
    assert row["kind"] == "measurement" and row["status"] is None
    run("list", "--status", "open", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out) == []
    run("list", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "measurement" in out and "[open]" not in out


def test_rename_matching_nothing_exits_1(tmp_path):
    """Printing 're-targeted 0 entries' and exiting 0 reads as done."""
    assert run("rename", "old", "new", "--type", "item", store_dir=tmp_path) == 1


def test_rename_matching_something_exits_0(tmp_path):
    run("add", "--kind", "note", "--type", "item", "--name", "old",
        store_dir=tmp_path)
    assert run("rename", "old", "new", "--type", "item", store_dir=tmp_path) == 0


def test_rename_reports_refs_and_a_ref_only_name_exits_0(tmp_path, capsys):
    """The old line was a count with no second half: the ref it skipped never
    showed. A name only refs carry is a real rename, not the typo exit 1 is for."""
    run("add", "--kind", "note", "--type", "project", "--ref", "item:old",
        store_dir=tmp_path)
    capsys.readouterr()
    assert run("rename", "old", "new", "--type", "item", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == \
        "re-targeted 0 entries, re-pointed 1 ref: item:old -> item:new\n"


def test_rename_miss_names_the_stored_names_it_nearly_matched(tmp_path, capsys):
    """`rename <short sha> <new> --type commit` read `re-targeted 0` beside
    a row stored on the full sha. `old` stays exact: a live-catalog match would
    redirect a departed name. A STORED name is not a live match, so a miss
    names the stored names holding `old` (targets and refs, in full), else
    the close ones, else nothing more."""
    run("add", "note", "--target", "item:src/parser.py", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--ref", "item:lib/parser_util.py",
        store_dir=tmp_path)
    run("add", "note", "--target", "commit:parser1", store_dir=tmp_path)   # other type
    capsys.readouterr()

    assert run("rename", "parser", "new", "--type", "item", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "item:lib/parser_util.py, item:src/parser.py" in err, err
    assert "commit:" not in err
    assert run("rename", "src/parsr.py", "new", "--type", "item", store_dir=tmp_path) == 1
    assert "item:src/parser.py" in capsys.readouterr().err
    for miss in ("zzz", ""):
        assert run("rename", miss, "new", "--type", "item", store_dir=tmp_path) == 1
        assert capsys.readouterr().err == "", miss


def test_rename_resolves_the_new_name_the_way_add_does(repo, tmp_path, capsys):
    """rename wrote `new` as typed, where add resolves it: a short sha stayed
    short, and `context --commit` on that sha missed the moved row. The
    target and the re-pointed ref both land on the full sha."""
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "c1")
    old, new = (subprocess.run(["git", "rev-parse", r], cwd=repo, check=True,
                               capture_output=True, text=True).stdout.strip()
                for r in ("HEAD~1", "HEAD"))
    s = tmp_path / "s"
    run("add", "--kind", "note", "--type", "commit", "--name", old, store_dir=s)
    run("add", "--kind", "note", "--type", "project", "--ref", f"commit:{old}",
        store_dir=s)
    capsys.readouterr()
    assert run("rename", old, new[:7], "--type", "commit", store_dir=s) == 0
    assert capsys.readouterr().out.endswith(f" -> commit:{new}\n")
    hs = store.heads(store.load(s))
    assert [n.target.name for n in hs if n.target.type == "commit"] == [new]
    assert [r.name for n in hs for r in n.refs] == [new]


def test_rename_resolves_a_short_old_sha_against_the_stored_ones(repo, tmp_path, capsys):
    """A row stores a commit's full sha, and `rename SHORT NEW --type commit`
    re-targeted nothing after a history rewrite (2026-10-15). OLD resolves
    against the stored names, never git: the rewrite took the old commit
    away. A prefix of two stored shas refuses; one stored as typed wins."""
    s = tmp_path / "s"
    full = "0a1b2c3" + "a" * 33
    run("add", "note", "--target", f"commit:{full}", store_dir=s)
    run("add", "note", "--target", "project", "--ref", f"commit:{full}", store_dir=s)
    capsys.readouterr()
    assert run("rename", full[:7], "b" * 40, "--type", "commit", store_dir=s) == 0
    assert capsys.readouterr().out.startswith("re-targeted 1 entry, re-pointed 1 ref:")
    hs = store.heads(store.load(s))
    assert {o.name for n in hs for o in (n.target, *n.refs) if o.type == "commit"} == {"b" * 40}
    run("add", "note", "--target", "commit:" + "c" * 40, store_dir=s)
    run("add", "note", "--target", "commit:" + "c" * 7, store_dir=s)
    capsys.readouterr()
    assert run("rename", "bbbb", "d" * 40, "--type", "commit", store_dir=s) == 0
    assert run("rename", "c" * 7, "e" * 40, "--type", "commit", store_dir=s) == 0
    run("add", "note", "--target", "commit:" + "f" * 40, store_dir=s)
    run("add", "note", "--target", "commit:" + "f" * 39 + "0", store_dir=s)
    capsys.readouterr()
    assert run("rename", "fffff", "1" * 40, "--type", "commit", store_dir=s) == 1
    assert "\nerror: commit:fffff begins 2 stored commits: " in capsys.readouterr().err
    names = {n.target.name for n in store.heads(store.load(s))}
    assert {"c" * 40, "d" * 40, "e" * 40, "f" * 40} <= names


def test_rename_names_the_rows_whose_body_still_names_the_old_name(repo, tmp_path, capsys):
    """An --amend moved a check and a resolve's ref to the new sha, and the
    resolve still read "Fixed in <old>." until a hand supersede (2026-10-02).
    A commit is matched by the 7-character prefix a body cites."""
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "c1")
    old, new = (subprocess.run(["git", "rev-parse", r], cwd=repo, check=True,
                               capture_output=True, text=True).stdout.strip()
                for r in ("HEAD~1", "HEAD"))
    s = tmp_path / "s"
    run("add", "--kind", "note", "--type", "commit", "--name", old, "--body", "ran", store_dir=s)
    run("add", "--kind", "note", "--type", "project", "--ref", f"commit:{old}",
        "--body", f"Fixed in {old[:7]}.", store_dir=s)
    run("add", "--kind", "note", "--type", "item", "--name", "x", "--body", f"see {old[:7]}",
        store_dir=s)
    run("add", "--kind", "note", "--type", "item", "--name", "y", "--body", "unrelated", store_dir=s)
    capsys.readouterr()
    assert run("rename", old, new, "--type", "commit", store_dir=s) == 0
    named = [n.id for n in store.newest_first(store.heads(store.load(s))) if old[:7] in n.body]
    assert len(named) == 2
    assert capsys.readouterr().err == (
        f"note: {old[:7]} is still in the body of 2 entries: {', '.join(named)}; "
        f"supersede each to update it (list --grep '\\b{old[:7]}')\n")
    assert run("rename", "y", "z", "--type", "item", store_dir=s) == 0
    assert capsys.readouterr().err == "", "no body names y"
    # A name is matched as a word: a one-letter item named three unrelated
    # rows (the 0.3.0 review, 2026-10-05).
    for body in ("banana", "plan on a", "nothing"):
        run("add", "note", "--target", "item:q", "--body", body, store_dir=s)
    run("add", "note", "--target", "item:a", "--body", "on itself", store_dir=s)
    capsys.readouterr()
    assert run("rename", "a", "b", "--type", "item", store_dir=s) == 0
    err = capsys.readouterr().err
    assert "a is still in the body of 1 entry:" in err and r"(list --grep '\ba\b')" in err, err


def test_seed_dry_run_creates_nothing(tmp_path, capsys):
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "item", "--name", "a", "--name", "b",
        "--dry-run", store_dir=tmp_path)
    assert "2" in capsys.readouterr().out
    assert store.load(tmp_path) == []


def test_seed_item_scope_needs_explicit_names(tmp_path):
    """`item` has no catalog; seeding it without --name must fail loudly rather
    than run a catalog command that does not exist."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    assert run("arc", "seed", aid, "--scope", "item", store_dir=tmp_path) == 1


def test_add_tag_preserves_existing_tags(tmp_path, capsys):
    """`--tag` replaces inherited tags; `--add-tag` must keep them."""
    run("add", "--kind", "bug", "--type", "project", "--tag", "keepme",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--add-tag", "priority", store_dir=tmp_path)
    head = store.heads(store.load(tmp_path))[0]
    assert set(head.tags) == {"keepme", "priority"}


def test_arc_todo_unknown_id_exits_1(tmp_path):
    assert run("arc", "todo", "nope", store_dir=tmp_path) == 1


def test_an_unknown_arc_names_the_arcs_or_the_near_miss(tmp_path, capsys):
    """`no arc 'nope'` stopped at the miss (2026-09-22). Now: candidates
    when one is near (difflib), else the legal values, and always the
    command that lists them. One helper serves every site,
    so `list --arc` and `arc todo` say the same thing."""
    for name in ("alpha", "beta"):
        run("arc", "create", "--name", name, "--scope", "item", store_dir=tmp_path)
    capsys.readouterr()
    assert run("arc", "todo", "nope", store_dir=tmp_path) == 1
    assert capsys.readouterr().err == "no arc 'nope'; arcs: alpha, beta (arc list)\n"
    assert run("list", "--arc", "alpah", store_dir=tmp_path) == 1
    assert capsys.readouterr().err == "no arc 'alpah'; did you mean alpha? (arc list)\n"
    store.ensure_store(tmp_path / "empty")
    assert run("arc", "todo", "nope", store_dir=tmp_path / "empty") == 1
    assert capsys.readouterr().err == "no arc 'nope'; no arcs yet (arc create)\n"


def test_arc_todo_omits_a_null_status_task_which_summary_then_names(tmp_path, capsys):
    """The trade, both halves in one place. A row carrying status: null drops
    out of the checklist, because `arc todo` is the OPEN list and the row
    never said it was open. It does not vanish: summary counts it, so the
    store says how many such rows it holds on every run."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    capsys.readouterr()
    row = {
        "id": "legacy-1", "kind": "task",
        "target": {"type": "item", "name": "thing1"},
        "created_at": "2020-01-01T00:00:00", "author": "claude",
        "body": "", "status": None, "arc_id": aid,
    }
    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert rows == []
    from symbion import summary as summ
    from symbion.config import Config
    assert summ.summary(tmp_path, Config(project_root=tmp_path))["no_status"] == 1


def test_arc_todo_excludes_a_resolved_item(tmp_path, capsys):
    """The exclusion direction for an explicit status: resolve the only item
    and assert it's gone, so a filter deleted outright fails here even though
    the null-status sibling above would still pass."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "item", "--name", "thing1", store_dir=tmp_path)
    capsys.readouterr()
    nid = store.arc_items(store.load(tmp_path), aid)[0].id
    run("resolve", nid, store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert rows == []


def test_reconcile_apply_leaves_stale_open(tmp_path):
    """The safety property the tool is built around: --apply must never
    close a stale item on its own, or unconfigured [renames] would silently
    close real open work."""
    aid = _seeded_reconcile_arc(tmp_path)
    assert run("arc", "reconcile", aid, "--apply", store_dir=tmp_path) == 0
    items = {n.target.name: n.status for n in store.arc_items(store.load(tmp_path), aid)}
    assert items["stale1"] == "open"


def test_reconcile_resolve_stale_closes_it(tmp_path, capsys):
    """The reason goes below the task's body, as `resolve --body` puts it:
    replacing lost the claim the row was about (2026-09-26 audit)."""
    aid = _seeded_reconcile_arc(tmp_path)
    stale, = [n for n in store.load(tmp_path) if n.target.name == "stale1"]
    run("supersede", stale.id, "--body", "the claim", store_dir=tmp_path)
    assert run("arc", "reconcile", aid, "--resolve-stale", store_dir=tmp_path) == 0
    items = {n.target.name: n for n in store.arc_items(store.load(tmp_path), aid)}
    assert items["stale1"].status == "resolved"
    assert items["stale1"].body == ("the claim\n\nreconcile: target 'stale1' disappeared "
                                    "from the thing catalog; no rename evidence.")


def test_reconcile_leads_with_how_much_it_could_check(tmp_path, capsys):
    """On an arc of items reconcile printed `9 open task(s): 9 uncheckable`,
    which read as a clean pass while it checked nothing; the drift that
    mattered was found by hand (an adopter, 2026-09-26). The header leads
    with how many targets it could check, and why the rest could not be."""
    aid = _seeded_reconcile_arc(tmp_path)
    run("add", "task", "--target", "item:free", "--arc", aid, store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "reconcile", aid, store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head == ("3 open tasks; 2 of 3 checkable: 1 live, 1 stale; "
                    "1 uncheckable (item: no catalog)"), head
    run("arc", "create", "--name", "Items", store_dir=tmp_path)
    iid = capsys.readouterr().out.strip()
    run("arc", "seed", iid, "--name", "a", "--name", "b", store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "reconcile", iid, store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[0] == \
        "2 open tasks; 0 of 2 checkable (item: no catalog)"


def test_a_name_that_matches_no_target_names_the_ones_containing_it(tmp_path, capsys):
    """`list --name` given the start of a longer item name read `0 of N`
    beside a dozen rows on that item (an adopter, 2026-09-26): --name matches
    exactly, while add takes a unique substring. The miss names the targets
    that contain the string."""
    long = "upload retry: backoff that ignores the retry-after hint"
    for i in range(2):
        run("add", "note", "--target", f"item:{long}", "--body", f"b{i}", store_dir=tmp_path)
    run("add", "note", "--target", "item:unrelated", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    for argv in (["--name", "upload retry"],
                 ["--type", "item", "--name", "Upload Retry"]):
        assert run("list", *argv, "--json", store_dir=tmp_path) == 0
        out = capsys.readouterr()
        assert json.loads(out.out) == []
        assert f"item:{long} (2 entries)" in out.err and "unrelated" not in out.err, out.err
    run("list", "--name", f"{long}", store_dir=tmp_path)
    assert "contain" not in capsys.readouterr().err, "an exact name that matches says nothing"


def test_a_name_that_another_filter_empties_is_not_called_missing(tmp_path, capsys):
    """`list --target item:zed --status open` on a target whose only row was
    resolved printed `no target is named 'zed'; these contain it: item:zed`
    (2026-10-06): the hint fired on any empty result, and its substring scan
    counted the exact name. The footer already names the filter that hid it."""
    rid = _add(tmp_path, capsys, "bug", "--target", "item:zed", "--body", "b")
    run("resolve", rid, store_dir=tmp_path)
    run("add", "task", "--target", "item:zed two", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--target", "item:zed", "--status", "open", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert "+1 resolved (--status resolved)" in out
    assert "no target is named" not in err, err


def test_a_partial_page_ends_with_what_it_left_out(tmp_path, capsys):
    """A session read a 10-of-33 page as the whole answer and minted a
    second item for one subject (an adopter, 2026-09-26). The header said
    so; the eye stops at the last row. A partial page ends with the count."""
    for i in range(27):
        run("add", "note", "--target", f"item:n{i}", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[-1] == "  +2 more not shown (--limit 0 for all)"
    run("list", "--limit", "0", store_dir=tmp_path)
    assert "more not shown" not in capsys.readouterr().out


def test_a_partial_page_into_a_pipe_says_so_on_stderr(tmp_path, capsys, monkeypatch):
    """`symbion list --status open --full | grep -i -E 'etag|cache'` read
    only the first page of open rows, and grep deleted the header that said
    so (dogfood, 2026-10-11): a miss on the page read as absence. Not at a
    terminal."""
    for i in range(26):
        run("add", "note", "--target", f"item:n{i}", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--grep", "b", store_dir=tmp_path)
    assert capsys.readouterr().err == \
        "note: 26 of 26 match --grep b, showing 25 newest (--limit N, 0 for all)\n"
    run("list", "--limit", "0", store_dir=tmp_path)
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("list", store_dir=tmp_path)
    assert capsys.readouterr().err == "", "a person sees the header"
    # Claude Code's shell gives both streams one file, where the copy printed
    # the header twice. Real processes, so the streams are real files.
    code = "import sys; from symbion import cli; sys.exit(cli.main(sys.argv[1:]))"
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).parents[1])}

    def lst(stderr):
        return subprocess.run([sys.executable, "-c", code, "--dir", str(tmp_path), "list"],
                              stdout=subprocess.PIPE, stderr=stderr, text=True, env=env)
    one = lst(subprocess.STDOUT).stdout
    # Windows and OpenBSD give a pipe no file id, so there the copy stays.
    r, w = os.pipe()
    no_id = not os.fstat(w).st_ino
    os.close(r)
    os.close(w)
    assert one.count("26 entries, showing 25 newest") == (2 if no_id else 1), one
    two = lst(subprocess.PIPE)
    assert two.stderr.startswith("note: 26 entries, showing 25 newest"), two.stderr


@pytest.mark.parametrize("bad", ["-1", "x"])
def test_list_limit_takes_a_count(tmp_path, capsys, bad):
    """`--limit -1` sliced the oldest row off the page."""
    run("add", "note", "--target", "item:x", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--limit", bad, store_dir=tmp_path) == 2
    assert f"{bad!r} is not an entry count: give N, or 0 for all" in capsys.readouterr().err


def test_a_batch_add_ends_with_its_catalog_misses(tmp_path, capsys, monkeypatch):
    """Each catalog miss prints its own note, in line order among the rows
    that hit: in a 23-row batch one typo scrolled past and minted a second
    head (2026-09-23). A batch ends with one line naming every miss."""
    _declare(tmp_path, '[catalogs]\nfile = "printf \'a.py\\\\nb.py\\\\n\'"\n')
    rows = "".join(json.dumps({"kind": "note", "target": {"type": "file", "name": nm},
                               "body": "b"}) + "\n" for nm in ("a.py", "typo.py", "b.py"))
    monkeypatch.setattr("sys.stdin", io.StringIO(rows))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    last = _sans_pending(capsys.readouterr().err).splitlines()[-1]
    assert last == "note: 1 of 3 names not in their catalog, stored as typed: file:typo.py", last
    run("add", "note", "--target", "file:other.py", store_dir=tmp_path)
    assert "not in their catalog" not in capsys.readouterr().err, "one entry: its own note suffices"


def test_a_batch_on_a_resolver_type_names_no_misses(repo, tmp_path, capsys, monkeypatch):
    """A resolver mints a new name on purpose (a first sighting), so its
    type is left out of the batch's miss line."""
    # A FIXED catalog: a store-derived one lists the rows just written, so
    # nothing would ever be a miss and the check could not fail.
    (tmp_path / "mint.py").write_text("import sys; print(sys.stdin.readline().strip())\n")
    _declare(tmp_path, f'[catalogs]\nnum = "printf 1.00"\n'
                       f'[resolvers]\nnum = "{Path(sys.executable).as_posix()} '
                       f'{(tmp_path / "mint.py").as_posix()}"\n')
    rows = "".join(json.dumps({"kind": "note", "target": {"type": "num", "name": v},
                               "body": "b"}) + "\n" for v in ("5.00", "7.00"))
    monkeypatch.setattr("sys.stdin", io.StringIO(rows))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    assert {n.target.name for n in store.load(tmp_path)} == {"5.00", "7.00"}, "both new"
    assert "not in their catalog" not in capsys.readouterr().err


def test_a_closed_catalog_takes_only_the_names_it_lists(repo, tmp_path, capsys):
    """The user, 2026-10-14: an opt-in catalog that refuses a name it does
    not list. `[resolvers] TYPE = "closed"` keeps the built-in match and
    refuses its miss, with the near names, on a write and on a read. A name
    the store already holds still reads after the catalog drops it; a path
    typed as `file:./x` meets the closed catalog too. An open catalog still
    takes a miss as typed."""
    hosts = "[catalogs]\nhost = \"printf 'frontend-one\\\\nbackend-two\\\\n'\"\n"
    files = 'file = "git ls-files"\n'
    _declare(tmp_path, hosts + files + '[resolvers]\nhost = "closed"\nfile = "closed"\n')

    def add(target):
        rc = run("add", "note", "--target", target, "--body", target, store_dir=tmp_path)
        return rc, capsys.readouterr()
    rc, c = add("host:backend")
    assert rc == 0 and "resolved to 'backend-two' (unique substring" in c.err, c.err
    rc, c = add("host:frontend-onee")
    assert rc == 1 and c.out == "", c.out
    assert ("'frontend-onee' matches nothing in the host catalog, which is closed: it takes "
            "only the names it lists; did you mean 'frontend-one'? ([resolvers] host = "
            "\"closed\" in symbion.toml)") in c.err, c.err
    assert run("list", "--target", "host:frontend-onee", store_dir=tmp_path) == 1
    assert "which is closed" in capsys.readouterr().err, "a read's typo is refused too"
    rc, c = add("file:./new.py")
    assert rc == 1 and "file:./new.py is file:new.py, which it does not list, and the file " \
        "catalog is closed" in c.err, c.err
    assert add("file:./f")[0] == 0, "a listed path"
    assert {n.target.name for n in store.load(tmp_path)} == {"backend-two", "f"}

    _declare(tmp_path, "[catalogs]\nhost = \"printf 'frontend-one\\\\n'\"\n" + files
             + '[resolvers]\nhost = "closed"\n')
    run("list", "--target", "host:backend-two", store_dir=tmp_path)
    assert "host:backend-two" in capsys.readouterr().out, "a stored name still reads"
    _declare(tmp_path, hosts + files)
    rc, c = add("host:frontend-onee")
    assert rc == 0 and "taken as typed; did you mean 'frontend-one'?" in c.err, c.err
    rc, c = add("host:edge")
    assert rc == 0 and "'edge' matches nothing in the host catalog; taken as typed" in c.err
    _declare(tmp_path, "[catalogs]\nhost = \"printf 'edge-three\\\\n'\"\n" + files
             + '[resolvers]\nhost = "closed"\n')
    run("list", "--target", "host:edge", store_dir=tmp_path)
    c = capsys.readouterr()
    assert "host:edge " in c.out and "'edge' is a stored host name, read as typed; the " \
        "catalog resolves it to 'edge-three'" in c.err, c


def test_a_catalog_name_with_a_dot_segment_its_catalog_lacks_is_refused(repo, tmp_path,
                                                                         capsys):
    """`--ref doc:../archive/x.py` stored a junk doc target at exit 0; its
    note went to a stderr the agent had piped away (the user, 2026-10-19:
    refuse it). No name a catalog lists has a `.` or `..` segment, unless
    the catalog says so; `file:` reads such a name as a path."""
    _declare(tmp_path, "[catalogs]\nlib = \"printf '../shared/x\\\\n'\"\n")
    (tmp_path / "docs" / "x.md").write_text("x")

    def add(*a):
        rc = run("add", "note", *a, "--body", "b", store_dir=tmp_path)
        return rc, capsys.readouterr().err
    rc, err = add("--target", "doc:../archive/x.py")
    assert rc == 1 and ("doc:../archive/x.py has a '..' segment, which no name the doc "
                        "catalog lists has; a doc: name is its path within the store's "
                        "docs/") in err, err
    rc, err = add("--target", "project", "--ref", "doc:./x.md")
    assert rc == 1 and "doc:./x.md has a '.' segment" in err, err
    assert add("--target", "lib:../shared/x")[0] == 0, "a name the catalog lists"
    assert [n.target.name for n in store.load(tmp_path)] == ["../shared/x"]


def test_reconcile_without_flags_mutates_nothing(tmp_path):
    aid = _seeded_reconcile_arc(tmp_path)
    before = store.load(tmp_path)
    assert run("arc", "reconcile", aid, store_dir=tmp_path) == 0
    assert store.load(tmp_path) == before


def test_init_writes_symbion_toml(repo, tmp_path):
    assert run("init", "--yes", store_dir=tmp_path) == 0
    assert (tmp_path / "symbion.toml").exists()


def test_init_that_git_cannot_init_fails_and_leaves_no_partial_git(repo, tmp_path,
                                                                   monkeypatch, capsys):
    """A failed `git init` (here a bad init.defaultBranch) exited 0 beside git's
    own `fatal:` line, and the partial `.git` it left made every later `init`
    skip git: the store could never commit (2026-09-29)."""
    s = tmp_path / "s"
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "init.defaultBranch",
           "GIT_CONFIG_VALUE_0": "a..b"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert run("init", "--yes", store_dir=s) == 1
    assert "invalid branch name" in capsys.readouterr().err
    assert not (s / ".git").exists()
    for k in env:
        monkeypatch.delenv(k)
    assert run("init", "--yes", store_dir=s) == 0                       # a re-run recovers
    assert subprocess.run(["git", "-C", str(s), "rev-parse", "--git-dir"],
                          capture_output=True, text=True).stdout.strip() == ".git"

def test_commit_leaves_no_uncommitted_rows(tmp_path):
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    assert run("commit", "-m", "test commit", store_dir=tmp_path) == 0
    assert gitref.uncommitted(tmp_path) == (0, False)


def test_resolve_marks_the_head_resolved(tmp_path, capsys):
    run("add", "--kind", "bug", "--type", "project", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("resolve", nid, store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert head.status == "resolved"


def test_arc_rename_keeps_id_and_checkboxes(tmp_path):
    run("arc", "create", "--name", "old-name", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "item", "--name", "thing1", store_dir=tmp_path)
    assert run("arc", "rename", aid, "new-name", store_dir=tmp_path) == 0
    acts = store.load_arcs(tmp_path)
    assert acts[0].id == aid
    assert acts[0].name == "new-name"
    assert len(store.arc_items(store.load(tmp_path), aid)) == 1


def test_arc_archive_hides_from_list_but_keeps_checkboxes(tmp_path, capsys):
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "item", "--name", "thing1", store_dir=tmp_path)
    assert run("arc", "archive", aid, store_dir=tmp_path) == 0
    capsys.readouterr()
    run("arc", "list", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert aid not in [r["id"] for r in rows]
    assert len(store.arc_items(store.load(tmp_path), aid)) == 1
    # An archived campaign's done/total was unreachable from the CLI
    # (2026-10-03): --all shows it, and the plain view says it hides one.
    run("arc", "list", store_dir=tmp_path)
    assert "+1 archived (arc list --all)" in capsys.readouterr().out
    run("arc", "list", "--all", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert [(r["id"], r["archived"], r["done"], r["total"]) for r in rows] == [(aid, True, 0, 1)]
    run("arc", "list", "--all", store_dir=tmp_path)
    assert "0/1  x  (archived)" in capsys.readouterr().out


def test_an_arc_id_that_names_no_arc_is_refused_on_write(tmp_path, capsys):
    """`add --arc-id nosuch` stored the row at exit 0, and summary, which
    counts a row with an arc_id on that arc's line, showed it nowhere. An
    archived arc is still an arc: a closing note may be filed there."""
    run("arc", "create", "--name", "probe", "--scope", "item", store_dir=tmp_path)
    run("arc", "create", "--name", "old", "--scope", "item", store_dir=tmp_path)
    run("arc", "archive", "old", store_dir=tmp_path)
    run("add", "task", "--target", "item:x", store_dir=tmp_path)
    nid = capsys.readouterr().out.split()[-1]

    assert run("add", "task", "--target", "item:y", "--arc-id", "prob",
               store_dir=tmp_path) == 1
    assert "no arc 'prob'; did you mean probe?" in capsys.readouterr().err
    assert run("supersede", nid, "--arc-id", "prob", store_dir=tmp_path) == 1
    assert "no arc 'prob'" in capsys.readouterr().err
    rows = tmp_path / "entries.jsonl"
    rows.write_text(json.dumps({"kind": "task", "target": {"type": "item", "name": "y"},
                                "arc_id": "prob"}) + "\n")
    assert run("add", "--from-json", str(rows), store_dir=tmp_path) == 1
    assert "line 1: no arc 'prob'" in capsys.readouterr().err
    assert len(store.load(tmp_path)) == 1

    for argv in (("add", "note", "--target", "item:y", "--arc-id", "old"),
                 ("add", "task", "--target", "item:y", "--arc-id", "probe"),
                 ("supersede", nid, "--arc-id", "probe"),
                 ("supersede", nid, "--arc-id", "")):
        assert run(*argv, store_dir=tmp_path) == 0, argv


def test_arc_archive_says_where_its_open_rows_go(tmp_path, capsys):
    """Archiving an arc that still held open rows printed only its id, and
    the rows left every summary block. They now list outside arcs; the
    archive says so, and says nothing when the arc is done."""
    for name in ("busy", "done"):
        run("arc", "create", "--name", name, "--scope", "item", store_dir=tmp_path)
    run("add", "task", "--target", "item:a", "--arc-id", "busy", store_dir=tmp_path)
    run("add", "task", "--target", "item:b", "--arc-id", "busy", store_dir=tmp_path)
    run("add", "task", "--target", "item:c", "--arc-id", "done", store_dir=tmp_path)
    run("resolve", capsys.readouterr().out.split()[-1], store_dir=tmp_path)
    capsys.readouterr()

    assert run("arc", "archive", "busy", store_dir=tmp_path) == 0
    c = capsys.readouterr()
    assert c.out == "busy\n"
    assert "2 open entries in busy, now listed outside arcs" in c.err, c.err
    assert run("arc", "archive", "done", store_dir=tmp_path) == 0
    assert capsys.readouterr().err == ""


def test_cli_resolve_records_the_resolver_not_the_original_author(tmp_path, capsys):
    """The resolver is recorded, not the original author, end to end through the CLI."""
    run("add", "--kind", "bug", "--type", "project", "--author", "claude",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("resolve", nid, "--author", "ada", store_dir=tmp_path)
    head = store.heads(store.load(tmp_path))[0]
    assert head.author == "ada"
    assert head.status == "resolved"


def test_list_json_carries_check_state_and_distance(repo, tmp_path, capsys):
    """`list --kind check` renders the state; `--json` carries it
    as a field. Was dead code -- check_state had no non-test caller."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "check", "--type", "project", "--checked", "pytest -q",
        "--result", "412 passed", store_dir=store_dir)
    capsys.readouterr()
    run("list", "--kind", "check", "--json", store_dir=store_dir)
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "current"
    assert rows[0]["distance"] == 0


def test_list_text_renders_the_check_state(repo, tmp_path, capsys):
    store_dir = tmp_path / "store"
    run("add", "--kind", "check", "--type", "project", "--checked", "pytest -q",
        "--result", "412 passed", store_dir=store_dir)
    capsys.readouterr()
    run("list", "--kind", "check", store_dir=store_dir)
    assert "state=current" in capsys.readouterr().out


def test_an_open_pre_registration_reads_pending_until_its_result(repo, tmp_path, capsys):
    """A pre-registration is stamped at `add`, before its run, so it listed
    `checked at <sha>` and `state=current` as if the run had happened
    there (a fresh agent read its own unrun prediction that way,
    2026-09-29). Open, it says what will be checked and where it was
    registered; resolved, the restamped row reads as any check does."""
    store_dir = tmp_path / "store"
    _declare(store_dir, PREREG)
    run("add", "prediction", "--target", "item:p", "--checked", "the sweep",
        "--body", "falsified if X", store_dir=store_dir)
    pid = capsys.readouterr().out.strip()
    sha = store.load(store_dir)[0].provenance["sha"][:7]
    run("list", "--kind", "prediction", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "state=pending" in out and f"to check, registered at {sha}: the sweep" in out, out
    assert "checked at" not in out and "state=current" not in out, out
    run("list", "--kind", "prediction", "--json", store_dir=store_dir)
    row = json.loads(capsys.readouterr().out)[0]
    assert (row["state"], row["distance"]) == ("pending", None)

    # A result cleared with `--result ''` is no result: still pending.
    run("supersede", pid, "--result", "HELD", store_dir=store_dir)
    run("supersede", capsys.readouterr().out.strip(), "--result", "", store_dir=store_dir)
    pid = capsys.readouterr().out.strip()
    run("list", "--kind", "prediction", store_dir=store_dir)
    assert "state=pending" in capsys.readouterr().out

    run("resolve", pid, "--result", "HELD", store_dir=store_dir)
    capsys.readouterr()
    run("list", "--kind", "prediction", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "state=current" in out and f"checked at {sha}: the sweep" in out, out
    assert "pending" not in out and "to check" not in out, out


def test_an_open_two_bit_row_with_a_result_reads_as_the_check_it_is(repo, tmp_path, capsys):
    """The other shape of an open status+verdict row: a kind declared with
    both bits for finished verifications (two adopter stores do), added
    with its result. It ran at its stamp, so `pending` would be false; the
    result, not the open status, says a run happened."""
    store_dir = tmp_path / "store"
    _declare(store_dir, '[kinds]\naudit = { status = true, verdict = true }\n')
    run("add", "audit", "--target", "item:a", "--checked", "the harness", "--result", "30/30",
        store_dir=store_dir)
    capsys.readouterr()
    sha = store.load(store_dir)[0].provenance["sha"][:7]
    run("list", "--kind", "audit", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "[open] state=current" in out and f"checked at {sha}: the harness" in out, out
    assert "pending" not in out and "to check" not in out, out


def test_an_open_external_pre_registration_reads_pending_without_a_sha(repo, tmp_path, capsys):
    """An `--external` stamp has no sha to be registered at."""
    store_dir = tmp_path / "store"
    _declare(store_dir, PREREG)
    run("add", "prediction", "--target", "item:p", "--external", "--checked", "the probe",
        store_dir=store_dir)
    capsys.readouterr()
    assert run("list", "--kind", "prediction", store_dir=store_dir) == 0
    out = capsys.readouterr().out
    assert "state=pending" in out and "to check, registered: the probe" in out, out


def test_a_tty_shows_an_open_pre_registration_as_pending(repo, tmp_path, capsys, tty):
    store_dir = tmp_path / "store"
    _declare(store_dir, PREREG)
    tty()
    pid = _add(store_dir, capsys, "prediction", "--target", "item:p", "--checked", "the sweep",
               "--body", "falsified if X")
    sha = store.load(store_dir)[0].provenance["sha"][:7]
    run("show", pid, store_dir=store_dir)
    lines = _screen(capsys.readouterr().out)
    assert "pending" in lines[0] and "current" not in lines[0], lines
    assert any(ln.strip() == f"to check, registered at {sha}: the sweep" for ln in lines), lines
    assert not any("checked at" in ln for ln in lines), lines


def test_a_check_reads_as_labelled_lines_not_python_reprs(repo, tmp_path, capsys):
    """A check's head line carried `checked='…' result='…'` as Python reprs,
    `prov={'sha': '<40 hex>', 'dirty': False}` as a raw dict, and each commit
    ref as 40 hex, all on one wrapped line. The verdict gets its own lines,
    the provenance a short sha."""
    store_dir = tmp_path / "store"
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.strip()
    run("add", "--kind", "check", "--type", "project", "--checked", "pytest -q",
        "--result", "412 passed", "--ref", f"commit:{sha}", "--body", "why",
        store_dir=store_dir)
    capsys.readouterr()
    run("list", "--kind", "check", store_dir=store_dir)
    lines = capsys.readouterr().out.splitlines()
    assert f"refs=commit:{sha[:7]}  " in lines[1], lines[1]
    assert "prov=" not in lines[1] and sha not in lines[1] and "checked=" not in lines[1]
    assert lines[2:] == [f"    checked at {sha[:7]}: pytest -q", "    result: 412 passed",
                         "    why"], lines


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MD_BODY = "**shipped** at HEAD\n\n```\nraw stays\n```"


def test_list_on_a_tty_renders_the_markdown_body(repo, tmp_path, capsys, monkeypatch):
    """A body is markdown by contract. On a terminal the markers render; the
    words survive. A sibling pins the pipe, which prints the body as written.
    Both pass --full: the default is one clipped line, which never reaches
    the body printer (test_list_clips_bodies_to_one_line_unless_full)."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project", "--body", _MD_BODY,
        store_dir=store_dir)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("list", "--full", store_dir=store_dir)
    out = _ANSI.sub("", capsys.readouterr().out)
    assert "**" not in out, "bold markers must render, not print"
    assert "shipped" in out and "raw stays" in out, "the words must survive rendering"


def test_a_tag_in_a_body_prints_on_a_tty(repo, tmp_path, capsys, monkeypatch):
    """rich's parser reads `<pre>` as inline HTML and drops it, so a body
    that names a tag lost the word; the pipe prints it."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project",
        "--body", "wrap it in <pre> tags; ../<repo>-notes", store_dir=store_dir)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("list", "--full", store_dir=store_dir)
    out = _ANSI.sub("", capsys.readouterr().out)
    assert "wrap it in <pre> tags" in out and "../<repo>-notes" in out, out


def test_underscores_in_a_body_stay_on_a_tty(repo, tmp_path, capsys, monkeypatch):
    """CommonMark bolds `init` in `__init__.py`, and bodies name files and
    dunders: `_` makes no emphasis, at a terminal as in the GUI. `*` does."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project",
        "--body", "edit __init__.py and *really* __main__", store_dir=store_dir)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("list", "--full", store_dir=store_dir)
    out = _ANSI.sub("", capsys.readouterr().out)
    assert "edit __init__.py and really __main__" in out, out


_RICH_PROBE = """
import io, sys
from symbion import cli
def loaded():
    return any(m.split(".")[0] == "rich" for m in sys.modules)
cli.main(["--dir", sys.argv[1], "list", "--full"])
cli.main(["--dir", sys.argv[1], "summary"])
cli.main(["--dir", sys.argv[1], "schema"])
for argv in (["--help"], ["arc", "seed", "--help"], ["list", "--kind"]):
    try:
        cli.main(["--dir", sys.argv[1], *argv])
    except SystemExit:
        pass
piped = loaded()
class Tty(io.StringIO):
    def isatty(self):
        return True
sys.stdout = Tty()
cli.main(["--dir", sys.argv[1], "list"])
print(piped, loaded(), file=sys.stderr)
"""


def test_only_a_terminal_loads_rich(repo, tmp_path, capsys):
    """rich is a core dependency, but only a terminal uses it. Agents,
    scripts and the session-start hook read through a pipe, and loading rich
    costs about 50 ms a call: an import hoisted to the top of cli.py would
    charge every one of them. A fresh interpreter, since this one may have
    loaded rich already; the second value is the control."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project", "--body", "b", store_dir=store_dir)
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).parents[1])}
    r = subprocess.run([sys.executable, "-c", _RICH_PROBE, str(store_dir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stderr.splitlines()[-1] == "False True", r.stderr


def test_list_on_a_pipe_prints_the_body_as_written(repo, tmp_path, capsys):
    """The agent path: capsys is not a tty, so the markdown reaches the
    reader exactly as it was written."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project", "--body", _MD_BODY,
        store_dir=store_dir)
    capsys.readouterr()
    assert not sys.stdout.isatty()
    run("list", "--full", store_dir=store_dir)
    assert "**shipped** at HEAD" in capsys.readouterr().out
    # The default is the summary's one-liner: words kept, emphasis dropped.
    run("list", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "shipped at HEAD" in out and "**" not in out


@pytest.fixture
def tty(monkeypatch):
    """An 80-column truecolor terminal, the way rich reads one. Call it in
    the test body: during fixture setup sys.stdout is not yet capsys's
    stream, and a patch there never reaches the test."""
    for k, v in (("COLUMNS", "80"), ("TERM", "xterm-256color"), ("COLORTERM", "truecolor")):
        monkeypatch.setenv(k, v)
    for k in ("NO_COLOR", "FORCE_COLOR", "TTY_COMPATIBLE"):
        monkeypatch.delenv(k, raising=False)
    return lambda: monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def _screen(out):
    """What the terminal shows: one entry per line, colour codes dropped."""
    return [_ANSI.sub("", ln) for ln in out.splitlines()]


@pytest.mark.skipif(os.name == "nt", reason="rich reads a piped Windows console as "
                    "legacy, which takes no ANSI codes, so term prints no colour")
def test_a_16_colour_terminal_tells_kinds_and_states_apart(tmp_path):
    """At 16 colours rich's nearest match printed bug, task, question,
    decision and note, and a check's current and dangling, all as white
    (measured 2026-10-06, TERM=xterm). Each prints in its own ANSI colour
    there, and the palette the GUI's CSS reads stays hex."""
    store.ensure_store(tmp_path)
    for kind in ("bug", "task", "question", "decision", "note"):
        store.add(tmp_path, kind=kind, target={"type": "item", "name": kind}, status=None
                  if kind in ("decision", "note") else "open")
    env = {k: v for k, v in os.environ.items() if k not in ("COLORTERM", "NO_COLOR")}
    env.update(TERM="xterm", FORCE_COLOR="1", COLUMNS="80")
    script = (f"import sys; sys.stdout.isatty = lambda: True\n"
              f"from symbion import cli, term\n"
              f"assert all(c.startswith('#') for c in (term.TEXT, term.GOOD, *term.KIND.values()))\n"
              f"cli.main(['--dir', {str(tmp_path)!r}, 'list'])\n"
              f"p = term.painter()\n"
              f"print(repr(p('x', 'good')), repr(p('x', 'bad')))\n")
    r = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                       text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    kind_code = dict(m.groups()[::-1] for m in re.finditer(
        r"\x1b\[(\d+)m(bug|task|question|decision|note) ", r.stdout))
    assert len(kind_code) == len(set(kind_code.values())) == 5, (kind_code, r.stdout)
    assert "'\\x1b[32mx\\x1b[0m' '\\x1b[91mx\\x1b[0m'" in r.stdout, r.stdout


def test_list_on_a_tty_lines_up_columns_inside_the_width(repo, tmp_path, capsys, tty):
    """A person at a terminal read the pipe line as a wall: heads past 120
    characters, 160-character bodies soft-wrapped to column 0, and status
    and id wherever the target happened to end (2026-09-29). On a terminal
    with rich, every line fits the width, the id's tail leads, a mark
    carries the status, refs are a count, and the body sits under the
    target."""
    store_dir = tmp_path / "store"
    tty()
    tid = _add(store_dir, capsys, "task", "--target", "item:flaky upload", "--tag", "ci",
               "--tag", "flaky", "--ref", "item:upload service",
               "--body", "fails about one run in twenty on CI and never locally; " * 5)
    _add(store_dir, capsys, "note", "--target", "project", "--body", "short")
    run("list", store_dir=store_dir)
    raw = capsys.readouterr().out
    lines = _screen(raw)
    assert lines[0] == "2 entries", lines
    assert max(len(ln) for ln in lines) <= 80, lines
    head = next(ln for ln in lines if ln.startswith(tid[-10:]))
    assert "○ task" in head and head.endswith("item:flaky upload  #ci #flaky  1 ref"), head
    assert "[open]" not in raw and "refs=" not in raw and tid not in raw, raw
    body = lines[lines.index(head) + 1]
    assert body.lstrip().startswith("fails about one run in twenty"), body
    assert len(body) - len(body.lstrip()) == head.index("item:"), (head, body)
    assert "\x1b[38;2;" in raw, "the entries carry colour"


def test_a_long_tty_row_gives_up_refs_then_tags_before_its_target(repo, tmp_path, capsys, tty):
    store_dir = tmp_path / "store"
    tty()
    name = "the nightly runner upload test"
    tid = _add(store_dir, capsys, "task", "--target", f"item:{name}", "--ref", "item:x",
               *[a for t in ("alpha-tag", "bravo-tag", "charlie-tag", "delta-tag")
                 for a in ("--tag", t)])
    long_id = _add(store_dir, capsys, "task", "--target", "item:" + "a very long item name " * 4,
                   "--tag", "ci")
    # Target and tags fit in 80 columns, and the refs count would not: the
    # count goes whole, rather than cutting it to `1 r…`.
    fits = _add(store_dir, capsys, "task", "--target", "item:the nightly upload runner test",
                "--tag", "ci", "--tag", "flaky", "--ref", "item:x")
    run("list", store_dir=store_dir)
    lines = _screen(capsys.readouterr().out)
    head = next(ln for ln in lines if ln.startswith(fits[-10:]))
    assert head.endswith("item:the nightly upload runner test  #ci #flaky"), head
    head = next(ln for ln in lines if ln.startswith(tid[-10:]))
    assert f"item:{name}  #alpha-tag" in head and "#delta-tag" not in head, head
    assert head.endswith("…") and " ref" not in head and len(head) <= 80, head
    head = next(ln for ln in lines if ln.startswith(long_id[-10:]))
    assert "#ci" not in head and head.endswith("…") and len(head) <= 80, head


def test_a_tty_row_marks_what_is_resolved_and_what_was_revised(repo, tmp_path, capsys, tty):
    store_dir = tmp_path / "store"
    tty()
    tid = _add(store_dir, capsys, "task", "--target", "item:x", "--body", "do it")
    run("resolve", tid, store_dir=store_dir)
    rid = capsys.readouterr().out.strip()
    assert rid == tid, "a resolve keeps the entry's id"
    tid = _earlier(store_dir, capsys, rid)
    run("list", "--all", store_dir=store_dir)
    raw = capsys.readouterr().out
    lines = _screen(raw)
    new = next(ln for ln in lines if ln.startswith(rid[-10:]))
    old = next(ln for ln in lines if ln.startswith(tid[-10:]))
    assert "✓ task" in new and "○" not in new, new
    assert "✓ task" in old and old.endswith(f"superseded → {rid[-10:]}"), old
    # Resolved rows fade: the open-row text colour never appears on them.
    text = "38;2;205;214;244"
    assert text not in "".join(ln for ln in raw.splitlines() if rid[-10:] in ln), raw
    _add(store_dir, capsys, "task", "--target", "item:y")
    run("list", store_dir=store_dir)
    assert text in capsys.readouterr().out, "the control: an open entry does print it"


def test_a_tty_check_row_leads_with_its_result(repo, tmp_path, capsys, tty):
    store_dir = tmp_path / "store"
    tty()
    cid = _add(store_dir, capsys, "check", "--target", "item:dns", "--external",
               "--checked", "dig +short example.org", "--result", "one A record")
    run("list", store_dir=store_dir)
    lines = _screen(capsys.readouterr().out)
    head = next(ln for ln in lines if ln.startswith(cid[-10:]))
    assert "item:dns  external (<1d ago)" in head, head
    assert lines[lines.index(head) + 1].lstrip() == "result: one A record", lines
    # The age column is the ROW's; a supersede inherits the check's `at`, so
    # only the state says how old the run is (2026-09-29: `now … external`
    # for a check run 30 days before).
    old = store.add(store_dir, kind="check", target={"type": "item", "name": "old"},
                    author="t", checked="x", result="y",
                    provenance={"external": True, "at": "2026-01-01T00:00:00+00:00"})
    run("list", "--name", "old", store_dir=store_dir)
    days = summ.age_days("2026-01-01T00:00:00+00:00")
    head = next(ln for ln in _screen(capsys.readouterr().out) if ln.startswith(old.id[-10:]))
    assert f"item:old  external ({days}d ago)" in head, head


def test_a_check_stamped_with_a_non_string_sha_prints(repo, tmp_path, capsys, tty):
    """A provenance command's object is stored as printed, and `{"sha": 123}`
    crashed `check_state`, then each reader that sliced the sha: `list`
    printed a traceback (review, 2026-10-02)."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    n = store.add(store_dir, kind="check", target={"type": "item", "name": "x"},
                  checked="c", result="r", provenance={"sha": 123})
    for argv in (("list",), ("show", n.id)):
        assert run(*argv, store_dir=store_dir) == 0, argv
        assert "unverifiable" in capsys.readouterr().out, argv
    tty()
    for argv in (("list",), ("show", n.id)):
        assert run(*argv, store_dir=store_dir) == 0, argv
        assert "unverifiable" in capsys.readouterr().out, argv


def test_show_on_a_tty_prints_the_whole_id_and_every_ref(repo, tmp_path, capsys, tty):
    store_dir = tmp_path / "store"
    tty()
    tid = _add(store_dir, capsys, "task", "--target", "item:x", "--ref", "item:upload service",
               "--ref", "item:runner", "--body", "the whole body")
    run("show", tid, store_dir=store_dir)
    lines = _screen(capsys.readouterr().out)
    assert lines[0].startswith(tid[-10:]) and " ref" not in lines[0], lines
    assert any(tid in ln and "by " in ln for ln in lines), lines
    assert any(ln.strip() == "refs  item:upload service, item:runner" for ln in lines), lines
    assert any("the whole body" in ln for ln in lines), lines


def _sgr(hex_colour):
    """The truecolor escape that opens `hex_colour`, as rich writes it."""
    return "\x1b[38;2;" + ";".join(str(int(hex_colour[i:i + 2], 16)) for i in (1, 3, 5)) + "m"


def test_text_views_on_a_tty_are_the_pipe_text_in_colour(tmp_path, capsys, tty):
    """summary, schema, tags and the arc views print the same words at a
    terminal as through a pipe, coloured as a `list` row is: a kind in its
    colour, an overdue phrase red, an id and a count muted. Each view names
    a colour it must carry, so a view that stops painting fails here. The
    arc holds a `project` row, which has no name: reconcile skips it, and
    painting it would join a str to None."""
    from symbion import term
    aid = _seeded_reconcile_arc(tmp_path)
    run("add", "task", "--target", "project", "--arc", aid, store_dir=tmp_path)
    run("add", "bug", "--target", "item:upload", "--due", "2000-01-01", "--tag", "ci",
        "--body", "fails on **CI**", store_dir=tmp_path)
    capsys.readouterr()
    views = {("summary",): term.BAD, ("schema",): term.KIND["bug"], ("tags",): term.META,
             ("arc", "list"): term.TEXT, ("arc", "todo", aid): term.KIND["task"],
             ("arc", "reconcile", aid): term.BAD}
    piped = {}
    for argv in views:
        assert run(*argv, store_dir=tmp_path) == 0, argv
        piped[argv] = capsys.readouterr().out
    assert "STALE    thing:stale1" in piped["arc", "reconcile", aid], piped
    tty()
    for argv, colour in views.items():
        assert run(*argv, store_dir=tmp_path) == 0, argv
        shown = capsys.readouterr().out
        assert _ANSI.sub("", shown) == piped[argv], argv
        assert _sgr(colour) in shown, (argv, shown)


def test_a_kinds_picked_colour_paints_its_rows_and_its_schema_line(tmp_path, capsys, tty):
    """A store's own kinds all printed in one lavender, so several of them read
    alike (2026-10-06). `color` names a palette colour per kind."""
    from symbion import kinds as K
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text(
        '[kinds]\nanomaly = { status = true, color = "flamingo" }\nnote = {}\n')
    run("add", "anomaly", "--target", "item:x", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    tty()
    for argv in (("list",), ("schema",), ("summary",)):
        assert run(*argv, store_dir=tmp_path) == 0, argv
        assert _sgr(K.COLORS["flamingo"]) in capsys.readouterr().out, argv


def _parsers(p):
    """A parser and every verb's under it, `arc seed` included."""
    yield p
    if p._subparsers is not None:
        for sub in p._subparsers._group_actions[0].choices.values():
            yield from _parsers(sub)


def _words(text):
    """Help text without its layout. argparse's wrap breaks a line after a
    hyphen (`near-` / `synonyms`); rich's keeps the word whole."""
    return " ".join(re.sub(r"(?<=\w-)\s+", "", _ANSI.sub("", text)).split())


def test_help_on_a_tty_is_the_pipe_text_in_colour(tmp_path, tty):
    """--help at a terminal prints through rich_argparse: argparse's words
    in argparse's order, in the palette, wrapped by rich. Its markup is off,
    or serve's `'symbion[gui]'` loses its brackets; its section names keep
    argparse's spelling, not `Options:`. A usage error's usage line is
    coloured too."""
    from symbion import term
    p = cli._build_parser(frozenset({"item"}), frozenset(), frozenset(), tmp_path,
                          K.DEFAULT_KINDS)
    parsers = list(_parsers(p))
    piped = [(q.format_help(), q.format_usage()) for q in parsers]
    assert "'symbion[gui]'" in piped[0][0] and "\x1b" not in piped[0][0]
    tty()
    for q, (help_, usage) in zip(parsers, piped):
        shown = q.format_help()
        assert _words(shown) == _words(help_), q.prog
        assert _sgr(term.HUE["cyan"]) in shown, q.prog          # every verb has -h
        assert _words(q.format_usage()) == _words(usage), q.prog
    assert len(parsers) > 15, [q.prog for q in parsers]


def test_a_tty_body_renders_in_the_palette(repo, tmp_path, capsys, tty):
    """A full body's prose is the body colour, as under a `list` row, and
    code is the code colour: rich's defaults drew inline code bold cyan on a
    black box and a fenced block on monokai's olive one."""
    from symbion import term
    store_dir = tmp_path / "store"
    run("add", "note", "--target", "project", "--body",
        "run `make test` first\n\n```\nmake test\n```", store_dir=store_dir)
    capsys.readouterr()
    tty()
    run("list", "--full", store_dir=store_dir)
    raw = capsys.readouterr().out
    assert _sgr(term.BODY) in raw and _sgr(term.HUE["cyan"]) in raw, raw
    assert "48;" not in raw and ";40m" not in raw, "no background box: " + raw


@pytest.mark.parametrize("ago, want", [
    ({"seconds": 5}, "now"), ({"minutes": 40}, "40m"), ({"hours": 8}, "8h"),
    ({"days": 3}, "3d"), ({"days": 20}, "2w"), ({"days": 100}, "3mo"), ({"days": 800}, "2y")])
def test_a_tty_age_is_one_short_unit(ago, want):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    assert summ.age((now - timedelta(**ago)).isoformat(), now) == want


def test_list_resolves_a_commit_target_to_its_subject_line(repo, tmp_path, capsys):
    """`list` shows a subject line for a commit target, degrading
    to the bare sha only when the subject can't be resolved."""
    store_dir = tmp_path / "store"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    run("add", "--kind", "note", "--type", "commit", "--name", "HEAD",
        "--body", "x", store_dir=store_dir)
    capsys.readouterr()
    run("list", "--type", "commit", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "c0" in out, "the commit's subject line ('c0') must render"
    assert head not in out, "must not fall back to the bare sha when the subject resolves"


def test_list_degrades_to_the_bare_sha_when_the_subject_is_unresolvable(repo, tmp_path, capsys):
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    bogus = "0" * 40
    store.add(store_dir, kind="note", target={"type": "commit", "name": bogus})
    run("list", "--type", "commit", store_dir=store_dir)
    assert bogus in capsys.readouterr().out


def test_main_reports_a_clean_error_when_cwd_is_not_a_git_repo(tmp_path, monkeypatch, capsys):
    """config.project_root()'s `git worktree list --porcelain, check=True`
    raises CalledProcessError outside a repo. That must become an actionable
    message and exit 1, not a traceback -- the same requirement as
    CatalogError/AmbiguousName."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["summary"]) == 1
    err = capsys.readouterr().err
    assert "not a git repository" in err
    assert "Traceback" not in err


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot remove a process's cwd")
@pytest.mark.parametrize("argv", [["summary"], ["completion", "bash"]])
def test_a_cwd_removed_under_the_shell_is_named_not_a_traceback(argv, tmp_path, monkeypatch,
                                                                capsys):
    """A shell sat in a directory that was then removed and made again: git
    exits 128 there, and os.getcwd() raises, so the not-a-repo message and
    each verb that reads the cwd printed a FileNotFoundError traceback."""
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "no longer exists" in err and "`cd .`" in err


@pytest.mark.parametrize("argv", [["schema"], ["list", "--json"]])
def test_a_reader_that_closes_the_pipe_early_gets_no_python_error(argv, tmp_path):
    """`symbion list | head -1`. A short output fails at the interpreter's
    last flush ("Exception ignored", exit 120); one past stdout's buffer
    fails inside print (a traceback, exit 1). Either way the reader asked
    for less, so stderr stays empty and the exit is SIGPIPE's 141, as for
    any Unix tool. A real process: the last flush happens after main()."""
    run("add", "note", "--target", "project", "--body", "x" * 200_000, store_dir=tmp_path)
    r, w = os.pipe()
    os.close(r)
    p = subprocess.run([str(Path(sysconfig.get_path("scripts")) / "symbion"), "--dir", str(tmp_path),
                        *argv], stdout=w, stderr=subprocess.PIPE, text=True)
    os.close(w)
    assert (p.returncode, p.stderr) == (141, "")


def test_a_pipe_in_the_windows_code_page_carries_utf8_both_ways(tmp_path):
    """On Windows a pipe's encoding is cp1252: `list` exited 1 on a row holding
    `→`, and a body piped to --body-file - was stored garbled. PYTHONIOENCODING
    gives any platform that pipe."""
    sym = str(Path(sysconfig.get_path("scripts")) / "symbion")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    store.ensure_store(tmp_path)
    add = subprocess.run([sym, "--dir", str(tmp_path), "add", "note", "--target", "project",
                          "--body-file", "-"], input="arrow →\n".encode(), env=env,
                         capture_output=True)
    assert add.returncode == 0, add.stderr
    assert store.load(tmp_path)[0].body.strip() == "arrow →"
    ls = subprocess.run([sym, "--dir", str(tmp_path), "list"], env=env, capture_output=True)
    assert ls.returncode == 0 and "arrow →" in ls.stdout.decode(), ls.stderr


@pytest.mark.parametrize("argv", [["--help"], ["init", "--help"], ["add", "-h"]])
def test_help_answers_outside_a_git_repo(argv, tmp_path, monkeypatch, capsys):
    """Help needs no store, but argparse reaches `-h` only after the store
    resolves, so every `--help` outside a repo exited 1 with no usage --
    including `init --help`, which is what a newcomer reads from the parent
    directory BEFORE creating the store (measured 2026-09-28)."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: symbion")
    assert "not a git repository" not in out


def test_a_literal_dash_h_outside_a_repo_still_reports_no_repo(tmp_path, monkeypatch,
                                                               capsys):
    """The fallback above stands the cwd in as a named store, and `-h` after
    `--` is a positional value, not a help request: argparse then returns
    normally and the command would run against a phantom store in the cwd.
    Deleting the post-parse re-raise makes this rename succeed."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["rename", "--type", "item", "--", "-h", "zzz"]) == 1
    assert "not a git repository" in capsys.readouterr().err
    assert not (tmp_path / "notes.jsonl").exists()


def test_add_commit_target_stores_full_sha_not_a_symbolic_ref(repo, tmp_path, capsys):
    """CRITICAL: `add --type commit --name HEAD` must store the peeled full
    object id, or the note can never match `context --commit HEAD`'s
    canonicalized query as HEAD moves (symbolic refs are frozen at
    write time for exactly this reason)."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    store_dir = tmp_path / "store"
    assert run("add", "--kind", "note", "--type", "commit", "--name", "HEAD",
               "--body", "x", store_dir=store_dir) == 0
    capsys.readouterr()
    note = store.load(store_dir)[0]
    assert note.target.name == head, \
        f"stored {note.target.name!r} unexpanded instead of the full sha"


def test_add_commit_target_expands_a_short_sha(repo, tmp_path, capsys):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "commit", "--name", head[:7],
        store_dir=store_dir)
    capsys.readouterr()
    note = store.load(store_dir)[0]
    assert note.target.name == head


def test_context_commit_HEAD_finds_a_note_added_against_HEAD(repo, tmp_path, capsys):
    """The write side (add --name HEAD) and the read side (context --commit
    HEAD) must land on the same stored sha. A decoy note on a different
    commit proves the query actually filters rather than returning
    everything."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "commit", "--name", "HEAD",
        "--body", "reviewed", store_dir=store_dir)
    capsys.readouterr()
    (repo / "g").write_text("y")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c1")
    run("add", "--kind", "note", "--type", "commit", "--name", "HEAD",
        "--body", "decoy", store_dir=store_dir)
    capsys.readouterr()
    run("context", "--commit", "HEAD~1", "--json", store_dir=store_dir)
    rows = json.loads(capsys.readouterr().out)["notes"]
    assert [r["body"] for r in rows] == ["reviewed"], \
        "must return only the queried commit's note, not every commit note"


def test_list_name_resolves_the_same_abbreviation_used_on_write(tmp_path, capsys):
    """`add --type file --name parser` stores the catalog-resolved
    `src/parser.py`; `list --type file --name parser` must resolve the same
    abbreviation on read, or the query silently returns nothing."""
    (tmp_path / "symbion.toml").write_text(
        '[catalogs]\nfile = "echo src/parser.py; echo src/cli.py"\n')
    run("add", "--kind", "note", "--type", "file", "--name", "parser",
        "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--type", "file", "--name", "parser", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1, "abbreviation used on write must also match on read"
    assert rows[0]["target"]["name"] == "src/parser.py"


def test_list_name_reads_the_type_prefix_its_own_miss_hint_prints(tmp_path, capsys):
    """The miss hint prints `item:NAME`, and pasting that back read a silent
    `0 of N` with no hint, since no target NAME carries the prefix (an adopter
    hit it twice, 2026-09-28). It is an input now, as `add --target` and
    `context --target` take it, and the split is announced."""
    run("add", "--kind", "note", "--type", "item", "--name", "dns audit",
        "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--name", "item:dns audit", "--json", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert len(json.loads(out)) == 1, "the hint's own form must match"
    assert "read 'item:dns audit' as --type item --name 'dns audit'" in err


def test_list_takes_target_as_add_and_context_do(tmp_path, capsys):
    """An agent typed `list --target file:src/x.py` by analogy with `add`
    and `context`, and got argparse's usage error (2026-10-02)."""
    run("add", "note", "--target", "item:dns audit", "--body", "x", store_dir=tmp_path)
    run("add", "note", "--target", "item:other", "--body", "y", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--body", "z", store_dir=tmp_path)
    capsys.readouterr()
    for target, bodies in (("item:dns audit", ["x"]), ("project", ["z"])):
        assert run("list", "--target", target, "--json", store_dir=tmp_path) == 0
        assert [r["body"] for r in json.loads(capsys.readouterr().out)] == bodies
    assert run("list", "--target", "item:x", "--name", "x", store_dir=tmp_path) == 2
    assert "--target or --type/--name, not both" in capsys.readouterr().err


def test_list_name_prefers_a_target_literally_named_with_a_colon(tmp_path, capsys):
    """The split is a fallback, not a rewrite: a row whose target name really
    is `item:x` must still win, and --type given means --name was literal."""
    run("add", "--kind", "note", "--type", "item", "--name", "item:x",
        "--body", "literal", store_dir=tmp_path)
    run("add", "--kind", "note", "--type", "item", "--name", "x",
        "--body", "split", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--name", "item:x", "--json", store_dir=tmp_path)
    out, err = capsys.readouterr()
    rows = json.loads(out)
    assert [r["body"] for r in rows] == ["literal"], rows
    assert "read 'item:x'" not in err


def test_list_name_leaves_an_unknown_type_prefix_alone(tmp_path, capsys):
    """An unknown TYPE can hold no rows, so splitting on it would turn a
    miss into a different miss and blame the wrong half of the name."""
    run("add", "--kind", "note", "--type", "item", "--name", "nope",
        "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--name", "zz:nope", "--json", store_dir=tmp_path)
    out, err = capsys.readouterr()
    assert json.loads(out) == []
    assert "read 'zz:nope'" not in err


def test_context_target_resolves_the_same_abbreviation_used_on_write(tmp_path, capsys):
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "echo src/parser.py"\n')
    run("add", "--kind", "note", "--type", "file", "--name", "parser",
        "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    run("context", "--target", "file:parser", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)["notes"]
    assert len(rows) == 1
    assert rows[0]["target"]["name"] == "src/parser.py"


def test_a_read_finds_a_deleted_file_by_its_stored_name(tmp_path, capsys):
    """A row on `index.html` became unreadable by its own target once the
    file was deleted: the live catalog no longer held the exact name, and two
    deeper `index.html` paths made it ambiguous (2026-10-07). With one deeper
    path, the read resolved there and read 0. A name the store holds verbatim,
    as a target or a ref, is that object on a read, and where the catalog
    would send it is said: a write of that name lands there. A write still
    runs the catalog: a new row on a deleted path is the misfile it stops."""
    _declare(tmp_path, '[catalogs]\nfile = "echo index.html; echo a/index.html; echo x.css"\n')
    run("add", "note", "--target", "file:index.html", "--body", "target", store_dir=tmp_path)
    run("add", "note", "--target", "item:x", "--ref", "file:index.html", "--body", "ref",
        store_dir=tmp_path)
    run("add", "note", "--target", "item:y", "--ref", "file:x.css", "--body", "css",
        store_dir=tmp_path)
    capsys.readouterr()

    def context(name):
        assert run("context", "--target", f"file:{name}", "--json", store_dir=tmp_path) == 0
        return sorted(r["body"] for r in json.loads(capsys.readouterr().out)["notes"])

    said = "note: 'index.html' is a stored file name, read as typed; the catalog resolves it to "
    one = '[catalogs]\nfile = "echo a/index.html; echo a/x.css"\n'
    for toml, note in (
            ('[catalogs]\nfile = "echo a/index.html; echo b/index.html; echo a/x.css; echo b/x.css"\n', ""),
            (one, said + "'a/index.html'\n"),
            (one + '[resolvers]\nfile = "cat >/dev/null; echo z/index.html"\n', said + "'z/index.html'\n")):
        _declare(tmp_path, toml)
        assert run("list", "--target", "file:index.html", "--json", store_dir=tmp_path) == 0, toml
        out, err = capsys.readouterr()
        assert [r["body"] for r in json.loads(out)] == ["target"], toml
        assert err == note, err
        assert context("index.html") == ["ref", "target"], toml
        assert context("x.css") == ["css"], "a name held only as a ref"
    _declare(tmp_path, '[catalogs]\nfile = "echo a/index.html; echo b/index.html"\n')
    assert run("add", "note", "--target", "file:index.html", "--body", "z", store_dir=tmp_path) != 0
    assert "matches 2" in capsys.readouterr().err


def test_main_reports_a_clean_error_on_broken_toml(tmp_path, capsys):
    (tmp_path / "symbion.toml").write_text("= invalid\n")
    assert run("summary", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "could not parse" in err
    assert "Traceback" not in err


def test_add_status_on_a_stateless_kind_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    assert run("add", "--kind", "decision", "--type", "project",
               "--status", "resolved", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_cli_resolve_on_a_stateless_kind_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    """`resolve`'s help text says "mark a bug/task resolved", but
    it calls `supersede(status="resolved")` unconditionally -- before the
    guard was extended to `supersede`, `resolve <id-of-a-note>` silently
    succeeded, contradicting both the help text and add's own refusal of
    `--status` on stateless kinds."""
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("resolve", nid, store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_summary_json_on_an_absent_store_is_a_valid_empty_object(repo, tmp_path, capsys):
    """A programmatic `--json` consumer needs valid JSON against an absent
    store, not an empty string. Measured before the fix: rc=0, stdout=''.
    (The text path names the missing store since 2026-09-20; see the test
    below.)"""
    store_dir = tmp_path / "store"
    assert not store_dir.exists()
    assert run("summary", "--json", store_dir=store_dir) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == summ.empty_summary()
    # The same keys as a store's: `named_store` was a KeyError on `store: null`
    # (2026-10-09 review).
    store.ensure_store(store_dir)
    assert run("summary", "--json", store_dir=store_dir) == 0
    assert set(json.loads(capsys.readouterr().out)) == set(data)


# ---- THE BLOCKER: cli.main must resolve HEAD/dirty against the worktree the
# caller is actually standing in (cfg.work_root), never the main worktree used
# only to derive the store path (cfg.project_root). Every other linked-
# worktree test (test_gitref.py) builds `Config` by hand and never exercises
# `cli.main` -- so `cli.main` dropping `work_root=wroot` (which makes
# `config.load` silently fall back to `work_root=project_root`) sailed through
# the whole suite. This reuses test_gitref.py's `linked`-fixture shape,
# end to end through the CLI, so that specific line is falsifiable. Verified
# 2026-09-04: with `work_root=wroot` deleted from cli.py, this test fails --
# `note.target.name` comes back as the MAIN checkout's HEAD and `dirty` reads
# False against a linked tree that is actually dirty.
def test_cli_add_commit_resolves_against_the_linked_worktree_not_main(tmp_path, monkeypatch):
    r = tmp_path / "p"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c0")

    wt = tmp_path / "wt1"
    _git(r, "worktree", "add", "-q", "-b", "topic", str(wt), "HEAD")
    (wt / "topic_only").write_text("z")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "topic-commit")

    main_head = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()
    linked_head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
    assert linked_head != main_head, "fixture must diverge, or this proves nothing"

    monkeypatch.chdir(wt)
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    assert cli.main(["--dir", str(store_dir), "add", "--kind", "check",
                     "--type", "commit", "--name", "HEAD"]) == 0

    note = store.heads(store.load(store_dir))[0]
    assert note.target.name == linked_head, \
        f"stored {note.target.name!r} -- resolved against the main checkout, not the linked one"
    assert note.provenance["sha"] == linked_head

    # Dirty ONLY the linked tree; the main checkout must stay clean, so this
    # assertion fails if `dirty` is read from the wrong tree.
    (wt / "scratch").write_text("uncommitted")
    assert cli.main(["--dir", str(store_dir), "add", "--kind", "check",
                     "--type", "commit", "--name", "HEAD"]) == 0
    main_status = subprocess.run(["git", "-C", str(r), "status", "--porcelain"],
                                 capture_output=True, text=True, check=True).stdout
    assert main_status == "", \
        "main checkout must stay clean -- otherwise this isn't testing the linked tree"

    notes = store.heads(store.load(store_dir))
    second = [n for n in notes if n.id != note.id][0]
    assert second.provenance["dirty"] is True


def test_arc_create_prints_the_bare_id(tmp_path, capsys):
    """`add`/`resolve`/`supersede` print a bare id an agent can capture with
    `$(...)`; `arc create` printed a sentence around its id, so the one
    write that mints an arc was the one an agent had to parse."""
    run("arc", "create", "--name", "Adoption", "--scope", "item", store_dir=tmp_path)
    assert capsys.readouterr().out.strip() == store.load_arcs(tmp_path)[0].id


def test_arc_list_carries_the_description(tmp_path, capsys):
    """--desc was write-only: stored, never rendered by any command."""
    run("arc", "create", "--name", "x", "--scope", "item", "--desc", "the why",
        store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "list", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)[0]["description"] == "the why"
    run("arc", "list", store_dir=tmp_path)
    assert "the why" in capsys.readouterr().out


def test_arc_todo_json_carries_the_body(tmp_path, capsys):
    """The body IS the ticket text; without it an agent working the list
    needs one extra `context --target` call per item."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("add", "--kind", "task", "--type", "item", "--name", "thing1",
        "--arc-id", aid, "--body", "do the thing", store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)[0]["body"] == "do the thing"


def test_a_check_with_no_stamp_reads_unstamped_with_its_age(repo, tmp_path, capsys, tty):
    """Rows migrated from a store that predates stamping read `unverifiable
    (no provenance)`, as a dirty or squashed stamp does: an alarm on every
    such row, none of them news. JSON names the state; text gives the age."""
    from datetime import datetime, timedelta
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    five = (datetime.now().astimezone() - timedelta(days=5, hours=1)).isoformat(timespec="seconds")
    store.add(store_dir, kind="check", target={"type": "item", "name": "x"},
              checked="c", result="r", created_at=five)
    run("list", "--kind", "check", "--json", store_dir=store_dir)
    assert [r["state"] for r in json.loads(capsys.readouterr().out)] == ["unstamped"]
    run("list", "--kind", "check", store_dir=store_dir)
    assert "state=unstamped (5d ago)" in capsys.readouterr().out
    tty()
    run("list", "--kind", "check", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "unstamped (5d ago)" in out and "unverifiable" not in out


def test_list_text_names_why_a_check_is_unverifiable(repo, tmp_path, capsys):
    """A dirty check renders `unverifiable (dirty tree)`. Both
    directions: the clean check on the same HEAD must still read `current`,
    and `--json` keeps the bare state so the field contract does not move."""
    store_dir = tmp_path / "store"
    run("add", "--kind", "check", "--type", "project", "--checked", "x",
        "--result", "y", store_dir=store_dir)
    (repo / "scratch").write_text("uncommitted")
    run("add", "--kind", "check", "--type", "project", "--checked", "x",
        "--result", "y", store_dir=store_dir)
    capsys.readouterr()
    run("list", "--kind", "check", store_dir=store_dir)
    out = capsys.readouterr().out
    assert "state=unverifiable (dirty tree)" in out
    assert "state=current distance=0" in out
    run("list", "--kind", "check", "--json", store_dir=store_dir)
    states = {r["state"] for r in json.loads(capsys.readouterr().out)}
    assert states == {"unverifiable", "current"}


def test_a_dirty_stamp_names_what_was_dirty(repo, tmp_path, capsys):
    """26% of 536 stamped rows across adopter stores are dirty, and three of
    nine trees were dirty at one look, each with an uncommitted CLAUDE.md,
    which no verdict depends on (measured 2026-09-27). A bare boolean cannot
    say what made a stamp dirty, so whether such files should count cannot
    be measured. The stamp names the paths; a clean one carries none."""
    store_dir = tmp_path / "store"
    run("add", "check", "--target", "project", "--checked", "x", "--result", "y",
        store_dir=store_dir)
    (repo / "scratch").write_text("uncommitted")
    (repo / "CLAUDE.md").write_text("x")
    run("add", "check", "--target", "project", "--checked", "x", "--result", "y",
        store_dir=store_dir)
    clean, dirty = [n.provenance for n in store.load(store_dir)]
    assert set(clean) == {"sha", "dirty"} and clean["dirty"] is False
    assert dirty["dirty"] is True and dirty["dirty_count"] == 2
    assert sorted(dirty["dirty_paths"]) == ["CLAUDE.md", "scratch"], dirty


def test_a_dirty_write_says_its_row_reads_unverifiable_for_good(repo, tmp_path, capsys):
    """A check written on a dirty tree reads `unverifiable (dirty tree)`
    from then on, and the write said nothing (2026-10-14). The write names
    the paths; a clean one stays quiet. A pre-registration's resolve is
    stamped anew, so it says so too, without --external, which resolve does
    not take; a resolve of a resolved row restamps nothing."""
    store_dir = tmp_path / "store"
    run("add", "check", "--target", "project", "--checked", "x", "--result", "y",
        store_dir=store_dir)
    assert "unverifiable" not in capsys.readouterr().err
    pid = _add(store_dir, capsys, "prediction", "--target", "item:p", "--checked", "the sweep",
               "--body", "x rises")
    (repo / "scratch").write_text("uncommitted")
    run("add", "check", "--target", "project", "--checked", "x", "--result", "y",
        store_dir=store_dir)
    err = capsys.readouterr().err
    assert ("note: the tree has 1 uncommitted path (scratch), so this check entry reads "
            "`unverifiable (dirty tree)` for good") in err, err
    assert "--external" in err
    run("resolve", pid, "--result", "rose", store_dir=store_dir)
    err = capsys.readouterr().err
    assert "so this prediction entry reads `unverifiable (dirty tree)`" in err, err
    assert "--external" not in err
    run("resolve", pid, "--body", "and more", store_dir=store_dir)
    assert "note: the tree has" not in capsys.readouterr().err



def test_a_dirty_stamp_keeps_each_path_as_itself(repo, tmp_path):
    """git quotes a path with a space or a non-ASCII byte in its line form,
    and the stamp kept `"caf\\303\\251.txt"`, quotes and all (2026-10-09
    review). A rename is stamped by its new name."""
    store_dir = tmp_path / "store"
    for name in ("café.txt", "has space.txt", "old.txt"):
        (repo / name).write_text("x")
    _git(repo, "add", "old.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "old")
    _git(repo, "mv", "old.txt", "new name.txt")
    run("add", "check", "--target", "project", "--checked", "x", "--result", "y",
        store_dir=store_dir)
    stamp = store.load(store_dir)[0].provenance
    assert sorted(stamp["dirty_paths"]) == ["café.txt", "has space.txt", "new name.txt"], stamp


def test_catalog_choice_error_names_where_a_catalog_is_declared(repo, tmp_path, capsys):
    """A store with no catalogs rejects `--type file`; argparse's bare
    'invalid choice' told nobody that [catalogs] in symbion.toml is the fix.
    The legal value on the same parser must still pass."""
    store_dir = tmp_path / "store"
    assert run("add", "--kind", "note", "--type", "file", "--name", "x",
               "--body", "b", store_dir=store_dir) == 2
    err = capsys.readouterr().err
    assert str(store_dir / "symbion.toml") in err and "[catalogs]" in err
    assert run("add", "--kind", "note", "--type", "item", "--name", "x",
               "--body", "b", store_dir=store_dir) == 0


def test_seed_dry_run_needs_no_arc(tmp_path, capsys):
    """A catalog's first dry-run happens before any arc exists for it;
    requiring the id forced a write to the real store just to look. The real
    seed on the same unknown id must still refuse."""
    assert run("arc", "seed", "nope", "--scope", "item", "--name", "a",
               "--name", "b", "--dry-run", store_dir=tmp_path) == 0
    assert "would seed 2" in capsys.readouterr().out
    assert run("arc", "seed", "nope", "--scope", "item", "--name", "a",
               store_dir=tmp_path) == 1


def test_seed_dry_run_takes_no_id_at_all(tmp_path, capsys):
    """The id above was still required and still documented, so a reader
    could not tell it was ignored: an adopter created and archived a probe
    arc just to count its catalog.
    Without --dry-run the id is still required, and nothing is written."""
    assert run("arc", "seed", "--scope", "item", "--name", "a", "--dry-run",
               store_dir=tmp_path) == 0
    assert "would seed 1" in capsys.readouterr().out
    assert run("arc", "seed", "--scope", "item", "--name", "a", store_dir=tmp_path) == 1
    assert "arc id" in capsys.readouterr().err
    assert store.load(tmp_path) == [] and store.load_arcs(tmp_path) == []


def test_seed_dry_run_prints_one_name_per_line(tmp_path, capsys):
    """400 names on one line is a 30KB line the docs tell you to read."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    run("arc", "seed", aid, "--scope", "item", "--name", "alpha",
        "--name", "beta", "--dry-run", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "would seed 2:"
    assert lines[1:] == ["alpha", "beta"]


def test_list_shows_a_short_sha_and_a_bounded_subject(repo, tmp_path, capsys):
    """Paragraph-long subjects made every commit-target row unreadable, and
    the sha the row is about was nowhere in the text output."""
    long = "s" * 200
    (repo / "g").write_text("y")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", long)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "commit", "--name", "HEAD",
        "--body", "x", store_dir=store_dir)
    capsys.readouterr()
    run("list", "--type", "commit", store_dir=store_dir)
    line = capsys.readouterr().out.splitlines()[1]      # line 0 is the count header
    assert head[:7] in line and head not in line
    assert "s" * 60 in line and "s" * 61 not in line


def test_arc_todo_json_is_the_same_shape_as_list_json(tmp_path, capsys):
    """One CLI, one note shape: a consumer written against `list --json`
    must not KeyError on `arc todo --json`."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    run("add", "--kind", "task", "--type", "item", "--name", "t",
        "--arc-id", aid, "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    todo = json.loads(capsys.readouterr().out)
    run("list", "--json", store_dir=tmp_path)
    listed = json.loads(capsys.readouterr().out)
    assert todo == listed
    assert todo[0]["target"] == {"type": "item", "name": "t"}
    assert "target_type" not in todo[0]


def test_arc_todo_keeps_insertion_order(tmp_path, capsys):
    """Numbered adoption steps printed alphabetically; the order a checklist
    was written in is the order it is meant to be done in."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    for name in ("step two", "step one"):
        run("add", "--kind", "task", "--type", "item", "--name", name,
            "--arc-id", aid, store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    names = [r["target"]["name"] for r in json.loads(capsys.readouterr().out)]
    assert names == ["step two", "step one"]


def test_add_from_json_writes_one_note_per_line_and_prints_each_id(tmp_path, capsys):
    """Bootstrap writes were one `symbion add` per row (dozens per bootstrap,
    each a process with a shell-quoted markdown body). One command, one row
    per line, the `list --json` shape in."""
    src = tmp_path / "entries.jsonl"
    src.write_text(
        '{"kind": "decision", "target": {"type": "project"}, "body": "b1", "tags": ["x"]}\n'
        '\n'
        '{"kind": "task", "target": {"type": "item", "name": "t"}, '
        '"refs": [{"type": "item", "name": "other"}], "author": "someone"}\n')
    assert run("add", "--from-json", str(src), store_dir=tmp_path) == 0
    ids = capsys.readouterr().out.split()
    assert len(ids) == 2 and len(set(ids)) == 2
    run("list", "--json", store_dir=tmp_path)
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert set(rows) == set(ids)
    fu = next(r for r in rows.values() if r["kind"] == "task")
    assert fu["target"] == {"type": "item", "name": "t"}
    assert fu["status"] == "open", "status is defaulted per kind, as the flag path does"
    assert fu["refs"] == [{"type": "item", "name": "other"}]
    assert fu["author"] == "someone"
    dec = next(r for r in rows.values() if r["kind"] == "decision")
    assert dec["tags"] == ["x"] and dec["body"] == "b1"


def test_add_from_json_reads_stdin_and_a_bad_row_writes_nothing(tmp_path, capsys, monkeypatch):
    """Validate every row before appending any: a bad row 30 of 45 must not
    leave 29 rows behind to supersede by hand. Both directions: the good row
    on line 1 is NOT written, and the error names line 2."""
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": {"type": "project"}}\n'
        '{"kind": "note", "target": {"type": "project"}, "id": "minted-by-hand"}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "line 2" in err and "id" in err
    assert store.load(tmp_path) == []


def test_add_from_json_names_every_failing_line_in_order(tmp_path, capsys, monkeypatch):
    """Keys were checked over every line before kinds, so a bad kind on
    line 2 and a bad key on line 3 named line 3, and line 2 only on the
    re-run (2026-10-09 review)."""
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": {"type": "project"}}\n'
        '{"kind": "nope", "target": {"type": "project"}}\n'
        '{"kind": "note", "target": {"type": "project"}, "title": "x"}\n'
        '[1]\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err.splitlines()
    assert [re.match(r"add: --from-json line (\d)", e)[1] for e in err] == ["2", "3", "4"], err
    assert store.load(tmp_path) == []


@pytest.mark.parametrize("piped", ["", " \n\n"])
def test_add_from_json_that_reads_no_rows_is_refused(tmp_path, capsys, monkeypatch, piped):
    """`jq … | symbion add --from-json -` with a jq that failed (no pipefail)
    read nothing and exited 0, a bootstrap that wrote nothing reading as one
    that worked. The twin of the --body-file guard."""
    monkeypatch.setattr("sys.stdin", io.StringIO(piped))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "--from-json -: read no entries, so nothing was written" in err
    assert store.load(tmp_path) == []


def test_add_from_json_reads_a_line_separator_inside_a_string(tmp_path, capsys, monkeypatch):
    """jq and json.dumps(ensure_ascii=False) leave U+2028, U+2029 and U+0085
    raw, and the line split refused the row as two halves."""
    body = "a b c\x85d"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"kind": "note", "target": {"type": "project"}, "body": body}, ensure_ascii=False)))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0, capsys.readouterr().err
    assert [n.body for n in store.load(tmp_path)] == [body]


@pytest.mark.parametrize("key,bad,said", [
    ("kind", [1], "kind must be a string"), ("body", 5, "body must be a string"),
    ("status", 5, "status must be a string"), ("status", "done", "status is open or resolved"),
    ("checked", 5, "checked must be a string"), ("result", 5, "result must be a string"),
    ("arc_id", 5, "arc_id must be a string"), ("due", 5, "due must be a string"),
    ("author", 5, "author must be a string"), ("tags", "pri", "tags must be a list"),
    ("tags", [1], "tags must be strings"), ("refs", "item:y", "refs must be a list"),
    ("refs", [5], "each ref is {type, name} or TYPE:NAME"),
    ("target", ["item", "x"], "target is {type, name} or TYPE:NAME")])
def test_from_json_refuses_a_key_of_the_wrong_type_before_it_writes(
        tmp_path, capsys, monkeypatch, key, bad, said):
    """A key of the wrong type crashed into api.py (a ref spelled TYPE:NAME),
    stored garbage at exit 0 (`"tags": "pri"` as three one-letter tags, a
    number as a status), or wrote the row and then crashed (2026-10-06)."""
    row = {"kind": "check" if key in ("checked", "result") else "task",
           "target": {"type": "item", "name": "x"}, key: bad}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(row) + "\n"))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert f"line 1: {said}" in err and "Traceback" not in err, err
    assert store.load(tmp_path) == []


def test_measure_stores_numbers_as_numbers_and_shows_them(tmp_path, capsys):
    """Counts in `--result` prose cannot be compared across rows; as
    measurements they can, through `list --json | jq`. Every reader lists
    them by name, any case, so two rows' numbers line up (the user,
    2026-10-09)."""
    assert run("add", "note", "--target", "item:x", "--measure", "passed=412",
               "--measure", "seconds=3.2", "--measure", "host=a.example",
               "--measure", "big=1e3", "--measure", "Mb=5", store_dir=tmp_path) == 0
    nid = capsys.readouterr().out.strip()
    assert list(store.load(tmp_path)[0].measurements.items()) == [
        ("big", 1000.0), ("host", "a.example"), ("Mb", 5), ("passed", 412), ("seconds", 3.2)]
    assert run("show", nid, store_dir=tmp_path) == 0
    assert "    measured: big=1000.0, host=a.example, Mb=5, passed=412, seconds=3.2\n" in \
        capsys.readouterr().out
    with pytest.MonkeyPatch.context() as mp:                  # and at a terminal
        mp.setattr(sys.stdout, "isatty", lambda: True)
        assert run("show", nid, store_dir=tmp_path) == 0
    assert "measured: big=1000.0, host=a.example, Mb=5" in \
        _ANSI.sub("", capsys.readouterr().out)
    assert run("show", nid, "--json", store_dir=tmp_path) == 0
    assert list(json.loads(capsys.readouterr().out)[0]["measurements"]) == [
        "big", "host", "Mb", "passed", "seconds"]
    _commit(tmp_path, capsys)
    assert run("supersede", nid, "--add-tag", "t", store_dir=tmp_path) == 0
    assert store.heads(store.load(tmp_path))[0].measurements["passed"] == 412


def test_measure_says_when_a_number_will_not_print_back_as_typed(tmp_path, capsys):
    """`python=3.10` was stored as 3.1 and `code=0042` as 42, silently. A
    number stays the default, since
    text breaks the comparison (`"1.50" > 2` is true in jq): the note names
    the quoted form that keeps it as typed."""
    assert run("add", "note", "--target", "item:x", "--measure", "python=3.10",
               "--measure", "code=0042", "--measure", 'tag="3.10"', "--measure", "n=412",
               store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert store.load(tmp_path)[0].measurements == {
        "python": 3.1, "code": 42, "tag": "3.10", "n": 412}
    assert "--measure python=3.10 is stored as the number 3.1" in err, err
    assert """--measure 'python="3.10"'""" in err and "code=0042" in err, err
    assert "tag" not in err and "n=412" not in err, err


def test_measure_takes_spaces_around_its_name_and_value(tmp_path, capsys):
    """`--measure "n = 5"` stored the name `'n '`, and its note said to write
    `--measure 'n =" 5"'`."""
    assert run("add", "note", "--target", "item:x", "--measure", "n = 5",
               "--measure", 'label = " a "', store_dir=tmp_path) == 0
    assert store.load(tmp_path)[0].measurements == {"n": 5, "label": " a "}
    assert _sans_pending(capsys.readouterr().err) == ""


@pytest.mark.parametrize("flags,said", [
    (["n"], "KEY=VALUE"), (["=5"], "KEY=VALUE"), (["n="], "empty"), (['n=""'], "empty"),
    (["n=nan"], "finite"), (["n=inf"], "finite"), (["n=-Infinity"], "finite"),
    (["n=1", "n=2"], "twice"), (["n = 1", "n=2"], "twice")])
def test_measure_refuses_what_cannot_be_compared(tmp_path, capsys, flags, said):
    """`--measure n=$count` with count unset stored "" at exit 0, and NaN is
    a float that equals nothing, itself included."""
    args = [a for f in flags for a in ("--measure", f)]
    assert run("add", "note", "--target", "item:x", *args, store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert said in err and "Traceback" not in err, err
    assert "stored as the number" not in err, err     # no note before a refusal
    assert store.load(tmp_path) == []


def test_resolve_and_supersede_measure_merge_onto_the_inherited_ones(repo, tmp_path, capsys):
    """A prediction's numbers exist only once it is judged, and `resolve`
    took no `--measure`; a wrong number from `add` could not be corrected,
    so its row kept a figure the body had to define away (2026-10-08). A
    key given replaces that key; the others are inherited."""
    _declare(tmp_path, PREREG)
    run("add", "prediction", "--target", "item:p", "--checked", "run x",
        "--body", "x rises", store_dir=tmp_path)
    pid = capsys.readouterr().out.strip()
    assert run("resolve", pid, "--result", "x rose", "--measure", "x=7",
               store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.status, head.measurements) == ("resolved", {"x": 7})
    capsys.readouterr()
    run("add", "note", "--target", "item:n", "--measure", "a=1", "--measure", "b=8",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("supersede", nid, "--measure", "b=9", "--measure", "c=2",
               store_dir=tmp_path) == 0
    # --add-tag alone takes a tags-only path; a measure beside it must not drop there
    assert run("supersede", nid, "--add-tag", "t", "--measure", "d=4", store_dir=tmp_path) == 0
    head = next(n for n in store.heads(store.load(tmp_path)) if n.target.name == "n")
    assert head.measurements == {"a": 1, "b": 9, "c": 2, "d": 4}
    assert set(head.tags) == {"t"}


@pytest.mark.parametrize("verb", ["resolve", "supersede"])
def test_resolve_and_supersede_refuse_the_measures_add_refuses(tmp_path, capsys, verb):
    run("add", "task", "--target", "item:x", "--measure", "n=1", store_dir=tmp_path)
    tid = capsys.readouterr().out.strip()
    before = store.load(tmp_path)
    for bad, said in (("n=", "empty"), ("n=nan", "finite"), ("n", "KEY=VALUE")):
        assert run(verb, tid, "--measure", bad, store_dir=tmp_path) == 1
        err = capsys.readouterr().err
        assert said in err and "Traceback" not in err, err
    assert store.load(tmp_path) == before


@pytest.mark.parametrize("bad", ['{}', '[]', '{"n": true}', '{"n": [1]}', '{"n": null}',
                                 '{"n": NaN}', '{"": 1}', '{"n": ""}'])
def test_from_json_measurements_are_flat_finite_and_named(tmp_path, capsys, monkeypatch, bad):
    """json.loads reads NaN and Infinity, and True is an int to Python."""
    monkeypatch.setattr("sys.stdin", io.StringIO(
        f'{{"kind": "note", "target": "item:x", "measurements": {bad}}}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "line 1: measurement" in err and "Traceback" not in err, err
    assert store.load(tmp_path) == []
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": "item:x", "measurements": {"n": 3, "s": "x"}}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    assert store.load(tmp_path)[0].measurements == {"n": 3, "s": "x"}


def test_from_json_takes_type_name_as_every_flag_spells_it(tmp_path, capsys, monkeypatch):
    """`--target`, `--ref` and `summary --json` spell an object TYPE:NAME, so
    a row writer guesses it; `refs: ["commit:abc1234"]` was a traceback."""
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": "item:x", "refs": ["item:y", "project", '
        '{"type": "item", "name": "z"}]}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    n = store.load(tmp_path)[0]
    assert summ.ref_label(n.target) == "item:x"
    assert [summ.ref_label(r) for r in n.refs] == ["item:y", "project:", "item:z"]


def test_add_from_json_fed_list_json_says_what_it_reads(tmp_path, capsys, monkeypatch):
    """The help said 'in the `list --json` shape', and a reader took the line
    format from it. `list --json` prints one array, and its rows carry keys
    symbion mints. Fed either, the error names the difference."""
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--json", store_dir=tmp_path)
    listed = capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(listed))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    assert "line 1 is a JSON array" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(json.loads(listed)[0]) + "\n"))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "'created_at'" in err and "an entry takes only kind, target, body" in err, err
    assert len(store.load(tmp_path)) == 1


def test_add_from_json_names_the_line_of_a_bad_status(tmp_path, capsys):
    src = tmp_path / "entries.jsonl"
    src.write_text('{"kind": "decision", "target": {"type": "project"}, "status": "open"}\n')
    assert run("add", "--from-json", str(src), store_dir=tmp_path) == 1
    assert "line 1" in capsys.readouterr().err


def test_add_without_kind_and_type_or_from_json_is_a_usage_error(tmp_path, capsys):
    assert run("add", "--body", "b", store_dir=tmp_path) == 2
    assert "--kind" in capsys.readouterr().err
    assert run("add", "--from-json", "-", "--kind", "note", store_dir=tmp_path) == 2


def test_add_from_json_author_flag_is_the_default_for_rows_without_one(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": {"type": "project"}}\n'
        '{"kind": "note", "target": {"type": "project"}, "author": "row"}\n'))
    assert run("add", "--from-json", "-", "--author", "flag", store_dir=tmp_path) == 0
    capsys.readouterr()
    run("list", "--json", store_dir=tmp_path)
    assert sorted(r["author"] for r in json.loads(capsys.readouterr().out)) == ["flag", "row"]


def test_list_arc_shows_resolved_items_that_todo_hides(tmp_path, capsys):
    """`arc todo` is the open list; reading a finished campaign's tickets
    had no CLI path at all (grep the JSONL). The filter is the store's own
    arc_id column."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    run("add", "--kind", "task", "--type", "item", "--name", "t",
        "--arc-id", aid, store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("add", "--kind", "task", "--type", "item", "--name", "elsewhere", store_dir=tmp_path)
    run("resolve", nid, store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out) == []
    run("list", "--arc", aid, "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert [(r["target"]["name"], r["status"]) for r in rows] == [("t", "resolved")]


def test_list_arc_unknown_id_exits_1(tmp_path, capsys):
    run("add", "--kind", "note", "--type", "project", store_dir=tmp_path)
    assert run("list", "--arc", "nope", store_dir=tmp_path) == 1
    assert "nope" in capsys.readouterr().err


def test_add_ref_with_an_unknown_type_is_refused_on_both_paths(tmp_path, capsys, monkeypatch):
    """`--ref TYPE:NAME` never checked TYPE, so a typo minted a ref of a type
    that does not exist -- silently, since refs are only ever read back by
    `context --target` on that type. One check in the shared row builder."""
    assert run("add", "--kind", "note", "--type", "project", "--ref", "fil:x", store_dir=tmp_path) == 1
    assert "fil" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": {"type": "project"}, "refs": [{"type": "fil", "name": "x"}]}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "line 1" in err and "fil" in err
    assert store.load(tmp_path) == []
    assert run("add", "--kind", "note", "--type", "project", "--ref", "item:x", store_dir=tmp_path) == 0


@pytest.mark.parametrize("argv, stdin, says", [
    (["add", "--kind", "task", "--type", "project", "--name", "foo"], None, "item:foo"),
    (["add", "task", "--target", "project:foo"], None, "item:foo"),
    (["add", "--kind", "task", "--type", "item"], None, "needs a name"),
    (["add", "--kind", "task", "--type", "item", "--name", ""], None, "needs a name"),
    (["add", "--kind", "task", "--type", "item", "--name", "  "], None, "needs a name"),
    (["add", "--kind", "task", "--type", "commit"], None, "needs a name"),
    (["add", "--kind", "task", "--type", "project", "--ref", "item:"], None, "needs a name"),
    (["add", "--kind", "task", "--type", "project", "--ref", "project:foo"], None, "item:foo"),
    (["add", "--from-json", "-"], '{"kind": "task", "target": {"type": "item"}}\n', "needs a name"),
    (["add", "--from-json", "-"], '{"kind": "task", "target": {"type": "project", "name": "foo"}}\n',
     "item:foo"),
])
def test_a_target_name_is_present_exactly_when_the_type_is_not_project(
        tmp_path, capsys, monkeypatch, argv, stdin, says):
    """A project target has no name and every other target has one; nothing
    checked either. `--type project --name foo` stored `project:foo`, which
    `context --target project:` never shows (measured 2026-09-26: six such rows
    in one adopter store, written for named subjects), and a nameless item
    printed as `item:None` with a hint to query the literal name "None"."""
    if stdin:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert run(*argv, store_dir=tmp_path) == 1
    assert says in capsys.readouterr().err
    assert not store.exists(tmp_path) or store.load(tmp_path) == []


def test_resolve_ref_adds_to_the_inherited_refs(tmp_path, capsys):
    """Two sessions, two days apart, typed `resolve <id> --ref commit:…` and
    got `unrecognized arguments`; the workaround, `supersede --status resolved
    --ref`, REPLACES the refs and drops the inherited ones without a word.
    On resolve the flag adds: the inherited refs are never re-resolved, so a
    file since deleted prints no catalog-miss note."""
    _declare(tmp_path, '[catalogs]\nfile = "printf \'live.py\\\\n\'"\n')
    run("add", "--kind", "bug", "--type", "item", "--name", "x", "--ref", "item:a",
        "--ref", "file:gone.py", store_dir=tmp_path)
    out = capsys.readouterr()
    nid = out.out.strip()
    assert "'gone.py' matches nothing" in out.err, "the miss this test looks for is reported"
    assert run("resolve", nid, "--ref", "item:b", "--ref", "item:a", "--ref", "file:live",
               store_dir=tmp_path) == 0
    assert "gone.py" not in capsys.readouterr().err, "an inherited ref was re-resolved"
    head = store.heads(store.load(tmp_path))[0]
    assert head.status == "resolved"
    assert [(r.type, r.name) for r in head.refs] == [
        ("item", "a"), ("file", "gone.py"), ("item", "b"), ("file", "live.py")]
    assert run("resolve", head.id, "--ref", "itm:c", store_dir=tmp_path) == 1
    assert "itm" in capsys.readouterr().err
    assert len(store.load(tmp_path)) == 2


def test_supersede_ref_empty_clears_the_refs(tmp_path, capsys):
    """`--due ''` and `--arc-id ''` cleared their fields; `--ref ''` was
    refused as `unknown ref type ''`, so a wrong ref could be replaced but
    never removed."""
    run("add", "--kind", "note", "--type", "item", "--name", "x", "--ref", "item:a",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("supersede", nid, "--ref", "", store_dir=tmp_path) == 0
    assert store.heads(store.load(tmp_path))[0].refs == ()


def test_supersede_add_ref_keeps_the_refs_and_a_replace_names_what_it_dropped(
        tmp_path, capsys):
    """`resolve --ref` adds and `supersede --ref` replaces: a session that
    meant to add a fixing commit with supersede dropped the row's only ref,
    and nothing said so (2026-10-01). `--add-ref` adds, as `--add-tag` does,
    and a `--ref` or `--tag` that drops an inherited value names it. Named
    was not enough: more drops by 2026-10-15, so a `--ref` that drops
    one is refused unless '' leads, and its drop note then gives no advice."""
    run("add", "--kind", "note", "--type", "item", "--name", "x", "--ref", "item:a",
        "--tag", "keep", "--tag", "star", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("supersede", nid, "--add-ref", "item:b", "--add-tag", "new",
               store_dir=tmp_path) == 0
    assert "dropping" not in capsys.readouterr().err
    head = store.heads(store.load(tmp_path))[0]
    assert [(r.type, r.name) for r in head.refs] == [("item", "a"), ("item", "b")]
    assert head.tags == ("keep", "new", "star")
    n = len(store.load(tmp_path))
    assert run("supersede", head.id, "--ref", "item:b", "--tag", "keep", "--tag", "new",
               store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "would drop item:a; nothing was written. --add-ref adds one" in err, err
    assert "lead with --ref ''" in err
    assert len(store.load(tmp_path)) == n
    assert run("supersede", head.id, "--ref", "item:a", "--ref", "item:b", "--ref", "item:c",
               store_dir=tmp_path) == 0, "a superset drops nothing"
    capsys.readouterr()
    head = store.heads(store.load(tmp_path))[0]
    assert run("supersede", head.id, "--ref", "", "--ref", "item:b", "--tag", "keep",
               "--tag", "new", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "--ref replaced the refs, dropping item:a, item:c\n" in err, err
    assert "--tag replaced the tags, dropping 'star'; --add-tag" in err
    assert [(r.type, r.name) for r in store.heads(store.load(tmp_path))[0].refs] \
        == [("item", "b")]


@pytest.mark.parametrize("argv", [
    ["add", "--kind", "note", "--type", "item", "--name", "x", "--ref", "item:x"],
    ["add", "--kind", "note", "--type", "project", "--ref", "project:"],
    ["supersede", "SELF", "--ref", "item:y"],
    ["resolve", "SELF", "--ref", "item:y"],
    ["resolve", "SELF", "--add-ref", "item:y"],      # supersede's word, typed by habit
])
def test_a_ref_to_the_rows_own_target_is_refused(tmp_path, capsys, argv):
    """A self-edge adds nothing, and `context --target` then reported the row
    twice (a fresh agent's skill test, 2026-09-24)."""
    run("add", "--kind", "task", "--type", "item", "--name", "y", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run(*[nid if a == "SELF" else a for a in argv], store_dir=tmp_path) == 1
    assert "own target" in capsys.readouterr().err
    assert len(store.load(tmp_path)) == 1


def test_the_target_list_prints_is_one_context_accepts(tmp_path, capsys):
    """`list` printed a project target as `project:(project)`; pasted into
    `context --target` it read `0 notes` at exit 0."""
    run("add", "--kind", "task", "--type", "project", store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    target = capsys.readouterr().out.splitlines()[1].split()[1]
    run("context", "--target", target, store_dir=tmp_path)
    assert capsys.readouterr().out.startswith("1 entry for project:"), target


def test_the_target_name_rule_covers_supersede_seed_and_rename(tmp_path, capsys):
    run("add", "--kind", "task", "--type", "item", "--name", "x", "--ref", "project:",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert nid, "the legal shapes still write: item with a name, project ref without"
    assert run("supersede", nid, "--ref", "item:", store_dir=tmp_path) == 1
    assert "needs a name" in capsys.readouterr().err
    run("arc", "create", "--name", "A", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    for argv in (["arc", "seed", aid, "--scope", "item", "--name", ""],
                 ["arc", "seed", "--scope", "item", "--name", "", "--dry-run"],
                 ["rename", "x", "", "--type", "item"]):
        assert run(*argv, store_dir=tmp_path) == 1, argv
        assert "needs a name" in capsys.readouterr().err, argv
    assert len(store.load(tmp_path)) == 1


def test_rows_filed_on_a_named_project_move_to_an_item(tmp_path, capsys):
    """A store from 0.1.0 holds `project:NAME` rows, which the name rule now
    refuses, so a follow-up could not share its predecessor's target and
    `rename` kept the type (2026-09-30). The refusal names the stored rows
    and the command that moves them; `--to-type` moves target and ref alike,
    each check keeping its stamp."""
    store.ensure_store(tmp_path)
    legacy = [store.note_from_dict({
        "id": store.new_id(), "kind": kind, "created_at": "2026-09-22T00:00:00Z",
        "target": target, "refs": refs, "provenance": {"sha": "a" * 40},
        **({"checked": "count", "result": "7"} if kind == "check" else {})})
        for kind, target, refs in (
            ("check", {"type": "project", "name": "cluster"}, []),
            ("note", {"type": "project", "name": None}, [{"type": "project", "name": "cluster"}]))]
    for n in legacy:
        store._append_note_unlocked(tmp_path, n)

    for argv in (["--target", "project:cluster"], ["--target", "item:x", "--ref", "project:cluster"]):
        assert run("add", "note", *argv, store_dir=tmp_path) == 1
        err = capsys.readouterr().err
        assert "this store holds 2 entries on project:cluster" in err, argv
        assert " rename cluster cluster --type project --to-type item` moves them" in err, argv
    assert run("add", "note", "--target", "project:other", store_dir=tmp_path) == 1
    assert "moves them" not in capsys.readouterr().err, "no entries, no migration"

    assert run("rename", "cluster", "cluster", "--type", "project", "--to-type", "item",
               store_dir=tmp_path) == 0
    assert capsys.readouterr().out == \
        "re-targeted 1 entry, re-pointed 1 ref: project:cluster -> item:cluster\n"
    hs = {n.id: n for n in store.heads(store.load(tmp_path))}
    moved, repointed = hs[legacy[0].id], hs[legacy[1].id]
    assert moved.target == store.Target("item", "cluster")
    assert moved.provenance == legacy[0].provenance, "a re-target is not a re-run"
    assert repointed.refs == (store.Target("item", "cluster"),)
    assert run("add", "note", "--target", "project:cluster", store_dir=tmp_path) == 1
    assert "moves them" not in capsys.readouterr().err, "moved entries are not counted"


def test_rename_to_another_type_resolves_the_name_as_that_type(repo, tmp_path, capsys):
    """`new` is resolved as the type it moves to: an item named by a short sha
    moved onto `commit` lands on the full sha, as `add` would store it."""
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "c1")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True).stdout.strip()
    s = tmp_path / "s"
    run("add", "note", "--target", "item:the fix", store_dir=s)
    capsys.readouterr()
    assert run("rename", "the fix", sha[:7], "--type", "item", "--to-type", "commit",
               store_dir=s) == 0
    assert capsys.readouterr().out.endswith(f"item:the fix -> commit:{sha}\n")
    assert [n.target for n in store.heads(store.load(s))] == [store.Target("commit", sha)]


def test_rename_onto_an_arc_needs_the_arc(tmp_path, capsys):
    """`rename … --to-type arc` moved rows onto an arc no page reaches, where
    `add --target arc:nosuch` is refused."""
    run("add", "note", "--target", "item:thing", store_dir=tmp_path)
    assert run("rename", "thing", "nosuch", "--type", "item", "--to-type", "arc",
               store_dir=tmp_path) == 1
    assert "no arc 'nosuch'" in capsys.readouterr().err
    assert [n.target for n in store.load(tmp_path)] == [store.Target("item", "thing")]
    run("arc", "create", "--name", "real", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    assert run("rename", "thing", aid, "--type", "item", "--to-type", "arc",
               store_dir=tmp_path) == 0
    assert [n.target for n in store.heads(store.load(tmp_path))] == [store.Target("arc", aid)]


def test_arc_list_aligns_the_progress_column_on_the_longest_id(tmp_path, capsys):
    long = "an arc whose slug runs past thirty-two characters"
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    run("arc", "create", "--name", long, "--scope", "item", store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "list", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert len({ln.index(" 0/0") for ln in lines}) == 1


def test_dir_is_accepted_after_the_subcommand(tmp_store, tmp_path):
    """_extract_dir's whole reason to exist: a leading-only global --dir
    would need docs warning agents never to put it later. Every
    other test in this file passes it first, which cannot see the difference."""
    assert cli.main(["add", "--kind", "note", "--type", "project",
                     "--body", "b", "--dir", str(tmp_path)]) == 0
    assert cli.main(["add", "--kind", "note", "--type", "project",
                     "--body", "b2", f"--dir={tmp_path}"]) == 0
    assert len(store.load(tmp_path)) == 2


def test_supersede_ref_replaces_the_inherited_refs_and_checks_the_type(tmp_path, capsys):
    """Attaching a decision to an object after the fact had no path but prose,
    which is exactly what --ref exists to avoid. Same type check as add,
    through the shared _refs_from_flags + api.check_refs, and the same
    resolution under the lock (canonicalize_rows)."""
    run("add", "--kind", "decision", "--type", "project", "--body", "d",
        "--ref", "item:one", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()

    assert run("supersede", nid, "--ref", "itm:two", store_dir=tmp_path) == 1
    assert "itm" in capsys.readouterr().err
    assert len(store.load(tmp_path)) == 1, "a refused supersede writes nothing"

    assert run("supersede", nid, "--body", "d2", store_dir=tmp_path) == 0
    inherited = store.heads(store.load(tmp_path))[0]
    assert [r.name for r in inherited.refs] == ["one"], "omitting --ref inherits"

    assert run("supersede", inherited.id, "--ref", "", "--ref", "item:two",
               store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert [(r.type, r.name) for r in head.refs] == [("item", "two")]


def test_arc_todo_and_list_arc_agree_on_the_open_set(tmp_path, capsys):
    """The invariant that broke: these are the two ways to read one campaign,
    and they disagreed silently -- `todo` collapsed several items on one target
    to one and dropped bugs outright, while `list --arc` showed them
    all. A user reading either one alone cannot tell which is short."""
    run("arc", "create", "--name", "C", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    for body in ("first", "second"):
        run("add", "--kind", "task", "--type", "item", "--name", "x.h",
            "--status", "open", "--arc-id", aid, "--body", body, store_dir=tmp_path)
    run("add", "--kind", "bug", "--type", "item", "--name", "broken",
        "--status", "open", "--arc-id", aid, store_dir=tmp_path)
    capsys.readouterr()

    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    todo = {n["id"] for n in json.loads(capsys.readouterr().out)}
    run("list", "--arc", aid, "--json", store_dir=tmp_path)
    listed = {n["id"] for n in json.loads(capsys.readouterr().out)
              if n["status"] == "open"}
    assert todo == listed, f"todo is short by {listed - todo}"
    assert len(todo) == 3


def test_supersede_can_file_a_note_under_an_arc(repo, tmp_store, tmp_path, capsys):
    """The GUI's edit dialog could do this before the CLI could, which breaks
    symbion's premise that both surfaces write the same store."""
    from symbion import api
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "project", author="ada")
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b"}, author="ada")
    assert n.arc_id is None

    rc = cli.main(["--dir", str(tmp_path), "supersede", n.id,
                   "--arc-id", act.id])
    capsys.readouterr()
    assert rc == 0
    head = store.heads(store.load(tmp_path))[0]
    assert head.arc_id == act.id


def test_supersede_can_detach_a_note_from_its_arc(repo, tmp_store, tmp_path, capsys):
    """The other direction. `--arc-id ''` detaches; without the empty-string
    case the flag can attach but never undo an attach."""
    from symbion import api
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "project", author="ada")
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b", "arc_id": act.id}, author="ada")
    assert n.arc_id == act.id

    rc = cli.main(["--dir", str(tmp_path), "supersede", n.id, "--arc-id", ""])
    capsys.readouterr()
    assert rc == 0
    assert store.heads(store.load(tmp_path))[0].arc_id is None


def test_add_tag_fast_path_does_not_swallow_an_arc_edit(repo, tmp_store, tmp_path, capsys):
    """--add-tag routes through api.retag, which knows nothing about
    arc_id. Combining the two must take the full supersede path or the
    arc edit is silently dropped."""
    from symbion import api
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "project", author="ada")
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b", "tags": ["keep"]}, author="ada")

    rc = cli.main(["--dir", str(tmp_path), "supersede", n.id,
                   "--add-tag", "extra", "--arc-id", act.id])
    capsys.readouterr()
    assert rc == 0
    head = store.heads(store.load(tmp_path))[0]
    assert head.arc_id == act.id, "the arc edit was dropped"
    assert "extra" in head.tags


def test_list_id_resolves_a_superseded_row_and_reports_a_miss(tmp_path, capsys):
    """A superseded id shows its chain's head, as `resolve`, `supersede` and
    `--grep` already took it: an id cited in a handoff went stale within
    hours, and its `--json` `status` read `open` on a resolved thread
    (dogfood, 2026-10-11). Until then `--id` meant "you named an exact row";
    `--all` still reads that version. A miss goes to stderr with exit 1 --
    an empty listing would be indistinguishable from a row that exists and
    matched nothing else."""
    run("add", "--kind", "note", "--type", "project", "--body", "first",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("supersede", nid, "--body", "second", store_dir=tmp_path)
    head = capsys.readouterr().out.strip()
    assert head == nid, "an edit keeps the entry's id"
    nid = _earlier(tmp_path, capsys, head)

    for argv in (["list", "--id", nid, "--json"], ["show", nid, head, "--json"]):
        assert run(*argv, store_dir=tmp_path) == 0
        out, err = capsys.readouterr()
        assert [(r["id"], r["body"]) for r in json.loads(out)] == [(head, "second")], argv
        assert err == f"note: {nid} was superseded; this is its head, {head} " \
                      f"(--all shows {nid} itself)\n", err
    for argv in (["list", "--id", nid, "--json", "--all"], ["show", "--all", nid, "--json"]):
        assert run(*argv, store_dir=tmp_path) == 0
        out, err = capsys.readouterr()
        assert [(r["id"], r["body"]) for r in json.loads(out)] == [(nid, "first")], argv
        assert not err, err

    assert run("list", "--id", "no-such-id", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    # A newcomer typed `show 6` (a tail must be a whole segment) and
    # `show "login page"` (a search): the miss names both forms.
    assert err == ("no entry found: no-such-id (an id, or its tail after a dash: 123456-a1b, "
                   "a1b; list --grep TEXT searches the entries)\n"), err
    assert run("resolve", "no-such-id", store_dir=tmp_path) == 1
    assert "list --grep TEXT searches" in capsys.readouterr().err, "same helper on every miss"


def test_show_into_a_pipe_indents_every_line_of_a_body(tmp_path, capsys):
    """Only a body's first line was indented, and three fresh agents each
    read its second paragraph as outside the row (2026-10-11). Blank lines
    stay blank, and the body's own indents are kept under the row's."""
    run("add", "--kind", "note", "--type", "project", "--body",
        "first\n\nsecond\n  - nested", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--append", "--author", "human", "--body", "reply\nmore",
        store_dir=tmp_path)
    capsys.readouterr()
    assert run("show", nid, store_dir=tmp_path) == 0
    out = capsys.readouterr().out.splitlines()
    i = out.index("    first")
    assert out[i:i + 7] == ["    first", "", "    second", "      - nested",
                            out[i + 4], "    reply", "    more"], out
    assert out[i + 4].startswith("    -- human added below"), out


def test_show_reads_the_rows_a_body_cites_through_their_heads(tmp_path, capsys, monkeypatch):
    """A body's cite of a row here prints with that row's head, and the
    rows that cite any version of this one are named. Across many stores,
    over half the cites between rows named a version since superseded,
    mostly by a resolve, and only a `show` of each said so (dogfood,
    2026-10-11).
    Another store's id, and a cite of the row's own chain, are no link."""
    run("add", "--kind", "bug", "--type", "project", "--body", "npm audit fails",
        store_dir=tmp_path)
    bug = capsys.readouterr().out.strip()
    run("add", "--kind", "task", "--type", "project", "--body",
        f"CI goes green; blocked by `{bug[-10:]}`, not 20990101-000000-000000-abc",
        store_dir=tmp_path)
    task = capsys.readouterr().out.strip()
    run("add", "--kind", "note", "--type", "project", "--body", f"see {bug[-17:]}",
        store_dir=tmp_path)
    see = capsys.readouterr().out.strip()
    run("resolve", bug, "--body", f"was {bug[-10:]}; the lockfile", store_dir=tmp_path)
    assert capsys.readouterr().out.strip() == bug, "a resolve keeps the entry's id"
    kept = _earlier(tmp_path, capsys, bug)
    run("add", "--kind", "note", "--type", "project", "--body", f"as {kept[-10:]} said",
        store_dir=tmp_path)
    old = capsys.readouterr().out.strip()

    def links(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        return [ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith(("    cites ", "    cited by "))]

    assert links("show", task) == [
        f"    cites {bug[-10:]} bug [resolved]: npm audit fails was {bug[-10:]}; the lockfile"]
    assert links("show", old) == [
        f"    cites {kept[-10:]} -> {bug[-10:]} bug [resolved]: npm audit fails was "
        f"{bug[-10:]}; the lockfile"]
    assert links("show", bug) == [
        f"    cited by {old[-10:]} note: as {kept[-10:]} said",
        f"    cited by {see[-10:]} note: see {bug[-17:]}",
        f"    cited by {task[-10:]} task [open]: CI goes green; blocked by `{bug[-10:]}`, "
        f"not 20990101-000000-000000-abc"]
    assert links("list", "--full") == [], "a listing is not one entry read"

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("show", old, store_dir=tmp_path)
    out = _ANSI.sub("", capsys.readouterr().out)
    assert f"cites {kept[-10:]} -> {bug[-10:]} bug [resolved]: npm audit" in out, out


def test_a_cited_check_with_no_body_leads_with_what_it_found(tmp_path, capsys):
    """A check with `--checked` and `--result` and no body, SKILL.md's first
    example, printed `cites <id> check` and nothing more (2026-10-11)."""
    run("add", "check", "--target", "project", "--checked", "pytest -k up --count 50",
        "--result", "50 passed", store_dir=tmp_path)
    ran = capsys.readouterr().out.strip()
    run("add", "check", "--target", "project", "--checked", "make lint", "--result", "",
        store_dir=tmp_path)
    bare = capsys.readouterr().out.strip()
    run("add", "note", "--target", "project", "--body", f"per {ran[-10:]}, {bare[-10:]}",
        store_dir=tmp_path)
    see = capsys.readouterr().out.strip()
    run("show", see, store_dir=tmp_path)
    assert [ln for ln in capsys.readouterr().out.splitlines() if "    cites " in ln] == [
        f"    cites {ran[-10:]} check: result: 50 passed",
        f"    cites {bare[-10:]} check: checked: make lint"]


def test_a_superseded_row_names_the_head_of_its_chain(tmp_path, capsys):
    """`list --id` on a superseded bug printed `[open]` and nothing else; its
    chain ended two hops later in a resolved row (2026-09-23), and it read
    as a live bug. The head is named, not the next hop, and the head
    itself carries no marker. The old row's own `[open]` and its due phrase
    are history: printed first, they still read as live (2026-09-28). The
    head's status in brackets read as the row's own, and with a prediction's
    `state=pending` and `(due in 9d)`, as a second open prediction
    (2026-10-14): it prints in parentheses after the head's id, and the old
    row prints no state and no due phrase."""
    run("add", "--kind", "bug", "--type", "project", "--body", "b",
        "--due", "2020-01-01", store_dir=tmp_path)
    first = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("supersede", first, "--body", "c", store_dir=tmp_path)
    mid = capsys.readouterr().out.strip()
    assert mid == first, "an edit keeps the entry's id"
    first = _earlier(tmp_path, capsys, mid)

    assert run("list", "--id", first, "--all", store_dir=tmp_path) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert f"project: superseded -> {mid} (open) due=2020-01-01  " in line, line

    run("resolve", mid, store_dir=tmp_path)
    head = capsys.readouterr().out.strip()
    assert run("list", "--id", first, "--all", store_dir=tmp_path) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert f"project: superseded -> {head} (resolved) due=2020-01-01  " in line, line
    run("list", "--id", first, "--all", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)[0]["head"] == head
    run("list", "--all", store_dir=tmp_path)
    assert capsys.readouterr().out.count("superseded ->") == 2, "both old entries, not the head"
    run("list", "--id", head, "--json", store_dir=tmp_path)
    assert "head" not in json.loads(capsys.readouterr().out)[0]

    run("add", "prediction", "--target", "item:p", "--body", "p", "--checked", "c",
        "--due", "2099-01-01", store_dir=tmp_path)
    pid = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("supersede", pid, "--append", "--body", "more", store_dir=tmp_path)
    capsys.readouterr()
    old = _earlier(tmp_path, capsys, pid)
    run("list", "--all", "--grep", "^p", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    was = next(ln for ln in lines if ln.endswith(old))
    assert f"superseded -> {pid} (open) due=2099-01-01  " in was, was
    assert "state=" not in was and "[" not in was, was
    now = next(ln for ln in lines if ln.endswith(pid))
    assert "[open] state=pending due=2099-01-01 (due in" in now, now


def test_seed_refusal_points_at_the_catalog_config(tmp_path, capsys):
    """After a fresh init no catalogs are configured, so `item` is the only
    scope on offer and the --name help's 'omit to sweep the catalog' is advice
    that cannot work. The refusal must name where catalogs live or the reader
    has nothing to act on. Other direction: a configured scope still sweeps --
    test_seed_dry_run_creates_nothing covers it."""
    assert run("arc", "seed", "x", "--scope", "item", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "--name" in err, "the CLI message names the flag, not the concept"
    assert "[catalogs]" in err and "symbion.toml" in err


def test_add_body_file_reads_a_path_and_a_dash_reads_stdin(tmp_path, capsys,
                                                           monkeypatch):
    """Bodies are prose with contractions, and --body on a single-quoted shell
    line turns every apostrophe into a four-character escape. One flag covers
    both directions: argparse.FileType already treats '-' as stdin."""
    prose = "it's two paragraphs\n\nand it's got apostrophes"
    p = tmp_path / "b.md"
    p.write_text(prose)
    run("add", "--kind", "note", "--type", "project", "--body-file", str(p),
        store_dir=tmp_path)
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO(prose + " piped"))
    run("add", "--kind", "note", "--type", "project", "--body-file", "-",
        store_dir=tmp_path)
    capsys.readouterr()

    assert sorted(n.body for n in store.load(tmp_path)) == \
        sorted([prose, prose + " piped"])


def test_body_and_body_file_together_are_refused(tmp_path, capsys):
    """Two sources for one field is a silent coin flip; argparse refuses it.
    Assert the REASON, not just the 2 -- an unrecognized flag exits 2 as well,
    so a bare code check would also pass on a build with no --body-file."""
    p = tmp_path / "b.md"
    p.write_text("x")
    # main() catches argparse's SystemExit and returns its code.
    assert run("add", "--kind", "note", "--type", "project", "--body", "y",
               "--body-file", str(p), store_dir=tmp_path) == 2
    assert "not allowed with" in capsys.readouterr().err


def test_a_missing_body_file_is_an_error_not_a_traceback(tmp_path, capsys):
    """OSError reaches no handler in main(); without the wrap the user gets a
    stack trace for a typo'd path."""
    assert run("add", "--kind", "note", "--type", "project",
               "--body-file", str(tmp_path / "nope.md"), store_dir=tmp_path) == 1
    assert "--body-file" in capsys.readouterr().err


def test_from_json_still_refuses_a_body_file(tmp_path, capsys):
    """--body-file must not slip past the guard --body trips. It resolves into
    the same args.body, and this is the assertion that keeps it there."""
    p = tmp_path / "b.md"
    p.write_text("x")
    assert run("add", "--from-json", "-", "--body-file", str(p),
               store_dir=tmp_path) == 2
    assert "no other note flags" in capsys.readouterr().err


def test_supersede_takes_a_body_file_too(tmp_path, capsys):
    """A correction is as long as the thing it corrects, and the GUI edit
    dialog is a textarea for both. The CLI should not be the surface where an
    apostrophe decides how you write."""
    run("add", "--kind", "note", "--type", "project", "--body", "first",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    p = tmp_path / "b.md"
    p.write_text("second, with an apostrophe's worth of prose")
    run("supersede", nid, "--body-file", str(p), store_dir=tmp_path)
    capsys.readouterr()

    head = store.heads(store.load(tmp_path))[0]
    assert head.body == "second, with an apostrophe's worth of prose"


@pytest.mark.parametrize("verb", ["supersede", "resolve", "add"])
@pytest.mark.parametrize("piped", ["", " \n\n"])
def test_a_body_file_that_reads_nothing_writes_nothing(tmp_path, capsys, monkeypatch,
                                                       verb, piped):
    """`producer | symbion supersede <id> --body-file -` with a producer that
    failed (no pipefail) read "" and replaced a long body at exit 0. Empty
    stdin is a broken producer far more often than intent; `--body ''` says intent."""
    run("add", "task", "--target", "project", "--body", "keep me", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    before = store.load(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(piped))
    args = {"supersede": ["supersede", nid], "resolve": ["resolve", nid],
            "add": ["add", "task", "--target", "project"]}[verb]
    assert run(*args, "--body-file", "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "--body-file" in err and "nothing was written" in err
    assert ("--body ''" in err) == (verb == "supersede")
    assert store.load(tmp_path) == before


@pytest.mark.parametrize("flag", ["--append-file", "--prepend-file"])
def test_a_mode_file_that_reads_nothing_names_its_own_flag(tmp_path, capsys, monkeypatch,
                                                           flag):
    """`supersede ID --append-file -` on empty stdin was refused as
    `--body-file -`, a flag the caller never typed, with a hint to blank the
    body, which an addition cannot do (dogfood, 2026-10-15)."""
    nid = _add(tmp_path, capsys, "task", "--target", "project", "--body", "keep me")
    monkeypatch.setattr("sys.stdin", io.StringIO(" \n"))
    assert run("supersede", nid, flag, "-", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert f"{flag} -: read nothing but whitespace" in err and "--body ''" not in err, err
    assert run("supersede", nid, flag, str(tmp_path / "missing.md"), store_dir=tmp_path) == 1
    assert f"{flag}: " in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["resolve", "supersede"])
def test_a_field_the_kind_cannot_hold_is_named_when_a_write_drops_it(tmp_path, capsys,
                                                                    verb):
    """A legacy bug carrying checked/result (a migrated store's) loses them
    from the head on its next write: the store drops what the kind cannot
    hold. Silent, that is text loss; said, the writer can move it into the body."""
    run("add", "bug", "--target", "project", "--body", "b", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    path = store.notes_path(tmp_path)
    path.write_text(path.read_text(encoding="utf-8")
                    .replace('"checked": null', '"checked": "ran it"')
                    .replace('"result": null', '"result": "it failed"'), encoding="utf-8")
    args = ["--body", "fixed"] if verb == "resolve" else ["--add-tag", "x"]
    assert run(verb, nid, *args, store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert out.strip() == nid
    assert "checked, result" in err and "verdict bit" in err
    assert f"its earlier version {_earlier(tmp_path, capsys, nid)} keeps them" in err, err

    capsys.readouterr()
    assert run("supersede", out.strip(), "--add-tag", "y", store_dir=tmp_path) == 0
    assert "keeps them" not in capsys.readouterr().err, "a clean entry says nothing"


def test_a_rename_names_the_rows_that_dropped_a_field(tmp_path, capsys):
    """A sweep supersedes each row it moves, so a legacy row's field its kind
    cannot hold left the head with only the sweep's counts printed
    (reproduced 2026-10-03)."""
    run("add", "decision", "--target", "item:a", "--body", "d", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("add", "decision", "--target", "item:a", "--body", "clean", store_dir=tmp_path)
    path = store.notes_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[0] = lines[0].replace('"checked": null', '"checked": "ran it"')
    path.write_text("".join(lines), encoding="utf-8")
    capsys.readouterr()
    assert run("rename", "a", "b", "--type", "item", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert (f"note: 1 entry dropped checked, a field its kind cannot hold; the old entry "
            f"keeps it: show {_earlier(tmp_path, capsys, nid)} --all --json") in err, err
    capsys.readouterr()
    assert run("rename", "b", "c", "--type", "item", store_dir=tmp_path) == 0
    assert "dropped" not in capsys.readouterr().err, "the moved entries are clean now"


def test_resolve_body_and_add_tag_land_on_the_resolving_row(repo, tmp_path, capsys):
    """A question's answer and an idea's fate are the value of closing them;
    one command, one row. The answer goes below the question: replacing it
    left 111 of 149 adopter resolves without their own claim (2026-09-26).
    `--append` is still accepted, and supersede is the way to replace."""
    for i, extra in enumerate(([], ["--append"])):
        run("add", "--kind", "question", "--type", "item", "--name", f"q{i}",
            "--body", "which?", "--tag", "design", store_dir=tmp_path)
        qid = capsys.readouterr().out.strip()
        assert run("resolve", qid, "--body", "decided: the first", "--add-tag", "adopted",
                   *extra, store_dir=tmp_path) == 0
        capsys.readouterr()
        head, = [h for h in store.heads(store.load(tmp_path)) if h.target.name == f"q{i}"]
        assert (head.status, head.body, head.tags) == \
            ("resolved", "which?\n\ndecided: the first", ("adopted", "design")), extra
    run("add", "--kind", "question", "--type", "item", "--name", "r", "--body", "which?",
        store_dir=tmp_path)
    qid = capsys.readouterr().out.strip()
    assert run("supersede", qid, "--status", "resolved", "--body", "decided",
               store_dir=tmp_path) == 0
    head, = [h for h in store.heads(store.load(tmp_path)) if h.target.name == "r"]
    assert (head.status, head.body) == ("resolved", "decided")


def test_bare_resolve_inherits_body_and_tags(repo, tmp_path, capsys):
    run("add", "--kind", "task", "--type", "item", "--name", "t", "--body", "do it",
        "--tag", "x", store_dir=tmp_path)
    tid = capsys.readouterr().out.strip()
    assert run("resolve", tid, store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.status, head.body, head.tags) == ("resolved", "do it", ("x",))


def test_resolve_body_file_reads_stdin(repo, tmp_path, capsys, monkeypatch):
    run("add", "--kind", "idea", "--type", "project", "--body", "maybe", store_dir=tmp_path)
    iid = capsys.readouterr().out.strip()
    monkeypatch.setattr("sys.stdin", io.StringIO("retired: no demand\n"))
    assert run("resolve", iid, "--body-file", "-", store_dir=tmp_path) == 0
    assert store.heads(store.load(tmp_path))[0].body == "maybe\n\nretired: no demand\n"


def test_append_adds_to_the_body_on_supersede_and_resolve(repo, tmp_path, capsys,
                                                          monkeypatch):
    """--append turns --body/--body-file into an addition after the current
    body. On resolve it is how a question keeps its question beside the answer."""
    run("add", "--kind", "question", "--type", "item", "--name", "q",
        "--body", "which?\n", store_dir=tmp_path)
    qid = capsys.readouterr().out.strip()
    assert run("supersede", qid, "--append", "--body", "AMENDED: or both?",
               store_dir=tmp_path) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("decided: the first\n"))
    assert run("resolve", qid, "--append", "--body-file", "-", store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.status, head.body) == (
        "resolved", "which?\n\nAMENDED: or both?\n\ndecided: the first\n")


@pytest.mark.parametrize("verb", ["supersede", "resolve"])
def test_append_without_a_body_is_refused_by_name(repo, tmp_path, capsys, verb):
    """Bare --append has nothing to add. Refused with the flags that supply
    it, and nothing written -- a no-op resolve would still close the row."""
    run("add", "--kind", "task", "--type", "item", "--name", "t", "--body", "do it",
        store_dir=tmp_path)
    tid = capsys.readouterr().out.strip()
    assert run(verb, tid, "--append", store_dir=tmp_path) != 0
    assert "--body" in capsys.readouterr().err
    assert len(store.load(tmp_path)) == 1


def test_supersede_corrects_a_check_verdict(tmp_path, capsys):
    """`checked` and `result` are what `list` prints and what makes a check
    queryable rather than prose, so a typo in a verdict was permanent in the
    way the body never was -- supersede could rewrite the prose and not the
    finding. Backwards, and store.supersede took **fields all along."""
    run("add", "--kind", "check", "--type", "project", "--body", "ran it",
        "--checked", "the suit", "--result", "412 pased", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()

    assert run("supersede", nid, "--checked", "the suite",
               "--result", "412 passed", store_dir=tmp_path) == 0
    capsys.readouterr()
    head = store.heads(store.load(tmp_path))[0]
    assert (head.checked, head.result) == ("the suite", "412 passed")
    assert head.body == "ran it", "omitting --body still inherits it"


def test_add_tag_fast_path_does_not_swallow_a_verdict(tmp_path, capsys):
    """--add-tag alone routes through api.retag, which carries tags and
    nothing else. Combined with --checked/--result it must fall through to
    the full supersede, or the verdict is dropped while the command prints a
    new id and exits 0 -- a silent empty result wearing a success."""
    run("add", "--kind", "check", "--type", "project", "--body", "b",
        "--checked", "c", "--result", "wrong", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()

    assert run("supersede", nid, "--add-tag", "release", "--result", "right",
               store_dir=tmp_path) == 0
    capsys.readouterr()
    head = store.heads(store.load(tmp_path))[0]
    assert head.result == "right", "the retag fast path swallowed --result"
    assert set(head.tags) == {"release"}, "and the tag still landed"


from symbion import kinds as K

PREREG = ('[kinds]\nprediction = { status = true, verdict = true }\n'
          'task = { status = true }\nnote = {}\n')


def _declare(store_dir, text):
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(text)


def test_prepend_puts_a_new_lead_above_the_body_but_never_a_pre_registrations(
        repo, tmp_path, capsys):
    """Correcting a long row's lead took a rebuild by hand: list --json, the
    body to one file, the new lead to another, cat, --body-file - (2026-10-02).
    A pre-registration keeps its registered text on top, so it refuses."""
    _declare(tmp_path, PREREG)
    run("add", "task", "--target", "item:t", "--body", "STILL BROKEN\n\ntables",
        store_dir=tmp_path)
    tid = capsys.readouterr().out.strip()
    assert run("supersede", tid, "--prepend", "--body", "FIXED", store_dir=tmp_path) == 0
    assert store.heads(store.load(tmp_path))[0].body == "FIXED\n\nSTILL BROKEN\n\ntables"
    assert run("supersede", tid, "--prepend", "--append", "--body", "x",
               store_dir=tmp_path) != 0
    capsys.readouterr()
    run("add", "prediction", "--target", "item:p", "--checked", "run x", "--body", "x rises",
        store_dir=tmp_path)
    pid = capsys.readouterr().out.strip()
    assert run("supersede", pid, "--prepend", "--body", "lead", store_dir=tmp_path) == 1
    assert "pre-registration" in capsys.readouterr().err
    assert len(store.load(tmp_path)) == 2


def test_a_pre_registrations_registered_text_and_run_take_no_rewrite(repo, tmp_path, capsys):
    """`--prepend` refused, but `--body` replaced a committed prediction's
    text at exit 0, and `show` printed `registered at <sha>:` above the new
    text (2026-10-13). The registered text and run stay as written; a body
    that keeps the text as its prefix, as the GUI's dialog sends, still goes."""
    run("add", "prediction", "--target", "item:p", "--checked", "run x", "--body", "x rises",
        store_dir=tmp_path)
    pid = capsys.readouterr().out.strip()
    run("commit", store_dir=tmp_path)
    for flags in (["--body", "x falls"], ["--checked", "run y"]):
        capsys.readouterr()
        assert run("supersede", pid, *flags, store_dir=tmp_path) == 1, flags
        assert "pre-registration" in capsys.readouterr().err
    assert run("supersede", pid, "--body", "x rises\n\nlater: y too", "--checked", "run x",
               store_dir=tmp_path) == 0
    assert run("supersede", pid, "--append", "--body", "and z", store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.body, head.checked) == ("x rises\n\nlater: y too\n\nand z", "run x")


def test_a_pre_registrations_uncommitted_amendment_takes_a_rewrite(repo, tmp_path, capsys):
    """An amendment git has not seen is a draft: a placeholder left in an
    uncommitted `--append` took a second append to patch, since `--body`
    had to keep the whole tip (2026-10-15). The registered text and each
    committed amendment still stay as written."""
    def add(name, body):
        capsys.readouterr()
        run("add", "prediction", "--target", f"item:{name}", "--checked", "run",
            "--body", body, store_dir=tmp_path)
        return capsys.readouterr().out.strip()
    p = add("p", "x rises")
    run("supersede", p, "--append", "--body", "seen: y", store_dir=tmp_path)
    run("commit", store_dir=tmp_path)
    q = add("q", "q holds")                       # nothing of it committed
    for rid in (p, q):
        run("supersede", rid, "--append", "--body", "n = TODO", store_dir=tmp_path)
    capsys.readouterr()
    for rid, body in ((p, "x rises\n\nn = 40"), (p, "x rises\n\nseen: z\n\nn = 40"),
                      (q, "q fails\n\nn = 40")):
        assert run("supersede", rid, "--body", body, store_dir=tmp_path) == 1, body
        assert "a body may change only text added since" in capsys.readouterr().err
    for rid, body in ((p, "x rises\n\nseen: y\n\nn = 40"), (q, "q holds\n\nn = 40")):
        assert run("supersede", rid, "--body", body, store_dir=tmp_path) == 0
        assert store.chain_tip(store.load(tmp_path), rid).body == body


def test_a_fresh_store_keeps_a_predictions_registered_text(repo, tmp_path, capsys):
    """A cold-start store had no status + verdict kind. Asked to write its
    expected counts down before a run, an agent filed a `note` and appended
    the result before the first commit, which rewrote the row in place: the
    store could no longer show the prediction came first (2026-10-09). The
    defaults carry `prediction`, and an edit never rewrites one."""
    run("add", "note", "--target", "item:n", "--body", "x rises", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("add", "prediction", "--target", "item:p", "--checked", "run x",
        "--body", "x rises", store_dir=tmp_path)
    pid = capsys.readouterr().out.strip()
    for rid in (nid, pid):
        assert run("supersede", rid, "--append", "--body", "x rose", store_dir=tmp_path) == 0
    rows = {n.id: n for n in store.load(tmp_path)}
    assert rows[nid].body == "x rises\n\nx rose"        # a note's draft is rewritten
    assert rows[rows[pid].supersedes].body == "x rises" and len(rows) == 3, \
        "a prediction keeps its registered text"


def test_add_kind_choices_come_from_the_table_and_the_error_names_it(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\n')
    assert run("add", "--kind", "anomaly", "--type", "project", store_dir=tmp_path) == 0
    capsys.readouterr()
    rc = run("add", "--kind", "bug", "--type", "project", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 2 and "invalid kind 'bug'" in err and "anomaly" in err and "[kinds]" in err
    # A default the table lacks names the line to paste: an older table has
    # no `prediction`.
    assert run("add", "prediction", "--target", "item:p", store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert "add under [kinds]" in err and "prediction = { status = true, verdict = true" in err


_VOCAB = '[kinds]\nanomaly = { status = true }\nnote = {}\n[catalogs]\nfile = "echo a.py"\n'


def test_a_bare_value_set_flag_names_its_values(tmp_path, capsys):
    """argparse's "expected one argument" named the flag but not what it
    takes; `add --type` bare is how a human asks what the types are
    (2026-09-24). One case per way a flag holds its set: config-extended
    through `type=` (kind, type) and static through `choices=` (status).
    --name holds no set and keeps the stock message."""
    _declare(tmp_path, _VOCAB)
    for argv, legal in ((["add", "--type"], "arc, commit, doc, file, item, project"),
                        (["add", "--kind"], "anomaly, note"),
                        (["list", "--status"], "open, resolved")):
        assert run(*argv, store_dir=tmp_path) == 2
        assert capsys.readouterr().err.endswith(
            f"argument {argv[-1]}: expected one argument (choose from {legal})\n")
    assert run("add", "--name", store_dir=tmp_path) == 2
    assert capsys.readouterr().err.endswith("argument --name: expected one argument\n")


def test_an_unknown_flag_names_the_subcommand_and_its_usage(tmp_path, capsys):
    """argparse reported a subcommand's leftover arguments from the top-level
    parser, whose usage lists the subcommands and none of their flags, so an
    agent could not see what the typed command takes (2026-09-23..26). One
    case per depth: a verb, and a verb under `arc`."""
    for argv, prog, flag in ((["add", "--kind", "bug", "--bogus"], "symbion add", "--kind"),
                             (["resolve", "x", "--bogus"], "symbion resolve", "--body"),
                             (["arc", "create", "--name", "x", "--scope", "item", "--bogus"],
                              "symbion arc create", "--desc")):
        assert run(*argv, store_dir=tmp_path) == 2
        err = capsys.readouterr().err
        assert err.startswith(f"usage: {prog} ") and flag in err
        assert f"{prog}: error: unrecognized arguments: " in err


def test_add_without_kind_or_type_names_both_sets(tmp_path, capsys):
    _declare(tmp_path, _VOCAB)
    assert run("add", "--body", "x", store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert "kinds: anomaly, note" in err and "types: arc, commit, doc, file, item, project" in err
    assert store.load(tmp_path) == []


def test_from_json_unknown_target_type_names_the_types(tmp_path, capsys):
    """Its siblings -- unknown kind, unknown ref type -- already named the set."""
    _declare(tmp_path, _VOCAB)
    p = tmp_path / "entries.jsonl"
    p.write_text('{"kind": "note", "target": {"type": "fiel", "name": "a.py"}}\n')
    assert run("add", "--from-json", str(p), store_dir=tmp_path) == 1
    assert ("unknown target type 'fiel' (choose from arc, commit, doc, file, item, project)"
            in capsys.readouterr().err)


@pytest.mark.parametrize("argv, what", [
    (["add", "task", "--target", "item:x", "--ref", "check:1"], "ref"),
    (["add", "task", "--target", "check:1"], "target"),
    (["add", "--from-json", "{rows}"], "target"),
])
def test_a_kind_typed_as_a_type_is_told_rows_join_through_objects(tmp_path, capsys, argv, what):
    """`--ref check:<id>` meant a row, and the refusal named only the
    types (dogfood, 2026-09-30)."""
    p = tmp_path / "entries.jsonl"
    p.write_text('{"kind": "note", "target": {"type": "check", "name": "1"}}\n')
    assert run(*[a.format(rows=p) for a in argv], store_dir=tmp_path) in (1, 2)
    assert (f"'check' is a kind: a {what} names an object, never an entry. Give this entry "
            f"that entry's target, and cite its id in the body") in capsys.readouterr().err
    assert store.load(tmp_path) == []


@pytest.mark.parametrize("argv", [
    ["add", "idea", "--target", "item:x", "--ref", "item:20261002-120000-123456-abc"],
    ["add", "idea", "--target", "item:123456-abc"],
    ["add", "idea", "--target", "item:x", "--ref", "item:…120000-123456-abc"],
])
def test_an_item_named_like_a_row_id_is_refused(tmp_path, capsys, argv):
    """`--ref item:<row id>` meant "see this row" and stored a new item by
    that name at exit 0, three times in one session (2026-10-02)."""
    assert run(*argv, store_dir=tmp_path) == 1
    assert ("has the shape of an entry id: an item names an object, never an entry. Give this "
            "entry that entry's target, and cite its id in the body") in capsys.readouterr().err
    assert store.load(tmp_path) == []
    assert run("add", "idea", "--target", "item:build 123456", store_dir=tmp_path) == 0


@pytest.mark.parametrize("argv", [
    ["add", "note", "--target", "commit:--output=x"],
    ["add", "note", "--target", "item:x", "--ref", "commit:-p"],
])
def test_a_commit_name_that_reads_as_an_option_is_refused(tmp_path, capsys, argv):
    """A commit name reaches git among its arguments; `--output=PATH` was
    stored at exit 0, and every later `list` wrote the log to PATH."""
    assert run(*argv, store_dir=tmp_path) == 1
    assert "starts with '-', which git reads as an option" in capsys.readouterr().err
    assert store.load(tmp_path) == []
    assert run("add", "note", "--target", "item:--json flag", store_dir=tmp_path) == 0


def test_show_is_list_id(tmp_path, capsys):
    """Agents typed `symbion show <id>` from CLI habit and read the
    invalid-choice error as "no such command"."""
    store.ensure_store(tmp_path)
    run("add", "--kind", "note", "--type", "project", "--body", "b", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("list", "--id", nid, "--json", store_dir=tmp_path) == 0
    want = capsys.readouterr().out
    assert json.loads(want)[0]["id"] == nid
    assert run("show", nid, "--json", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == want
    # The flag first, as agents type it, read as `list --id --json ID`: a
    # usage error (2026-10-09).
    assert run("show", "--json", nid, store_dir=tmp_path) == 0
    assert capsys.readouterr().out == want
    assert run("show", "nope", store_dir=tmp_path) == 1
    capsys.readouterr()
    # a list filter read the row away: `no note found` for a row that exists
    assert run("show", nid, "--grep", "zzz", store_dir=tmp_path) == 2
    assert "symbion show: error: unrecognized arguments: --grep zzz" in capsys.readouterr().err
    assert run("show", "-h", store_dir=tmp_path) == 0, "was `list --id -h`: an error"
    assert capsys.readouterr().out.startswith("usage: symbion show [-h] [--json] [--all] id [id ...]")


def test_show_takes_several_ids(tmp_path, capsys):
    """`show A B` failed with `symbion list: error: unrecognized arguments: B`
    and printed the usage of a verb the caller never typed."""
    store.ensure_store(tmp_path)
    ids = []
    for body in ("a", "b", "c"):
        run("add", "--kind", "note", "--type", "project", "--body", body, store_dir=tmp_path)
        ids.append(capsys.readouterr().out.strip())
    # in the order given: a script pairs its arguments with the rows (2026-10-14)
    for want in ([ids[0], ids[2]], [ids[2], ids[0]]):
        assert run("show", *(i[-10:] for i in want), "--json", store_dir=tmp_path) == 0
        assert [r["id"] for r in json.loads(capsys.readouterr().out)] == want
        assert run("show", *want, store_dir=tmp_path) == 0
        out = capsys.readouterr().out
        assert out.index(want[0]) < out.index(want[1]), out
    assert run("show", ids[0], "nope", store_dir=tmp_path) == 1
    out, err = capsys.readouterr()
    assert "no entry found: nope" in err and "list" not in err.split("(")[0]


def test_show_names_who_added_each_amendment_and_when(tmp_path, capsys, tty):
    """A user's reply appended to an agent's row read, in `show`, as the
    agent's own text: only a serve page marked the seam, so the agent signed
    each paragraph by hand (2026-10-10)."""
    store.ensure_store(tmp_path)
    run("add", "--kind", "note", "--type", "project", "--author", "claude",
        "--body", "human decides", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--append", "--body", "keep it", "--author", "ada", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--prepend", "--body", "settled", "--author", "claude",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    want = ["settled", "-- claude added the above, T --", "human decides",
            "-- ada added below, T --", "keep it"]
    stamp = re.compile(r", \d{4}-\d\d-\d\d \S+ --$")
    assert run("show", nid, store_dir=tmp_path) == 0
    body = capsys.readouterr().out.splitlines()[2:]
    assert [stamp.sub(", T --", ln) for ln in body] == [f"    {w}" for w in want]
    tty()
    assert run("show", nid, store_dir=tmp_path) == 0
    lines = [stamp.sub(", T --", ln.strip()) for ln in _screen(capsys.readouterr().out)]
    assert [ln for ln in lines if ln in want] == want, lines


def test_a_unique_id_tail_stands_for_the_id_on_every_verb_that_takes_one(tmp_store, tmp_path, capsys):
    """A short tail is what gets cited (`a1b`, `…123456-a1b`), and `show a1b` read
    `no note found` (measured 2026-09-24: 17 comment lines in one adopter's
    tree cited ids no verb could follow). A tail on a `-` boundary expands
    when exactly one id ends in it; a shared tail lists every candidate and
    writes nothing."""
    a = store.add(tmp_path, id="20260101-000000-000001-abc", kind="task",
                  target={"type": "item", "name": "x"}, body="first abc", status="open")
    store.add(tmp_path, id="20260101-000000-000002-abc", kind="task",
              target={"type": "item", "name": "y"}, body="second abc", status="open")
    c = store.add(tmp_path, id="20260101-000000-000003-def", kind="task",
                  target={"type": "item", "name": "z"}, body="only def", status="open")

    for tail in ("def", "000003-def", "…def", "...def", c.id):
        assert run("show", tail, "--json", store_dir=tmp_path) == 0, tail
        assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [c.id], tail
    # As symbion prints a tail ("rows end in -abc"), and past a `--`: `show
    # -def` printed nothing and exited 0 (the user, 2026-10-09).
    for argv in (["-def"], ["-000003-def"], ["--", "def"], ["--", "-def"]):
        assert run("show", *argv, "--json", store_dir=tmp_path) == 0, argv
        assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [c.id], argv
    assert run("list", "--id", "000001-abc", "--json", store_dir=tmp_path) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [a.id]
    assert run("show", "ef", store_dir=tmp_path) == 1, "a tail ends on a `-` boundary"
    assert "no entry found: ef" in capsys.readouterr().err

    assert run("resolve", "abc", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "ambiguous id 'abc': 2 entries end in -abc; give more of it", err
    assert "20260101-000000-000001-abc" in err and "second abc" in err
    assert len(store.load(tmp_path)) == 3, "an ambiguous tail writes nothing"

    assert run("resolve", "def", store_dir=tmp_path) == 0
    assert run("supersede", "000001-abc", "--body", "b2", store_dir=tmp_path) == 0
    heads = {n.target.name: n for n in store.heads(store.load(tmp_path))}
    assert heads["z"].status == "resolved" and heads["x"].body == "b2"


def test_reconcile_json_is_the_report_alone_on_stdout(tmp_path, capsys):
    """SKILL.md documented `arc reconcile --json` for 20 days before the flag
    existed. stdout must still parse when a write runs: the applied line goes
    to stderr."""
    aid = _seeded_reconcile_arc(tmp_path)
    capsys.readouterr()
    assert run("arc", "reconcile", aid, "--json", "--resolve-stale", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    rows = json.loads(out)
    assert {(r["target_name"], r["status"]) for r in rows} == \
        {("live1", "live"), ("stale1", "stale")}
    assert set(rows[0]) == {"id", "target_type", "target_name", "status",
                            "suggestion", "needs_result"}
    assert "applied: 0 re-targeted and 0 re-pointed (renamed, whole store), " \
        "1 resolved (stale)" in err


def test_the_arc_flag_takes_both_spellings_on_every_verb(tmp_path, capsys):
    """add and supersede spelled it --arc-id and list spelled it --arc, so
    `list --arc-id` was refused as unrecognized (2026-09-24). add's --arc
    worked only as argparse's prefix abbreviation of --arc-id."""
    store.ensure_store(tmp_path)
    run("arc", "create", "--name", "A", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    assert run("add", "--kind", "task", "--type", "item", "--name", "x", "--arc", aid,
               store_dir=tmp_path) == 0
    run("add", "--kind", "task", "--type", "item", "--name", "y", store_dir=tmp_path)
    other = capsys.readouterr().out.split()[-1]
    assert run("supersede", other, "--arc", aid, store_dir=tmp_path) == 0
    capsys.readouterr()
    for flag in ("--arc", "--arc-id"):
        assert run("list", flag, aid, "--json", store_dir=tmp_path) == 0
        assert {r["target"]["name"] for r in json.loads(capsys.readouterr().out)} == {"x", "y"}


def test_context_refuses_an_unknown_target_type(tmp_path, capsys):
    """`context --target fiel:a.py` printed "0 notes" and exited 0: a typo read
    as a clean target. test_context_names_its_count_and_what_it_is_for holds
    the other direction, a known type with no notes."""
    _declare(tmp_path, _VOCAB)
    assert run("context", "--target", "fiel:a.py", store_dir=tmp_path) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "invalid type 'fiel' (choose from arc, commit, doc, file, item, project)" in err
    assert run("context", "--target", "file:a.py", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "0 entries for file:a.py\n"


def test_list_kind_filters_on_a_declared_label(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\nnote = {}\n')
    run("add", "--kind", "anomaly", "--type", "project", store_dir=tmp_path)
    run("add", "--kind", "note", "--type", "project", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--kind", "anomaly", "--json", store_dir=tmp_path) == 0
    assert [r["kind"] for r in json.loads(capsys.readouterr().out)] == ["anomaly"]


def test_resolve_result_closes_a_prediction_and_its_absence_is_refused(tmp_path, capsys):
    _declare(tmp_path, PREREG)
    run("add", "--kind", "prediction", "--type", "project", "--checked", "the sweep",
        "--body", "falsified if the control shows it", store_dir=tmp_path)
    pid = capsys.readouterr().out.strip()
    assert run("resolve", pid, store_dir=tmp_path) == 1
    assert "--result" in capsys.readouterr().err
    assert store.read_status(store.heads(store.load(tmp_path))[0]) == "open"
    assert run("resolve", pid, "--result", "HELD", store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.status, head.result) == ("resolved", "HELD")


def test_a_new_tag_near_an_existing_one_names_it(tmp_path, capsys):
    """Near-duplicate tags split one subject silently: `list --tag
    flaky-test` missed 5 rows tagged `flaky-tests` (measured 2026-09-26:
    such pairs in two adopter stores). A write that brings a tag no other
    head carries names the existing tag it nearly is: case, `_`/`-`, a
    trailing s."""
    for i in range(2):
        run("add", "--kind", "note", "--type", "item", "--name", "a", "--tag", "flaky-test",
            "--body", f"b{i}", store_dir=tmp_path)
    capsys.readouterr()

    def err(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        return _sans_pending(capsys.readouterr().err)

    want = ("note: new tag 'flaky-tests'; this store has 'flaky-test' (2): if they mean "
            "the same, reuse it\n")
    assert err("add", "--kind", "note", "--type", "item", "--name", "b",
               "--tag", "flaky-tests") == want
    assert "this store has 'flaky-test' (2)" in err(
        "add", "--kind", "note", "--type", "item", "--name", "c", "--tag", "Flaky_Test")
    assert err("add", "--kind", "note", "--type", "item", "--name", "d",
               "--tag", "flaky-test", "--tag", "unrelated") == ""
    nid = store.heads(store.load(tmp_path))[0].id
    assert "new tag 'flaky-tests'" not in err("supersede", nid, "--add-tag", "flaky-tests"), \
        "a tag another head carries is not new"
    assert "new tag 'FLAKY-TEST'" in err("supersede", nid, "--add-tag", "FLAKY-TEST")


def test_list_json_names_the_matches_it_hides_on_stderr(tmp_path, capsys):
    """A body that replaced a claim left `list --grep <claim> --json` at
    `[]`, exit 0, empty stderr, while the text form said `+1 superseded
    (--all)` (measured 2026-09-26: 111 of 149 adopter resolves dropped the
    claim's words from the head). stdout stays the bare array."""
    run("add", "--kind", "question", "--type", "item", "--name", "x",
        "--body", "should alpha run?", store_dir=tmp_path)
    qid = capsys.readouterr().out.strip()
    run("supersede", qid, "--status", "resolved", "--body", "answered: no", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--grep", "alpha", "--json", store_dir=tmp_path) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == [] and \
        out.err == "note: not in this array: 1 entry matches only in an older version (--all)\n", out.err
    run("list", "--grep", "answered", "--json", store_dir=tmp_path)
    assert capsys.readouterr().err == "", "nothing hidden, nothing said"
    run("list", "--json", store_dir=tmp_path)
    assert capsys.readouterr().err == "", "an unfiltered array is heads by contract"
    run("list", "--grep", "alpha", "--all", "--json", store_dir=tmp_path)
    out = capsys.readouterr()
    assert len(json.loads(out.out)) == 1 and out.err == "", "--all hides nothing"
    run("add", "--kind", "note", "--type", "item", "--name", "y", "--tag", "t",
        store_dir=tmp_path)
    run("supersede", capsys.readouterr().out.strip(), "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--tag", "t", "--json", store_dir=tmp_path)
    out = capsys.readouterr()
    assert len(json.loads(out.out)) == 1 and out.err == "", "its head is in the array"


def test_a_null_word_is_not_a_verdict(tmp_path, capsys):
    """`resolve --result None` closed a pre-registration with the string
    "None" as its verdict, and open predictions registered with `--result
    None` (measured 2026-09-26: 3 of 11 in one adopter store) would pass the
    guard on a bare `resolve`. The words are refused; an inherited one is
    dropped on the next supersede, as any field a row cannot hold is."""
    _declare(tmp_path, PREREG)
    for flag, word in (("--result", "None"), ("--checked", "null")):
        argv = ["add", "--kind", "prediction", "--type", "project", "--checked", "c", flag, word]
        assert run(*argv, store_dir=tmp_path) == 1
        assert "omit the flag" in capsys.readouterr().err
    assert store.load(tmp_path) == []
    legacy = store.note_from_dict({"id": store.new_id(), "kind": "prediction",
                                   "created_at": "2026-09-01T00:00:00Z", "status": "open",
                                   "target": {"type": "project", "name": None},
                                   "checked": "c", "result": "None"},
                                  kinds=K.read_kinds(tmp_path))
    store._append_note_unlocked(tmp_path, legacy)
    assert run("resolve", legacy.id, store_dir=tmp_path) == 1, "None is no verdict"
    assert "--result" in capsys.readouterr().err
    assert run("supersede", legacy.id, "--append", "--body", "amended", store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.status, head.result, head.checked) == ("open", None, "c")


def test_resolve_help_names_no_label(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli._build_parser(frozenset(), frozenset(), frozenset(), tmp_path,
                          K.DEFAULT_KINDS).parse_args(["resolve", "--help"])
    out = capsys.readouterr().out
    assert "open entry" in out and "bug/task" not in out


@pytest.mark.parametrize("argv, names", [
    ([], ["summary", "show ID", "commit"]),
    (["arc"], ["todo", "create"]),
])
def test_a_bare_verb_prints_its_help_not_an_internal_name(tmp_path, capsys, argv, names):
    """Bare `symbion` said `the following arguments are required: cmd`: the
    parser's own field name, and no way forward."""
    assert run(*argv, store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert "required" not in err and all(n in err for n in names), err


@pytest.mark.parametrize("alias, status", [("--open", "open"), ("--resolved", "resolved"),
                                           ("--closed", "resolved")])
def test_list_takes_an_obvious_status_flag_as_the_status(alias, status, tmp_path, capsys):
    """`list --open` was the guess agents made most, in three projects (field,
    2026-09-21..29); the hint answered it, and the next call was the same list.
    It reads as `--status open` now, and its two opposites as `resolved`."""
    run("add", "task", "--target", "item:a", "--body", "stays open", store_dir=tmp_path)
    run("add", "task", "--target", "item:b", "--body", "gets done", store_dir=tmp_path)
    done = capsys.readouterr().out.split()[-1]
    run("resolve", done, store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--status", status, "--json", store_dir=tmp_path)
    want = capsys.readouterr().out
    assert len(json.loads(want)) == 1
    assert run("list", alias, "--json", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == want


def test_the_status_aliases_stay_out_of_list_help(tmp_path, capsys):
    """`-h` is what agents read: it teaches one form, `--status`."""
    run("list", "-h", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "--status" in out and not any(f in out for f in ("--open", "--resolved", "--closed"))


def test_supersede_takes_append_file_and_prepend_file(tmp_path, capsys, monkeypatch):
    """The -file twins of --append and --prepend, guessed from --body-file in
    fresh-agent runs and in demo sessions (2026-10-14). The hint
    that named `--append --body-file` went to stderr, which one agent
    dropped. Each takes a path or `-`; beside a body or the other mode it is
    refused, as --body-file is."""
    nid = _add(tmp_path, capsys, "note", "--target", "item:x", "--body", "lead")
    _commit(tmp_path, capsys)
    (tmp_path / "below.md").write_text("below\n")
    assert run("supersede", nid, "--append-file", str(tmp_path / "below.md"),
               store_dir=tmp_path) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("above\n"))
    assert run("supersede", nid, "--prepend-file", "-", store_dir=tmp_path) == 0
    capsys.readouterr()
    run("show", nid, "--json", store_dir=tmp_path)
    body = json.loads(capsys.readouterr().out)[0]["body"]
    assert body.startswith("above") and "lead" in body and body.rstrip().endswith("below"), body
    f = str(tmp_path / "below.md")
    for argv, says in ((["--append-file", f, "--body", "x"], "not allowed with"),
                       (["--append-file", f, "--prepend-file", f], "not allowed with"),
                       (["--append-file", f, "--prepend"], "pick one"),
                       (["--prepend", "--append-file", f], "pick one")):
        assert run("supersede", nid, *argv, store_dir=tmp_path) != 0, argv
        assert says in capsys.readouterr().err, argv


@pytest.mark.parametrize("argv, says", [
    (["delete", "x"], "append-only: `resolve ID` closes"),
    (["rm", "x"], "`undo ID` takes back your last write before a commit"),
    (["edit", "x"], "`supersede ID --body"),
    (["append", "x"], "`supersede ID --append --body"),   # dogfood, 2026-10-01
    (["close", "x"], "`resolve ID`"),
    (["search", "x"], "`list --grep TEXT`"),
    (["due", "--help"], "`list --overdue`"),         # an adopter's agent, 2026-09-25
    (["add", "bug", "--target", "item:x", "login times out"], 'text goes in --body "…"'),
    # three runs taught three rules (dogfood, 2026-10-09): one names them all
    (["add", "task", "login times out", "--tag", "x"],
     'text goes in --body "…"; a target is required: --target TYPE:NAME'),
    (["add", "task", "login times out", "--target", "project:login"],
     'text goes in --body "…"; a project target takes no name'),
    # quoted text fills KIND: its quotes are there, its --body is not
    (["add", "--target", "item:x", "login times out"],
     "'login times out' was read as KIND: text goes in --body \"…\"; "
     "a kind is required: `add KIND`"),
    (["add", "--target", "item:x", "--kind", "bug", "login times out"],
     "'login times out' was read as KIND: text goes in --body \"…\"\n"),
    # an unquoted value that split (dogfood, 2026-09-27: the hint above sent
    # its author to --body when the target lacked quotes)
    (["add", "task", "--target", "item:leak", "list", "coverage"],
     'a value with spaces needs quotes: --target "item:a b"'),
    # the split value's next word fills the optional KIND, whose error listed
    # the kinds (dogfood, 2026-09-29: with --kind already given)
    (["add", "--kind", "note", "--target", "project", "--ref", "item:login", "page", "timeout"],
     'a value with spaces needs quotes: --target "item:a b"'),
    (["add", "--kind", "note", "--target", "project", "--ref", "item:login", "page"],
     'a value with spaces needs quotes: --target "item:a b"'),
    (["add", "--target", "item:login", "page", "timeout"],
     'a value with spaces needs quotes: --target "item:a b"'),
    (["add", "bgu", "--target", "item:x"], "invalid kind 'bgu' (choose from"),
    # said only that --name is required (dogfood, 2026-10-09)
    (["arc", "create", "Adoption"],
     "required: --name; 'Adoption' went unused: it goes in --name \"Adoption\""),
    (["arc", "create", "My", "arc", "--scope", "item"],
     "'My arc' went unused: it goes in --name \"My arc\""),
    (["rename", "a", "b"], "required: --type\n"),     # its positionals took them
    # a verb typed as a flag printed 60 lines of help, none naming it (2026-10-13)
    (["--init"], "--init is no flag; did you mean `symbion init`?"),
    (["arc", "--create"], "--create is no flag; did you mean `symbion arc create`?"),
    # each named a flag the verb lacks, or none (dogfood, 2026-10-14)
    (["list", "--text", "x"], "--text: use --grep"),
    (["list", "--body", "x"], "--body: use --grep"),
    (["add", "note", "--target", "project", "-F", "x"], "-F: use --body-file"),
    (["commit", "-F", "msg.txt"], '-F: use -m "$(cat FILE)"'),
    (["add", "--type", "bug", "--body", "x"], "'bug' is a kind: `add bug --target TYPE:NAME`"),
    (["list", "--type", "task"], "'task' is a kind: `list --kind task`"),
    (["add", "bug", "--target", "login page"], "--target 'item:login page'"),
    # a mode flag read as taking the text (fresh agents, 2026-10-14: four in a day)
    (["supersede", "x", "--append", "more", "text"],
     'unrecognized arguments: more text; text goes in --append --body "…"'),
    (["supersede", "x", "--prepend", "new lead"], 'text goes in --prepend --body "…"'),
    (["supersede", "x", "--ref", "item:login", "page"], 'needs quotes: --ref "item:a b"'),
    (["supersede", "x", "--target", "item:login", "page"], 'needs quotes: --target "item:a b"'),
    # reported as `symbion list: error: argument --id` (2026-10-09 review)
    (["show"], "symbion show: error: the following arguments are required: id"),
    (["show", "--json"], "symbion show: error: the following arguments are required: id"),
])
def test_a_first_guess_that_fails_names_the_command_that_works(tmp_path, capsys, argv, says):
    """A first-day user's guesses, each met by an error that named no way
    forward (newcomer walk-through, 2026-09-26): an invalid choice, a bare
    `unrecognized arguments`, `invalid type 'bug'`, a TYPE list for a
    target typed without its TYPE."""
    assert run(*argv, store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert says in err, err


def test_a_stray_word_beside_a_body_file_names_no_body(tmp_path, capsys):
    """The text came by --body-file, and the hint still sent it to --body
    (dogfood, 2026-10-15). Both directions: with no body it still does."""
    body = tmp_path / "body.md"
    body.write_text("text")
    for argv, hinted in ((["--body-file", str(body)], False),
                         (["--append-file", str(body)], False), ([], True)):
        verb = ["supersede", "x"] if "--append-file" in argv else ["add", "note"]
        assert run(*verb, "--target", "item:login", "page", *argv, store_dir=tmp_path) == 2
        err = capsys.readouterr().err
        assert ("--body \"…\"" in err) is hinted, (argv, err)
        assert 'needs quotes: --target "item:a b"' in err


@pytest.mark.parametrize("argv, flag", [
    (["context", "--target", "item:x", "--open"], "--status"),
    (["show", "x", "--search", "y"], "--grep"),
])
def test_a_flag_hint_names_only_a_flag_the_verb_takes(tmp_path, capsys, argv, flag):
    """`list --text x` said `use --body`, and `list` takes no --body: the next
    try failed too (dogfood, 2026-10-14). A hint the verb cannot take is left out."""
    assert run(*argv, store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err and f"use {flag}" not in err, err


def test_an_unknown_flags_value_is_not_read_as_a_split_value(tmp_path, capsys):
    """`add … -m x`, typed from `git commit` habit, was told "a value with
    spaces needs quotes": `x` is the value of `-m`, not half of another."""
    assert run("add", "note", "--target", "item:x", "-m", "x", store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert "-m: use --body" in err and "spaces" not in err, err


@pytest.mark.parametrize("argv", [
    ["add", "check", "--target", "project", "--measure", "a=1", "b=2", "c=3"],
    ["add", "--kind", "check", "--target", "project", "--measure", "a=1", "b=2"],  # b=2 fills KIND
    ["resolve", "x", "--measure", "a=1", "b=2"],
    ["supersede", "x", "--measure", "a=1", "b=2"],
])
def test_several_pairs_after_one_measure_name_the_flag_per_pair(tmp_path, capsys, argv):
    """`--measure a=1 b=2` was told "a value with spaces needs quotes", and
    its author looked for a quoting error that did not exist (dogfood,
    2026-10-08): `--measure` takes one pair per flag."""
    assert run(*argv, store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert "each KEY=VALUE needs its own --measure: --measure b=2" in err, err
    assert "needs quotes" not in err, err


def test_a_body_that_cites_no_row_says_so(tmp_path, capsys):
    """Bodies cited rows that did not exist yet, as placeholders
    (`20990101-123xxx`, a full id of zeros), four times in five days, and
    each was found only by reading the row back; a tail's too (`588877-xxx`,
    2026-10-16). Never a refusal."""
    run("add", "task", "--target", "project", "--body", "x", store_dir=tmp_path)
    real = capsys.readouterr().out.strip()
    bad = ("20990101-123xxx", "20990102-000000-000000-000", "123456-abc", "588877-xxx")
    body = (f"see {real} and …{real[-10:]}, not {bad[0]}, {bad[1]}, `{bad[2]}` or {bad[3]}; "
            f"2026-09-27 is a date, {real[:15]} a prefix, 20990101-000000Z a stamp, "
            f"20991009-131946 and 20991009-1319 file stamps and 50000000-byte a size")
    assert run("add", "note", "--target", "project", "--body", body, store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    # One line: a retro that cited four rows of another store printed four
    # (dogfood, 2026-10-11), and cross-store cites are often asked for.
    assert [ln for ln in err.splitlines() if "matches" in ln] == \
        [f"note: no entry in this store matches {', '.join(bad)} (cited in the body)"], err
    assert run("resolve", real, "--body", "see 654321-fed", store_dir=tmp_path) == 0
    assert "matches 654321-fed" in capsys.readouterr().err
    assert run("supersede", real, "--append", "--body", "and 654321-fee",
               store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "matches 654321-fee" in err and "654321-fed" not in err, "only the new text"


def test_a_ref_to_an_item_no_row_names_says_so(tmp_path, capsys):
    """A `--ref item:NAME` meant for an existing followup named
    nothing, and was stored with no word (dogfood, 2026-10-10): `item` has
    no catalog to miss. A ref to a known item, a target and an inherited
    ref stay quiet; a new one is a note, never a refusal. The note names the
    targets that contain the name, as `list --name` does: a short ref
    started an item beside the longer one it meant (dogfood, 2026-10-13)."""
    run("add", "task", "--target", "item:dwell", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("add", "note", "--target", "project", "--body", "y",
               "--ref", "item:dwel", "--ref", "item:dwell", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    new = [ln for ln in err.splitlines() if "starts a new item" in ln]
    assert new == ["note: no other entry names item:dwel, so this ref starts a new item; "
                   "these contain it: item:dwell (1 entry)"], err
    for argv in (["supersede", out.strip(), "--append", "--body", "z"],
                 ["add", "task", "--target", "item:fresh"]):
        assert run(*argv, store_dir=tmp_path) == 0
        assert "starts a new item" not in capsys.readouterr().err, argv
    assert run("resolve", nid, "--ref", "item:typo", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "names item:typo" in err and "contain it" not in err, err
    assert run("supersede", nid, "--add-ref", "item:typo2", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "names item:typo2" in err and "item:typo," not in err, err


def test_a_new_item_a_letter_off_a_stored_one_says_did_you_mean(tmp_path, capsys):
    """`add --target item:NAME` one letter off a stored item forked its thread
    at exit 0 (2026-10-13, 2026-10-15): `item` has no catalog to miss. A new
    target is named only beside a near name, as a catalog miss is: a new item
    is how a task starts. Names that differ only in digits are a series."""
    for name in ("parser timeout", "nightly run 2099-01-05"):
        run("add", "task", "--target", f"item:{name}", store_dir=tmp_path)
    capsys.readouterr()
    assert run("add", "task", "--target", "item:Parser timout", store_dir=tmp_path) == 0
    assert ("note: no other entry names item:Parser timout; did you mean 'parser timeout'?\n"
            in capsys.readouterr().err)
    for name in ("nightly run 2099-01-07", "a fresh thread", "parser timeout"):
        assert run("add", "task", "--target", f"item:{name}", store_dir=tmp_path) == 0
        assert "did you mean" not in capsys.readouterr().err, name
    assert run("add", "note", "--target", "project", "--ref", "item:parser timeot",
               store_dir=tmp_path) == 0
    assert ("so this ref starts a new item; did you mean 'parser timeout', 'Parser timout'?"
            in capsys.readouterr().err)
    assert run("add", "task", "--target", "item:Nightly run 2099-01-07", store_dir=tmp_path) == 0
    assert "did you mean 'nightly run 2099-01-07'?" in capsys.readouterr().err, "case is no series"


def test_an_add_identical_to_a_stored_entry_writes_nothing(tmp_path, capsys, monkeypatch):
    """A shell command whose `$(...)` failed after its add ran was retried,
    and the store held the entry twice (2026-10-15); `undo` reaches only the
    last write. An add whose every field equals an uncommitted entry's prints
    that entry's id and writes nothing, so a retry is safe. An external
    stamp's time is no field. A batch holding one refuses whole, naming the
    line. A committed twin is a new run."""
    argv = ["add", "note", "--target", "item:x", "--body", "same", "--tag", "t"]
    assert run(*argv, store_dir=tmp_path) == 0
    first = capsys.readouterr().out.strip()
    assert run(*argv, store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert out == f"{first}\n"
    assert f"note: an identical entry is already stored: {first}; nothing written" in err
    assert len(store.load(tmp_path)) == 1
    assert run(*argv[:-1], "u", store_dir=tmp_path) == 0, "one field apart is new"
    ext = ["add", "check", "--target", "project", "--checked", "c", "--result", "r",
           "--external"]
    for sec in (5, 16):                  # a retry seconds later: its stamp's time differs
        with monkeypatch.context() as m:
            m.setattr(store, "_now_iso", lambda: f"2099-01-01T00:00:{sec}+00:00")
            assert run(*ext, store_dir=tmp_path) == 0
    assert len(store.load(tmp_path)) == 3
    rows = tmp_path / "entries.jsonl"
    rows.write_text("".join(json.dumps(r) + "\n" for r in (
        {"kind": "note", "target": {"type": "item", "name": "x"}, "body": "new"},
        {"kind": "note", "target": {"type": "item", "name": "x"}, "body": "same",
         "tags": ["t"]})))
    capsys.readouterr()
    assert run("add", "--from-json", str(rows), store_dir=tmp_path) == 1
    assert (f"add: --from-json line 2: identical to stored entry {first}; nothing was written\n"
            in capsys.readouterr().err)
    assert len(store.load(tmp_path)) == 3
    assert run("commit", "--all", store_dir=tmp_path) == 0
    assert run(*argv, store_dir=tmp_path) == 0
    assert capsys.readouterr().out.strip() not in ("", first), "a committed twin is a new run"
    assert len(store.load(tmp_path)) == 4


def test_a_verdict_row_with_no_checked_says_so(tmp_path, capsys):
    """A verdict row with no `--checked` still reads `current` or `behind N`,
    as if something ran (2026-09-26: a quarter of the verdict heads in two
    adopter stores had neither field). A note, never a refusal; a row that
    names its run, and a kind with no verdict, stay quiet."""
    for kind in ("check", "prediction"):
        assert run("add", kind, "--target", "project", "--body", "x", store_dir=tmp_path) == 0
        err = capsys.readouterr().err
        assert f"note: no --checked on this {kind} entry" in err, err
    for argv in (["add", "check", "--target", "project", "--checked", "pytest", "--result", "ok"],
                 ["add", "task", "--target", "project", "--body", "x"]):
        assert run(*argv, store_dir=tmp_path) == 0
        assert "no --checked" not in capsys.readouterr().err, argv


def test_a_parked_row_with_no_due_says_its_writer_will_not_see_it(tmp_path, capsys, monkeypatch):
    """A parked row prints at session start only when due soon or written
    by someone else than the reader: most of one store's open ideas were
    the agent's own, undated, and no session of its saw them again
    (2026-10-12). Filed as another author, or with a date, it reaches one."""
    monkeypatch.setenv("SYMBION_AUTHOR", "agent")
    assert run("add", "idea", "--target", "project", "--body", "x", store_dir=tmp_path) == 0
    assert "note: no --due on this idea entry" in capsys.readouterr().err
    for extra in (["--due", "2099-01-01"], ["--author", "human"], []):
        kind = "task" if not extra else "idea"
        assert run("add", kind, "--target", "project", "--body", "x", *extra,
                   store_dir=tmp_path) == 0
        assert "no --due" not in capsys.readouterr().err, extra


def test_a_write_names_what_others_wrote_since_the_writers_last(tmp_path, capsys, monkeypatch):
    """A user's reply through the GUI mid-session reached the agent only by
    commit's per-author count, four times (2026-10-10, 2026-10-11). A write
    names each row someone else wrote or amended since the writer's previous
    one, as the summary prints it. A first write says nothing: the session
    start covered that. A row this write touched is not news."""
    clock = (f"2026-10-12T10:00:{s:02d}+00:00" for s in range(60))
    monkeypatch.setattr(store, "_now_iso", lambda: next(clock))
    monkeypatch.setenv("SYMBION_AUTHOR", "agent")
    run("add", "question", "--target", "project", "--body", "ship it? " + "why " * 40,
        store_dir=tmp_path)
    q = capsys.readouterr().out.strip()
    run("add", "bug", "--target", "item:x", "--body", "x broke", "--author", "human",
        store_dir=tmp_path)
    bug = capsys.readouterr().out.strip()
    run("supersede", q, "--append", "--body", "yes, ship it", "--author", "human",
        store_dir=tmp_path)
    capsys.readouterr()
    run("add", "task", "--target", "project", "--body", "y", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert f"  waits on you, from human [bug] item:x  x broke  {bug}" in err, err
    assert f"  waits on you, last by human [question, +1 amendment] project:  ship it? why" in err, err
    assert "\n    human added " in err and ": yes, ship it\n" in err, err
    run("add", "task", "--target", "project", "--body", "z", store_dir=tmp_path)
    assert "previous write" not in capsys.readouterr().err, "said once"
    run("resolve", bug, "--body", "fixed", "--author", "human", store_dir=tmp_path)
    capsys.readouterr()
    run("supersede", bug, "--add-tag", "seen", store_dir=tmp_path)
    assert "previous write" not in capsys.readouterr().err, "the entry this write touched"


def test_the_session_start_text_fits_the_hook_limit(tmp_path, capsys, monkeypatch):
    """Claude Code saves a hook's text past 10,000 characters to a file and
    gives the agent only its path and first 2,000 characters, and the handoff
    prints last. The hook's text drops rows, least needed first, each counted
    into its block's `+N more` line, until it fits, and a line says so."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("SYMBION_AUTHOR", "agent")
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    kinds = [("task", {})] * 60 + [("bug", {"due": soon})] * 15 + \
        [("task", {"tags": ["priority"]})] * 15 + [("task", {"author": "human"})] * 15
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(
        json.dumps({"kind": k, "target": {"type": "item", "name": f"t{i}"},
                    "body": f"{k} {i}: " + "words " * 40, **extra}) + "\n"
        for i, (k, extra) in enumerate(kinds))))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    for i in range(10):
        run("arc", "create", "--name", f"an arc with a name of some length, number {i}",
            store_dir=tmp_path)
    for n in store.load(tmp_path)[:5]:              # the agent's, closed by the user
        run("resolve", n.id, "--author", "human", "--body", "done", store_dir=tmp_path)
    assert run("commit", "--all", "--wrap", "-m", "Next: pick up t1\n\nthe handoff paragraph, "
               + "about what the session did " * 15, store_dir=tmp_path) == 0
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    whole = capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    run("summary", "--hook", store_dir=tmp_path)
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert len(whole.encode("utf-16-le")) // 2 > summ.HOOK_CHARS, "the store must be over it"
    assert len(text.encode("utf-16-le")) // 2 <= summ.HOOK_CHARS
    assert "the handoff paragraph" in text and "more cut to fit the session-start hook" in text
    d = summ.summary(tmp_path, api.resolve(str(tmp_path)).cfg, reader="agent")
    fitted = summ.fit(d)
    for block in summ._TRIM:
        assert len(fitted[block]) + fitted[f"{block}_elided"] == \
            len(d[block]) + d[f"{block}_elided"], block
    assert fitted["fitted"] == sum(len(d[b]) - len(fitted[b]) for b in summ._TRIM)
    assert len(fitted["waiting"]) == len(d["waiting"]) and fitted["due"], \
        "the open list goes before the due rows"
    small = summ.summary(tmp_path, api.resolve(str(tmp_path)).cfg, reader="agent") | \
        {"heads": [], "due": [], "priority": [], "people": []}
    assert summ.fit(small) == small, "under the limit, nothing changes"


def test_a_sessions_first_write_skips_what_its_start_summary_printed(tmp_path, capsys,
                                                                      monkeypatch):
    """The floor was the writer's previous write, so a session's first write
    repeated, as news, the rows its session-start summary had printed, and
    the agent went on without a word (2026-10-13). A row written after that
    summary is still news."""
    clock = (f"2026-10-13T10:00:{s:02d}+00:00" for s in range(60))
    monkeypatch.setattr(store, "_now_iso", lambda: next(clock))
    monkeypatch.setenv("SYMBION_AUTHOR", "agent")
    run("add", "task", "--target", "project", "--body", "earlier", store_dir=tmp_path)
    run("add", "bug", "--target", "item:x", "--body", "x broke", "--author", "human",
        store_dir=tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "s1"}')))
    run("summary", "--hook", store_dir=tmp_path)
    assert "x broke" in capsys.readouterr().out
    run("add", "task", "--target", "project", "--body", "y", store_dir=tmp_path)
    assert "previous write" not in capsys.readouterr().err, "session start printed it"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")          # the user, at a terminal
    run("add", "bug", "--target", "item:z", "--body", "z broke", "--author", "human",
        store_dir=tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    capsys.readouterr()
    run("add", "task", "--target", "project", "--body", "w", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert "z broke" in err and "x broke" not in err, err
    run("add", "note", "--target", "project", "--body", "the user's words", "--author", "human",
        store_dir=tmp_path)
    capsys.readouterr()
    run("add", "task", "--target", "project", "--body", "v", store_dir=tmp_path)
    assert "previous write" not in capsys.readouterr().err, \
        "an entry this session wrote in the user's words is not news to it"


def test_the_news_floor_is_the_writers_row_above_this_write(tmp_path, capsys, monkeypatch):
    """The floor was the writer's last row stamped before the command began,
    and a stamp holds whole seconds. A write in the second the next command
    began did not count, and commands run in parallel all began before any of
    them wrote: each printed the same news (2026-10-15).
    By place in the file instead: the writer's last row above this write."""
    monkeypatch.setattr(store, "_now_iso", lambda: "2026-10-16T10:00:00+00:00")
    monkeypatch.setenv("SYMBION_AUTHOR", "agent")

    def add(body, *extra):
        run("add", "task", "--target", "project", "--body", body, *extra, store_dir=tmp_path)
        return capsys.readouterr()
    add("first")
    bug = add("x broke", "--author", "human").out.strip()
    err = add("all in one second").err
    assert f"from human [task] project:  x broke  {bug}" in err, err
    assert "previous write" not in add("still that second").err, "said once"
    add("y broke", "--author", "human")
    out = add("mine to edit")
    assert "y broke" in out.err, out.err
    run("supersede", out.out.strip(), "--body", "edited in place", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert "in place" in err and "previous write" not in err, \
        "an edit in place keeps its line and time: it is still the write after y"
    add("v broke", "--author", "human")
    run("supersede", bug, "--add-tag", "seen", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert "v broke" in err and "x broke" not in err, err

    clock = (f"2026-10-16T10:00:{s:02d}+00:00" for s in range(1, 60))
    monkeypatch.setattr(store, "_now_iso", lambda: next(clock))
    add("z broke", "--author", "human")
    real = api.add_fields

    def parallel(ctx, fields):          # another command's write lands first
        monkeypatch.setattr(api, "add_fields", real)
        real(ctx, [dict(fields[0], body="the other command's")])
        return real(ctx, fields)
    monkeypatch.setattr(api, "add_fields", parallel)
    assert "previous write" not in add("this command's").err, \
        "the other command's row is above this one, and its news was its own"

    def after(ctx, fields):             # someone else writes after this write
        monkeypatch.setattr(api, "add_fields", real)
        mine = real(ctx, fields)
        real(ctx, [dict(fields[0], body="w broke", author="human")])
        return mine
    monkeypatch.setattr(api, "add_fields", after)
    assert "w broke" not in add("before w").err, "news is what landed before this write"
    assert "w broke" in add("after w").err


def test_a_write_to_an_old_id_names_the_row_it_changed(tmp_path, capsys):
    """`supersede b3d --body again` after b3d was superseded replaced the
    newer row's text, and `resolve` on a resolved row replaced its
    resolution; each printed only a new id (newcomer walk-through,
    2026-09-26). The fast-forward stays: it is what closes the race."""
    run("add", "bug", "--target", "item:x", "--body", "v1", store_dir=tmp_path)
    old = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("supersede", old, "--body", "v2", store_dir=tmp_path)
    tip = capsys.readouterr().out.strip()
    old = _earlier(tmp_path, capsys, tip)
    assert run("supersede", old, "--body", "v3", store_dir=tmp_path) == 0
    assert f"note: {old} was superseded by {tip}; this applies to {tip}" in \
        capsys.readouterr().err
    head = store.heads(store.load(tmp_path))[0]
    assert run("resolve", head.id, store_dir=tmp_path) == 0
    assert _sans_pending(capsys.readouterr().err) == "", "a live open head: nothing to say"
    head = store.heads(store.load(tmp_path))[0]
    rows = len(store.load(tmp_path))
    assert run("resolve", head.id, store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert f"note: {head.id} is already resolved" in err
    # It went on to say `edited in place`: with nothing to add, nothing is
    # written, and the id printed is the row's own (2026-10-09 review).
    assert "nothing to add, so nothing was written" in err and "in place" not in err, err
    assert out.strip() == head.id and len(store.load(tmp_path)) == rows
    assert run("resolve", head.id, "--body", "and why", store_dir=tmp_path) == 0
    assert "nothing to add" not in capsys.readouterr().err


def test_text_a_human_reads_says_what_it_means(tmp_path, capsys, monkeypatch):
    """From the newcomer walk-through (2026-09-26): `tags` on a store with
    none printed nothing; `2 notes uncommitted` read as unsaved; the
    summary's `before a write: load the symbion skill` line is for an agent,
    and a human at a terminal read it as noise."""
    run("add", "note", "--target", "project", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    run("tags", store_dir=tmp_path)
    assert capsys.readouterr().out == "no tags yet (add --tag NAME)\n"
    home = Path(os.environ["HOME"])
    (home / cli._SKILL_LINK).mkdir(parents=True)
    (home / cli._SKILL_LINK / "SKILL.md").write_text("x")
    run("summary", store_dir=tmp_path)
    piped = capsys.readouterr().out
    assert re.search(r"1 entry not yet in the store's git \(symbion --dir \S+ commit\)\n", piped), piped
    assert "load the symbion skill" in piped, "a pipe is how the hook and agents read it"
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("summary", store_dir=tmp_path)
    assert "load the symbion skill" not in capsys.readouterr().out
    p = cli._build_parser(frozenset({"item"}), frozenset(), frozenset(), tmp_path,
                          K.DEFAULT_KINDS)
    init = p._subparsers._group_actions[0].choices["init"]
    assert init.description and "~/.claude" in init.description


def test_a_checklist_takes_one_try(tmp_path, capsys):
    """A newcomer needed four tries for a three-step checklist (2026-09-26):
    `arc create` required a --scope nothing explained, `arc seed` required
    one though the arc had its own, and `seeded 3 new task(s)` printed no
    ids to tick. --scope defaults (mixed on create; on seed the arc's own
    when seedable, else item for --name); seed prints ids like add."""
    assert run("arc", "create", "--name", "Release", store_dir=tmp_path) == 0
    aid = capsys.readouterr().out.strip()
    assert store.load_arcs(tmp_path)[0].target_scope == "mixed"
    assert run("arc", "seed", aid, "--name", "changelog", "--name", "tag",
               store_dir=tmp_path) == 0
    out = capsys.readouterr()
    ids = out.out.split()
    assert out.err == "seeded 2 new task(s)\n", out.err
    assert [n.id for n in store.load(tmp_path)] == ids
    assert {n.target.name for n in store.load(tmp_path)} == {"changelog", "tag"}
    run("arc", "create", "--name", "Items", "--scope", "item", store_dir=tmp_path)
    iid = capsys.readouterr().out.strip()
    assert run("arc", "seed", iid, "--name", "x", store_dir=tmp_path) == 0
    assert run("arc", "seed", aid, store_dir=tmp_path) == 2, "no names, no seedable scope"
    assert "--scope" in capsys.readouterr().err
    p = cli._build_parser(frozenset({"item"}), frozenset({"item", "mixed", "project"}),
                          frozenset({"item"}), tmp_path, K.DEFAULT_KINDS)
    arc = p._subparsers._group_actions[0].choices["arc"]
    assert "`add task --arc ID" in " ".join(arc.format_help().split())


def test_session_start_names_open_rows_a_person_wrote(tmp_path, capsys, monkeypatch):
    """A person's rows were invisible to agents when their kind was parked:
    the user's `idea` in one adopter store went unseen by every session,
    and an agent's `symbion commit` swept it in without a word (measured
    2026-09-24). The summary names open rows, parked included, by anyone
    other than its reader, once each, capped; not on a terminal, where the
    reader is the person. The label is the row's, not the block's: a person's
    row the heads block prints carries `from <author>` there, or the
    attribution would depend on which side of the cap the row fell."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")
    run("add", "task", "--target", "item:mine", "--body", "an agent's", store_dir=tmp_path)
    run("add", "task", "--target", "item:t", "--body", "alice's task", "--author", "alice",
        store_dir=tmp_path)
    for i in range(4):
        run("add", "idea", "--target", f"item:i{i}", "--body", f"alice idea {i}",
            "--author", "alice", store_dir=tmp_path)
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    out = capsys.readouterr().out
    from_alice = [ln for ln in out.splitlines() if ln.startswith("  from alice [idea, parked]")]
    assert [ln.split("  ")[-2] for ln in from_alice] == \
        ["alice idea 3", "alice idea 2", "alice idea 1", "alice idea 0"], out
    assert from_alice[0].startswith("  from alice [idea, parked] item:i3"), from_alice[0]
    assert "more from others" not in out, "each waits on the reader, so each prints"
    assert out.count("alice's task") == 1, "an entry the heads block prints is not repeated"
    assert "  from alice [task] item:t" in out, \
        "attribution cannot depend on which side of the cap an entry fell: " + out
    assert "an agent's" in out and "from claude" not in out
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("summary", store_dir=tmp_path)
    assert "from alice" not in capsys.readouterr().out


def test_a_project_row_that_repeats_an_open_rows_first_line_names_it(tmp_path, capsys):
    """The open-rows notice skips `project`, every row's fallback target, so
    a revision there landed as a second open box beside its head (measured
    2026-09-24: same bold first line, 27 minutes apart). On `project` it
    names only the open rows whose first line the new row repeats."""
    def add(body):
        run("add", "task", "--target", "project", "--body", body, store_dir=tmp_path)
        c = capsys.readouterr()
        return c.out.strip(), c.err
    first, _ = add("**The cache never expires on restart.**\n\ndetail one")
    _, err = add("**The build is slow on a cold cache.**\n\nother")
    assert "open on project" not in err, "distinct first lines stay silent"
    _, err = add("Done.")
    _, err = add("Done.")
    assert "open on project" not in err, "a short first line is not a title"
    _, err = add("The cache never expires on RESTART.\n\nrevised detail")
    assert "note: 1 open on project: with this title; " in err, err
    # each row with its lead, so a duplicate shows without a `show` (2026-10-07)
    assert f"\n  {first}  The cache never expires on restart. detail one\n" in err, err
    # The measured pair: a bold title with its detail on the same line, then
    # the same title alone. The title is the bold lead, else the first sentence.
    lead, _ = add("**Handoffs keep symbion workarounds after the fix.** one handoff still says X.")
    _, err = add("**Handoffs keep symbion workarounds after the fix.**\n\nDone: a, b.")
    assert f"\n  {lead}  Handoffs keep symbion workarounds" in err, err


def test_context_on_a_commit_git_does_not_know_says_so(repo, tmp_path, capsys):
    """`context --commit deadbeef` read `0 notes` at exit 0 (doc claims
    audit, 2026-09-27). A rebased-away commit keeps its rows, so the name
    is still matched as typed; stderr says git does not know it."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    assert run("context", "--commit", "deadbeef", store_dir=store_dir) == 0
    assert "git does not know commit 'deadbeef'" in capsys.readouterr().err
    run("context", "--commit", "HEAD", store_dir=store_dir)
    assert capsys.readouterr().err == ""


def test_a_write_on_a_commit_git_does_not_know_says_so(repo, tmp_path, capsys):
    """`add --target commit:<typo>` stored the typed string with nothing
    said, where `context --commit` names the miss (2026-10-09 review)."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    note = "note: git does not know commit 'deadbeef' here; taken as typed"
    assert run("add", "note", "--target", "commit:deadbeef", store_dir=store_dir) == 0
    assert note in capsys.readouterr().err
    assert run("add", "note", "--target", "project", "--ref", "commit:deadbeef",
               store_dir=store_dir) == 0
    assert note in capsys.readouterr().err
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    for name in ("HEAD", head):
        assert run("add", "note", "--target", f"commit:{name}", store_dir=store_dir) == 0
        assert "git does not know" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["list", "--json"], ["context", "--json"],
                                  ["arc", "list", "--json"]])
def test_a_json_read_of_an_absent_store_says_so_on_stderr(tmp_path, capsys, argv):
    """A wrong --dir read as an empty store: `list --json` printed `[]` at
    exit 0 with empty stderr (doc claims audit, 2026-09-27). The shape and
    the exit stay, since a consumer parses them; stderr names the path.
    `summary --json` keeps its `store: null` gate and says nothing."""
    gone = tmp_path / "no-such-store"
    assert run(*argv, store_dir=gone) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) is not None
    assert f"symbion: no store at {gone}" in out.err, out.err
    if argv[0] == "context":
        # summary's gate: a client that reads only stdout and the exit code
        # showed "no notes" for a missing store (2026-10-03)
        assert json.loads(out.out)["store"] is None
    assert run("summary", "--json", store_dir=gone) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["store"] is None and out.err == ""


def test_an_arc_target_or_ref_names_an_arc(tmp_path, capsys):
    """`--arc-id` had to name an arc and `--target arc:nosuch` did not: the
    row was on nothing (doc claims audit, 2026-09-27)."""
    run("arc", "create", "--name", "Real", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    for argv in (["add", "note", "--target", "arc:nosuch", "--body", "x"],
                 ["add", "note", "--target", "project", "--ref", "arc:nosuch", "--body", "x"]):
        assert run(*argv, store_dir=tmp_path) == 1, argv
        assert "no arc 'nosuch'" in capsys.readouterr().err
    assert run("add", "note", "--target", f"arc:{aid}", "--ref", f"arc:{aid}",
               "--body", "x", store_dir=tmp_path) == 1, "a self-ref is still refused"
    capsys.readouterr()
    assert run("add", "note", "--target", "project", "--ref", f"arc:{aid}", "--body", "x",
               store_dir=tmp_path) == 0
    nid = capsys.readouterr().out.strip()
    assert run("add", "note", "--target", f"arc:{aid}", "--body", "x", store_dir=tmp_path) == 0
    assert run("supersede", nid, "--ref", "arc:nosuch", store_dir=tmp_path) == 1
    assert run("resolve", nid, "--ref", "arc:nosuch", store_dir=tmp_path) == 1
    assert len(store.load(tmp_path)) == 2


def test_an_open_pre_registration_names_its_registration_date(tmp_path, capsys):
    """14 of 14 open prediction heads in three adopter stores opened with
    "PRE-REGISTERED <date>" (measured 2026-09-26): an amendment is a new row,
    so the head's own date is not the registration, and nothing printed the
    root's. The session-start line names it; the body can lead with the claim."""
    _declare(tmp_path, PREREG)
    root = store.add(tmp_path, kind="prediction", target={"type": "item", "name": "p"},
                     status="open", checked="the sweep", body="falsified if X",
                     created_at="2026-09-20T12:00:00+00:00")
    run("supersede", root.id, "--append", "--body", "amended", store_dir=tmp_path)
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "item:p" in ln)
    assert line.startswith("  [prediction, registered 2026-09-20, +1 amendment] "
                           "item:p  falsified if X"), line


def test_the_summary_counts_the_amendments_its_line_cannot_show(tmp_path, capsys):
    """A digest prints a body's start, so `--append` never reaches it: a
    prediction's corrected registration time printed its typo until the row
    resolved (dogfood, 2026-09-30). An appended body counts; a rewrite (whose
    new start is printed), a tag change and a first body do not."""
    def add(name, body):
        run("add", "task", "--target", f"item:{name}", *(["--body", body] if body else []),
            store_dir=tmp_path)
        return capsys.readouterr().out.strip()
    two, other = add("two", "at ~10:00Z"), add("other", "v1")
    first = add("first", "")
    for argv in (["supersede", two, "--append", "--body", "no, 09:41Z"],
                 ["supersede", two, "--add-tag", "x"],
                 ["supersede", two, "--append", "--body", "and again"],
                 ["supersede", other, "--body", "v2, rewritten longer"],
                 ["supersede", other, "--add-tag", "x"],
                 ["supersede", first, "--append", "--body", "a first body"]):
        _commit(tmp_path, capsys)            # each edit its own row
        assert run(*argv, store_dir=tmp_path) == 0
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert next(ln for ln in lines if "item:two" in ln).startswith(
        "  [task, +2 amendments] item:two  at ~10:00Z"), lines
    assert all("amendment" not in ln for ln in lines if "item:two" not in ln), lines
    run("summary", "--json", store_dir=tmp_path)
    heads = json.loads(capsys.readouterr().out)["heads"]
    assert {h["target"]: h.get("amendments") for h in heads} == \
        {"item:two": 2, "item:other": None, "item:first": None}


def test_from_names_who_raised_a_row_and_last_who_wrote_its_head(tmp_path, capsys,
                                                                 monkeypatch):
    """`from <author>` named the head's author, so an amendment relabelled a
    request: the user's task, amended by Codex, printed `from codex`, and one
    the reader amended lost its label, left the block of rows by others, and
    read as the agent's own (the user's call, 2026-10-02: name both)."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def add(kind, name, author):
        run("add", kind, "--target", f"item:{name}", "--body", name, "--author", author,
            store_dir=tmp_path)
        return capsys.readouterr().out.strip()

    def append(nid, author):
        assert run("supersede", nid, "--append", "--body", "more", "--author", author,
                   store_dir=tmp_path) == 0
        capsys.readouterr()
    append(add("task", "relabelled", "alice"), "codex")
    append(add("idea", "amended-by-me", "alice"), "claude")
    append(add("idea", "mine-amended", "claude"), "alice")
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    out = capsys.readouterr().out

    def line(name):
        return next((ln.strip() for ln in out.splitlines() if f"item:{name} " in ln), "")
    assert line("relabelled").startswith("from alice, last by codex [task, +1 amendment] "), out
    assert line("amended-by-me").startswith("from alice, last by claude [idea, parked, +1 amendment] "), out
    assert line("mine-amended").startswith("last by alice [idea, parked, +1 amendment] "), out


def test_rows_that_wait_on_the_reader_are_counted_first_and_lead_their_block(
        tmp_path, capsys, monkeypatch):
    """SKILL.md's rule, "an open row someone else wrote is a request to
    you", loaded after the summary was read, and it also covered a row the
    reader answered last (2026-10-14). The summary opens on the count of the
    rows whose newest word is someone else's and lists them under it, ahead
    of every other block: spread over the blocks, after a wrap they sat among
    the reader's own, and three of four fresh agents read the lines under the
    count as those rows (2026-10-14). A due one keeps its phrase. A
    pre-registration asks for nothing until it comes due."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")
    import datetime as dt
    soon = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=2)).isoformat()

    def add(name, *a):
        run("add", "task", "--target", f"item:{name}", "--body", name, "--author", "alice", *a,
            store_dir=tmp_path)
        return capsys.readouterr().out.strip()
    add("waits0")
    add("waits1", "--due", soon)
    run("supersede", add("answered"), "--append", "--body", "my answer", store_dir=tmp_path)
    run("add", "prediction", "--target", "item:p", "--checked", "the sweep", "--body", "x rises",
        "--author", "alice", store_dir=tmp_path)
    run("add", "task", "--target", "item:mine", "--tag", "priority", "--body", "mine",
        store_dir=tmp_path)
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:4] == [
        "  2 open entries wait on you: someone else wrote the newest word on each; act on them "
        "or name them to the user before other work",
        next(ln for ln in lines if ln.startswith("  due in 2d from alice [task] item:waits1 ")),
        next(ln for ln in lines if ln.startswith("  from alice [task] item:waits0 "))], lines
    assert sum("item:waits1 " in ln for ln in lines) == 1, "the due block does not repeat it"
    assert any(ln.startswith("  from alice, last by claude [task, +1 amendment] item:answered ")
               for ln in lines), lines
    run("summary", "--full", "--json", store_dir=tmp_path)
    d = json.loads(capsys.readouterr().out)
    assert [w["target"] for w in d["waiting"]] == ["item:waits1", "item:waits0"]
    assert d["waiting"][0]["phrase"] == "due in 2d" and "phrase" not in d["waiting"][1]
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("summary", store_dir=tmp_path)
    assert "wait on you" not in capsys.readouterr().out, "at a terminal the reader is the person"


def test_the_summary_names_a_reader_its_agent_would_not_be(tmp_path, capsys, monkeypatch):
    """A `$SYMBION_AUTHOR` left exported made a Claude Code session read as
    another author: the block of what waits on you listed the agent's own
    questions, and was nearly reported as its view (2026-10-16).
    The first line names the reader when it is not the shell's agent's own
    name, as Codex's hook sets it."""
    run("add", "question", "--target", "item:q", "--body", "which?", "--author", "claude",
        store_dir=tmp_path)
    monkeypatch.setenv("CLAUDECODE", "1")

    def first(*env):
        monkeypatch.delenv("SYMBION_AUTHOR", raising=False)
        for name in env:
            monkeypatch.setenv("SYMBION_AUTHOR", name)
        capsys.readouterr()
        run("summary", store_dir=tmp_path)
        return capsys.readouterr().out.splitlines()[0]
    assert first("other-agent").endswith(
        " (for other-agent): open outside arcs: bug 0, task 0, question 1, prediction 0")
    assert "(for " not in first()
    assert "(for " not in first("claude"), "the agent's own name"
    monkeypatch.setenv("SYMBION_AUTHOR", "other-agent")
    run("summary", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)["reader"] == "other-agent"


def test_the_line_someone_else_added_prints_below_the_lead(tmp_path, capsys, monkeypatch):
    """A user's answer appended to a question sits at the body's end, and a
    digest prints the start: two sessions in a row relayed answered decisions
    as pending, `last by <name>` the only sign (2026-10-07). The newest
    amendment by someone other than the reader prints its first new line,
    unless the clipped lead already shows it; the reader's own `noted` after
    it does not hide it. It says when the lead was rewritten after it: an
    answer the reader had already acted on read as a fresh answer to the new
    lead (2026-10-07). An append, `noted`, is no rewrite of the lead."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def w(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        nid = capsys.readouterr().out.strip()
        _commit(tmp_path, capsys)             # each edit its own row
        return nid
    asked = w("add", "task", "--target", "item:asked", "--body", "ship it?\n\n" + "a reason. " * 30)
    answered = w("supersede", asked, "--append", "--body", "yes, ship it", "--author", "alice")
    noted = w("supersede", answered, "--append", "--body", "noted")
    w("supersede", noted, "--add-tag", "x", "--author", "alice")    # adds no line
    gone = w("add", "task", "--target", "item:gone", "--body", "g")
    gone = w("supersede", gone, "--append", "--body", "an answer", "--author", "alice")
    w("supersede", gone, "--body", "g, rewritten without it")
    mine = w("add", "task", "--target", "item:mine", "--body", "m")
    w("supersede", mine, "--append", "--body", "my own amendment")
    led = w("add", "task", "--target", "item:led", "--body", "l")
    redone = w("add", "task", "--target", "item:redone", "--body", "do it?\n\n" + "why. " * 30)
    redone = w("supersede", redone, "--append", "--body", "go ahead", "--author", "alice")
    w("supersede", redone, "--prepend", "--body", "DONE: did it. OPEN: the next part?")
    w("supersede", led, "--prepend", "--body", "DECIDED", "--author", "alice")

    def below(out, name):
        lines = out.splitlines()
        i = next(i for i, ln in enumerate(lines) if f"item:{name} " in ln)
        return lines[i + 1] if i + 1 < len(lines) else ""
    run("summary", store_dir=tmp_path)
    out = capsys.readouterr().out
    want = "    alice added just now: yes, ship it"
    assert below(out, "asked") == want, out
    assert below(out, "redone") == "    alice added just now, lead rewritten since: go ahead", out
    assert " added " not in below(out, "mine") + below(out, "led") + below(out, "gone"), out
    run("summary", "--json", store_dir=tmp_path)
    d = json.loads(capsys.readouterr().out)
    heads = {h["target"]: h for h in d["heads"] + d["waiting"]}      # asked waits on the reader
    a = heads["item:asked"]
    assert (a["added"], a["added_by"], a["lead_rewritten_since"]) == ("yes, ship it", "alice", False)
    assert heads["item:redone"]["lead_rewritten_since"] is True
    run("list", "--all", "--json", "--limit", "0", store_dir=tmp_path)
    assert a["added_at"] == next(r["created_at"] for r in json.loads(capsys.readouterr().out)
                                 if r["author"] == "alice" and r["body"].endswith("ship it"))
    assert not any("added" in heads[f"item:{t}"] for t in ("mine", "led", "gone"))
    run("list", "--status", "open", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert want in out.splitlines(), out
    assert out.count(" added ") == 2, out


def test_a_waiting_row_says_what_its_newest_word_changed(tmp_path, capsys, monkeypatch):
    """The user starred an idea, and it waited on the agent under the line
    `phreakocious added 5d ago: …`, a link the agent had answered since: a
    link that changes fields and no text printed nothing (2026-10-16). The
    newest link, when someone else's, says what it changed, under the lines
    added before it. Since 2026-10-22 such a link alone waits on no one
    (test_an_edit_by_someone_else_with_no_text_waits_on_no_one), so here
    someone else's text comes before it."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def w(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        nid = capsys.readouterr().out.strip()
        _commit(tmp_path, capsys)             # each edit its own row
        return nid
    star = w("add", "idea", "--target", "item:star", "--body", "look at this", "--author", "alice")
    star = w("supersede", star, "--append", "--body", "looked: it holds")
    star = w("supersede", star, "--append", "--body", "and the next one?", "--author", "alice")
    w("supersede", star, "--add-tag", "priority", "--author", "alice")
    tag = w("add", "task", "--target", "item:tag", "--body", "do this", "--author", "alice")
    tag = w("supersede", tag, "--append", "--body", "on it")
    tag = w("supersede", tag, "--append", "--body", "and that", "--author", "alice")
    w("supersede", tag, "--add-tag", "later", "--due", "2030-01-01", "--author", "alice")
    w("supersede", w("add", "task", "--target", "item:mine", "--body", "m"), "--add-tag", "x")
    run("summary", "--full", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    at = {name: next(i for i, ln in enumerate(lines) if f"item:{name} " in ln)
          for name in ("star", "tag", "mine")}
    assert lines[at["star"] + 1] == "    alice starred it just now", lines
    assert lines[at["tag"] + 1] == "    alice changed its due date, tags just now", lines
    assert sum("starred it" in ln or "changed its" in ln for ln in lines) == 2, \
        "the reader's own retag is no news"
    run("summary", "--json", store_dir=tmp_path)
    w_ = {r["target"]: r for r in json.loads(capsys.readouterr().out)["waiting"]}
    assert (w_["item:star"]["changed"], w_["item:star"]["changed_by"]) == ("starred it", "alice")


def test_an_edit_by_someone_else_with_no_text_waits_on_no_one(tmp_path, capsys, monkeypatch):
    """Someone else's options on the agent's own questions put each in its
    `wait on you` block (2026-10-22). The user ruled out every edit with
    no body text: options, a star, a tag, a due date, an arc. Text below
    still waits, and the GUI's `asks` reads as `waits` does."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def w(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        nid = capsys.readouterr().out.strip()
        _commit(tmp_path, capsys)             # each edit its own row
        return nid
    arc = w("arc", "create", "--name", "x")
    for name, *edit in (("opts", "--option", "a", "--option", "b"), ("star", "--add-tag", "priority"),
                        ("tag", "--add-tag", "later"), ("due", "--due", "2030-01-01"),
                        ("arc", "--arc-id", arc), ("text", "--append", "--body", "and this?")):
        nid = w("add", "question", "--target", f"item:{name}", "--body", "which?")
        w("supersede", nid, *edit, "--author", "alice")
    run("summary", "--json", store_dir=tmp_path)
    d = json.loads(capsys.readouterr().out)
    assert [r["target"] for r in d["waiting"]] == ["item:text"], d["waiting"]
    assert [r["target"] for r in d["priority"]] == ["item:star"], "a star still prints"
    loaded = store.load(tmp_path)
    by_id = {n.id: n for n in loaded}
    assert [n.target.name for n in store.heads(loaded)
            if summ.asks("claude", by_id, n)] == ["text"]


def test_an_answer_below_an_edited_lead_still_prints(tmp_path, capsys, monkeypatch):
    """A user answered by rewriting the body: the lead lost its last
    sentence, and the answer went on a line below it. The first changed line
    was the edited lead, which the summary already shows, so the answer never
    printed (2026-10-11). The line printed is the first new one the lead does
    not show."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")
    assert run("add", "question", "--target", "item:asked", "--body",
               "keep it or flip it? " + "why. " * 30 + "Answer with resolve --body.",
               store_dir=tmp_path) == 0
    asked = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    assert run("supersede", asked, "--body", "keep it or flip it? " + "why. " * 30
               + "\n\nb. a would mislead",
               "--author", "alice", store_dir=tmp_path) == 0
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "    alice added just now: b. a would mislead" in out.splitlines(), out


def test_each_other_author_keeps_a_line_below_the_lead(tmp_path, capsys, monkeypatch):
    """The user answered an agent's question `approved.`, then a reviewer
    appended a relay: the agent's summary printed the reviewer's line alone,
    and the answer reached it only by `show` (2026-10-14). Each other author's
    newest line prints, oldest first; an author whose newest line the lead
    shows prints none, and does not hide an earlier author's."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def w(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        nid = capsys.readouterr().out.strip()
        _commit(tmp_path, capsys)
        return nid
    asked = w("add", "question", "--target", "item:rig", "--body", "a rig?\n\n" + "why. " * 30)
    asked = w("supersede", asked, "--append", "--body", "approved.", "--author", "alice")
    asked = w("supersede", asked, "--append", "--body", "seconded", "--author", "carol")
    asked = w("supersede", asked, "--append", "--body", "relay: no second rig", "--author", "bob")
    w("supersede", asked, "--append", "--body", "noted")
    led = w("add", "question", "--target", "item:led", "--body", "l?\n\n" + "why. " * 30)
    led = w("supersede", led, "--append", "--body", "yes", "--author", "alice")
    w("supersede", led, "--prepend", "--body", "DECIDED", "--author", "bob")
    twice = w("add", "question", "--target", "item:twice", "--body", "t?\n\n" + "why. " * 30)
    twice = w("supersede", twice, "--append", "--body", "first", "--author", "alice")
    w("supersede", twice, "--append", "--body", "second", "--author", "alice")

    run("summary", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, ln in enumerate(lines) if "item:rig " in ln)
    assert lines[i + 1:i + 4] == ["    alice added just now: approved.",
                                  "    carol added just now: seconded",
                                  "    bob added just now: relay: no second rig"], lines
    i = next(i for i, ln in enumerate(lines) if "item:led " in ln)
    assert lines[i + 1] == "    alice added just now, lead rewritten since: yes", lines
    i = next(i for i, ln in enumerate(lines) if "item:twice " in ln)
    assert lines[i + 1] == "    alice added just now: second", lines
    assert sum(" added " in ln for ln in lines) == 5, lines
    run("summary", "--json", store_dir=tmp_path)
    d = json.loads(capsys.readouterr().out)
    rig = next(h for h in d["heads"] + d["waiting"] if h["target"] == "item:rig")
    assert (rig["added"], rig["added_by"]) == ("relay: no second rig", "bob")
    assert [a["added_by"] for a in rig["added_earlier"]] == ["alice", "carol"]
    run("list", "--status", "open", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "    alice added just now: approved." in out.splitlines(), out


def test_a_row_someone_else_closed_reaches_its_raiser_until_it_writes(tmp_path, capsys,
                                                                    monkeypatch):
    """A user answered an agent's question with `resolve --body`, and every
    summary block lists open rows, so the agent never saw the answer
    (2026-10-11). The raiser's summary names who closed the row and the line
    they added, until the raiser writes again. Not a row the raiser closed
    that someone re-tagged after, nor a row someone else raised."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def w(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        nid = capsys.readouterr().out.strip()
        _commit(tmp_path, capsys)
        return nid
    asked = w("add", "question", "--target", "item:asked", "--body", "flip it?\n\n" + "why. " * 30)
    done = w("add", "task", "--target", "item:done", "--body", "d")
    done = w("resolve", done, "--body", "did it")
    answer = w("resolve", asked, "--body", "flip it.", "--author", "alice")
    w("supersede", done, "--add-tag", "x", "--author", "alice")
    theirs = w("add", "task", "--target", "item:theirs", "--body", "t", "--author", "alice")
    w("resolve", theirs, "--author", "alice")

    run("summary", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, ln in enumerate(lines) if "closed by" in ln)
    assert lines[i].startswith("  closed by alice [question") and "item:asked" in lines[i], lines
    assert lines[i + 1] == "    alice added just now: flip it.", lines
    assert sum("closed by" in ln for ln in lines) == 1, lines
    run("summary", "--json", store_dir=tmp_path)
    c = json.loads(capsys.readouterr().out)["closed"]
    assert [(r["id"], r["closed_by"], r["added"]) for r in c] == [(answer, "alice", "flip it.")]

    w("add", "note", "--target", "item:asked", "--body", "flipped")
    run("summary", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)["closed"] == []


def test_a_correction_to_a_row_the_reader_closed_reaches_it(tmp_path, capsys, monkeypatch):
    """An agent resolved its bug with a claim, and the user appended a
    correction: the row was resolved, so no block printed it, and the agent's
    next session read only its own stale claim (2026-10-12). A row the reader
    raised or closed prints in the closed block when someone else wrote to its
    body after the reader's last write."""
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")

    def w(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        nid = capsys.readouterr().out.strip()
        _commit(tmp_path, capsys)
        return nid
    mine = w("add", "bug", "--target", "item:lint", "--body", "ruff fails. " + "why. " * 30)
    w("resolve", mine, "--body", "fixed; the format drift is deliberate")
    theirs = w("add", "task", "--target", "item:ship", "--body", "ship it. " + "why. " * 30,
               "--author", "alice")
    w("resolve", theirs, "--body", "shipped")
    w("supersede", mine, "--append", "--body", "not deliberate: the README formats",
      "--author", "alice")
    w("supersede", theirs, "--append", "--body", "not shipped: no tag", "--author", "alice")

    run("summary", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    at = [i for i, ln in enumerate(lines) if "closed by" in ln]
    assert [lines[i].split(" [")[0] for i in at] == ["  closed by claude, from alice",
                                                     "  closed by claude, last by alice"], lines
    assert [lines[i + 1] for i in at] == ["    alice added just now: not shipped: no tag",
                                          "    alice added just now: not deliberate: the README formats"]

    w("add", "note", "--target", "item:lint", "--body", "read it")
    run("summary", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)["closed"] == []


def test_a_line_from_an_edit_that_wrote_no_paragraph_reads_edited(tmp_path, capsys,
                                                                 monkeypatch):
    """An agent rewrote a row the user had amended, and the user's summary
    printed `claude added 18h ago: Fix options: …`, a reworded sentence of
    the first version, under a row with no amendment count (2026-10-12).
    A line from a link that wrote no paragraph of its own reads `edited`;
    an append still reads `added` and counts."""
    monkeypatch.setenv("SYMBION_AUTHOR", "ada")
    lead = "the build fails. " + "why. " * 30
    run("add", "bug", "--target", "item:b", "--body", lead + "\n\nfix: restore the dir.",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)

    def summary():
        run("summary", store_dir=tmp_path)
        lines = capsys.readouterr().out.splitlines()
        i = next(i for i, ln in enumerate(lines) if "item:b " in ln)
        return lines[i], lines[i + 1]
    run("supersede", nid, "--body", lead + "\n\nfix: restore the dir, or document it.",
        "--author", "sam", store_dir=tmp_path)
    _commit(tmp_path, capsys)
    row, line = summary()
    assert "[bug]" in row and line == "    sam edited just now: fix: restore the dir, or document it."
    run("supersede", nid, "--append", "--body", "also: pin it.", "--author", "sam",
        store_dir=tmp_path)
    _commit(tmp_path, capsys)
    row, line = summary()
    assert "[bug, +1 amendment]" in row and line == "    sam added just now: also: pin it."
    # A body written onto a row that had none is the link's own text: seams()
    # marks no seam on an empty body.
    run("add", "task", "--target", "item:e", store_dir=tmp_path)
    eid = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("supersede", eid, "--body", lead + "\n\nsee: the log.", "--author", "sam",
        store_dir=tmp_path)
    _commit(tmp_path, capsys)
    run("summary", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "    sam added just now: see: the log." in out.splitlines(), out


@pytest.mark.parametrize("since, want", [
    (False, "alice added 19h ago:"), (True, "alice added 19h ago, lead rewritten since:")])
def test_the_added_line_says_its_age_and_a_lead_rewritten_since(since, want):
    from datetime import datetime, timedelta, timezone
    at = (datetime.now(timezone.utc) - timedelta(hours=19, minutes=5)).isoformat()
    assert summ.added_label({"added_by": "alice", "added_at": at,
                             "lead_rewritten_since": since}) == want


def test_json_rows_name_who_raised_them_and_when(tmp_path, capsys, monkeypatch):
    """`author` is the head's, so one `supersede --append` by a teammate moved
    a row out of a consumer's filter on its raiser (an agent harness, 2026-10-07).
    `raised_by` is the author of the chain's first row, on every JSON row, and
    `raised_at` its time: by `created_at`, a row amended today read as filed
    today, and dating each chain took a walk of `supersedes` (2026-10-12)."""
    run("arc", "create", "--name", "a", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    run("add", "task", "--target", "item:x", "--body", "b", "--author", "ada",
        "--arc-id", aid, store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    _commit(tmp_path, capsys)
    run("list", "--json", store_dir=tmp_path)
    filed = json.loads(capsys.readouterr().out)[0]["created_at"]
    monkeypatch.setattr(store, "_now_iso", lambda: "2099-01-01T00:00:00+00:00")
    run("supersede", nid, "--append", "--body", "more", "--author", "grace",
        store_dir=tmp_path)
    run("add", "task", "--target", "item:y", "--body", "b", "--author", "solo",
        store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert {r["target"]["name"]: (r["author"], r["raised_by"]) for r in rows} == \
        {"x": ("grace", "ada"), "y": ("solo", "solo")}
    assert {r["target"]["name"]: (r["created_at"], r["raised_at"]) for r in rows} == \
        {"x": ("2099-01-01T00:00:00+00:00", filed),
         "y": ("2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")}
    run("context", "--target", "item:x", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)["notes"][0]["raised_by"] == "ada"
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)[0]["raised_by"] == "ada"


def test_a_row_keeps_its_labels_in_every_summary_block(tmp_path, capsys, monkeypatch):
    """`from <author>` and a pre-registration's `registered <date>` rode on
    the heads block only, so a star, or a due date coming near, moved a row
    to a block that dropped both (a fresh agent starred its prediction and
    lost the date, 2026-09-29). The labels are the row's, not the block's."""
    import datetime as dt
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")
    _declare(tmp_path, PREREG)
    soon = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=2)).isoformat()
    kw = dict(kind="prediction", status="open", checked="the sweep", author="ada",
              created_at="2026-09-20T12:00:00+00:00")
    store.add(tmp_path, target={"type": "item", "name": "starred"}, body="s",
              tags=["priority"], **kw)
    store.add(tmp_path, target={"type": "item", "name": "due"}, body="d", due=soon, **kw)
    store.add(tmp_path, target={"type": "item", "name": "plain"}, body="p", **kw)
    run("add", "task", "--target", "item:mine", "--tag", "priority", "--body", "m",
        store_dir=tmp_path)
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()

    def line(name):
        return next(ln for ln in lines if f"item:{name} " in ln)
    assert line("starred").startswith("  priority from ada "), line("starred")
    assert line("due").startswith("  due in 2d from ada "), line("due")
    for name in ("starred", "due", "plain"):
        assert "from ada [prediction, registered 2026-09-20" in line(name), line(name)
    assert "registered 2026-09-20, starred " in line("starred"), "a star's age rides along"
    assert line("mine").startswith("  priority [task] item:mine "), line("mine")


def test_a_full_row_says_when_and_by_whom_and_what_it_revises(tmp_path, capsys):
    """Text `show` had no date, no author and no earlier versions; only
    --json carried created_at and supersedes (newcomer walk-through,
    2026-09-26). The id is a timestamp no human reads as one."""
    run("add", "note", "--target", "item:x", "--body", "v1", "--author", "ada",
        store_dir=tmp_path)
    old = capsys.readouterr().out.strip()
    run("supersede", old, "--body", "v2", "--author", "sam", store_dir=tmp_path)
    new = capsys.readouterr().out.strip()
    rows = {n.id: n for n in store.load(tmp_path)}
    head = store.heads(rows.values())[0]
    run("show", new[-3:], store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    utc = [r.created_at.replace("T", " ").replace("+00:00", "Z")
           for r in (rows[head.supersedes], head)]
    assert lines[1] == (f"    written {utc[0]} by ada; body changed {utc[1]} by sam; "
                        f"earlier version saved as {head.supersedes}"), lines
    assert new == old != head.supersedes
    run("list", store_dir=tmp_path)
    assert "written" not in capsys.readouterr().out, "a page stays one line per entry"


def test_a_star_does_not_take_the_bodys_authorship(tmp_path, capsys, monkeypatch):
    """The user starred an agent's bug, and `show` said the row was
    "written … by" the user: an edit keeps the id, so the head's author
    read as the body's (2026-10-12). The line names who wrote the body and
    what the newest link changed."""
    run("add", "bug", "--target", "item:x", "--body", "b", "--author", "ada",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--add-tag", "priority", "--due", "2030-01-01", "--author", "sam",
        store_dir=tmp_path)
    capsys.readouterr()
    rows = {n.id: n for n in store.load(tmp_path)}
    head, first = rows[nid], rows[rows[nid].supersedes]
    utc = [r.created_at.replace("T", " ").replace("+00:00", "Z") for r in (first, head)]
    run("show", nid, store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[1] == (
        f"    written {utc[0]} by ada; due, tags changed {utc[1]} by sam; "
        f"earlier version saved as {head.supersedes}")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("COLUMNS", "200")
    run("show", nid, store_dir=tmp_path)
    assert (f"written {utc[0]} by ada · due, tags changed {utc[1]} by sam · earlier version saved as"
            in _ANSI.sub("", capsys.readouterr().out))


def test_supersede_kind_parks_a_task_in_place(tmp_path, capsys):
    """Asked to park a task, 4 of 5 fresh agents kept it open with a due date,
    and 1 filed an idea and resolved the task, which then read as done under
    another id (2026-10-14). `--kind idea` parks it in place: the same id,
    still open. Beside --add-tag it is one write, past the retag fast path."""
    run("add", "task", "--target", "item:x", "--body", "b", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("supersede", nid, "--kind", "idea", "--add-tag", "later",
               store_dir=tmp_path) == 0
    assert capsys.readouterr().out.strip() == nid
    head = store.heads(store.load(tmp_path))[0]
    assert (head.id, head.kind, head.status, head.tags) == (nid, "idea", "open", ("later",))


def test_an_addition_does_not_take_the_raisers_authorship(tmp_path, capsys):
    """A gotcha the user wrote and the agent amended read `written … by` the
    agent: the line named the newest body's author only (skill trial,
    2026-10-14). It names who raised the entry, then who changed its body."""
    run("add", "task", "--target", "item:x", "--body", "b", "--author", "ada",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--body", "more", "--append", "--author", "sam", store_dir=tmp_path)
    capsys.readouterr()
    rows = {n.id: n for n in store.load(tmp_path)}
    head, first = rows[nid], rows[rows[nid].supersedes]
    utc = [r.created_at.replace("T", " ").replace("+00:00", "Z") for r in (first, head)]
    run("show", nid, store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[1] == (
        f"    written {utc[0]} by ada; body changed {utc[1]} by sam; "
        f"earlier version saved as {head.supersedes}")


def test_list_since_keeps_the_rows_written_in_a_window(tmp_store, tmp_path, capsys):
    """An adopter's agent typed `symbion list --since 2h` to see what the
    last hours wrote, and got `unrecognized arguments` (measured 2026-09-25).
    `git log --since` is the convention: a span back from now, a date, or a
    datetime. Rows written before offsets were stamped are naive: local."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now().astimezone()
    def add(when, body):
        store.add(tmp_path, kind="note", target={"type": "item", "name": body},
                  body=body, created_at=when)
    add((now - timedelta(hours=3)).isoformat(timespec="seconds"), "three hours ago")
    add((now - timedelta(minutes=30)).isoformat(timespec="seconds"), "half an hour ago")
    add("2026-09-02T10:00:00", "naive, early September")

    def bodies(*argv):
        assert run("list", *argv, "--json", store_dir=tmp_path) == 0
        return {r["body"] for r in json.loads(capsys.readouterr().out)}
    assert bodies("--since", "2h") == {"half an hour ago"}
    assert bodies("--since", "90m") == {"half an hour ago"}
    assert bodies("--since", "1d") == {"half an hour ago", "three hours ago"}
    assert bodies("--since", "2026-09-01") >= {"naive, early September"}
    legacy = datetime(2026, 9, 2, 10).astimezone()       # naive: the writer's local time
    after = (legacy + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
    assert "naive, early September" not in bodies("--since", after)
    assert run("list", "--since", "yesterday", store_dir=tmp_path) == 2
    assert "30m, 2h, 3d, 1w" in capsys.readouterr().err
    run("list", "--since", "2h", store_dir=tmp_path)
    assert capsys.readouterr().out.startswith("1 of 3 match --since 2h")


def test_list_since_wrap_opens_the_window_at_the_last_wrap(tmp_store, tmp_path, capsys,
                                                            monkeypatch):
    """A handoff written from memory rounded a "mostly held" result up to
    "held" (2026-10-15). `--since wrap` lists what was written since the last
    wrap, a resolve included, each entry with its state and result, so a wrap
    writes from what the store holds. With no wrap yet it says what to type."""
    from datetime import datetime, timedelta
    before = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    store.add(tmp_path, kind="note", target={"type": "project", "name": None},
              body="an early fact", created_at=before)
    p = store.add(tmp_path, kind="prediction", target={"type": "project", "name": None},
                  body="it will work", checked="the run", created_at=before)
    assert run("list", "--since", "wrap", store_dir=tmp_path) == 2
    assert "no wrap in this store yet" in capsys.readouterr().err
    monkeypatch.setenv("SYMBION_AUTHOR", "ada")
    assert run("commit", "--wrap", "-m", "the gist\n\nthe day", store_dir=tmp_path) == 0
    assert run("resolve", p.id, "--result", "mostly held; missed one", store_dir=tmp_path) == 0
    capsys.readouterr()
    assert run("list", "--since", "wrap", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 of 2 match --since wrap"), out
    assert "result: mostly held; missed one" in out and "early fact" not in out


def test_an_arc_line_symbion_cannot_read_survives_every_arc_write(tmp_path, capsys):
    """load_arcs skipped an unreadable line silently and every arc write
    rewrote the registry from what it had read, so the next `arc create`
    erased a hand-edited line for good (reproduced 2026-09-27). Each write
    keeps such a line verbatim, and the summary counts it."""
    run("arc", "create", "--name", "A", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    bad = '{"id": "hand-edited", "name": "B", "target_scope": "item"}'
    with open(tmp_path / "arcs.jsonl", "a") as f:
        f.write(bad + "\n")
    for argv in (["arc", "create", "--name", "C"], ["arc", "rename", aid, "A2"],
                 ["arc", "archive", aid]):
        assert run(*argv, store_dir=tmp_path) == 0, argv
        assert bad in (tmp_path / "arcs.jsonl").read_text().splitlines(), argv
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    # line 3: a rewrite keeps the line after the arcs it wrote (a, c)
    assert "1 arc line unreadable, first: line 3: missing key 'created_at'" in \
        capsys.readouterr().out


@pytest.mark.parametrize("file, line, what", [
    ("notes.jsonl", {"target": {"type": "global", "name": None}}, "entry"),
    ("notes.jsonl", {"target": {"type": "activity", "name": "a"}}, "entry"),
    ("notes.jsonl", {"target": {"type": "project", "name": None},
                     "refs": [{"type": "global", "name": None}]}, "entry"),
    ("arcs.jsonl", {"target_scope": "global"}, "arc line"),
])
def test_a_row_or_arc_in_the_retired_vocabulary_is_named_on_load(tmp_path, capsys, file, line, what):
    """The vocabulary spec's safety argument: a row or arc left in the
    retired vocabulary is refused on load, with the migration named. Kinds
    were; `global`/`activity` targets and refs, and a `global` arc scope,
    loaded silently (spec drift audit, 2026-09-25). One case per name."""
    store.ensure_store(tmp_path)
    base = ({"id": "20260901-000000-000001-abc", "kind": "note", "created_at": "2026-09-01T00:00:00Z"}
            if file == "notes.jsonl" else
            {"id": "old", "name": "Old", "created_at": "2026-09-01T00:00:00Z"})
    raw = json.dumps(base | line)
    (tmp_path / file).write_text(raw + "\n")
    run("summary", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert f"1 {what} unreadable" in out and "global->project" in out, out
    assert raw in (tmp_path / file).read_text(), "reported, never dropped"


def test_summary_heads_are_newest_first_within_one_second(tmp_path, capsys, monkeypatch):
    """`created_at` is to the second, and the summary sorted on it alone, so
    a batch written in one second printed oldest first (doc claims audit,
    2026-09-27). The id breaks the tie, as in `list`."""
    rows = "".join(json.dumps({"kind": "task", "target": {"type": "item", "name": f"t{i}"},
                               "body": f"entry {i}"}) + "\n" for i in range(4))
    monkeypatch.setattr("sys.stdin", io.StringIO(rows))
    run("add", "--from-json", "-", store_dir=tmp_path)
    ids = capsys.readouterr().out.split()
    assert len({n.created_at for n in store.load(tmp_path)}) == 1, "the batch shares a second"
    run("summary", store_dir=tmp_path)
    shown = [ln.split()[-1] for ln in capsys.readouterr().out.splitlines() if "[task]" in ln]
    assert shown == ids[::-1], shown


def test_every_verb_and_arc_verb_says_what_it_does(tmp_path):
    """`symbion arc -h` listed create, list, rename, archive and seed with no
    text beside them, and `context` read `pull-based detail`."""
    p = cli._build_parser(frozenset({"item"}), frozenset(), frozenset(), tmp_path,
                          K.DEFAULT_KINDS)
    top = p._subparsers._group_actions[0]
    arc = top.choices["arc"]._subparsers._group_actions[0]
    for sub in (top, arc):
        # argparse lists a verb in _choices_actions only when it has help=.
        said = {a.dest for a in sub._choices_actions if (a.help or "").strip()}
        assert set(sub.choices) - said == set(), set(sub.choices) - said
    assert "pull-based" not in top.choices["context"].format_help()


def test_list_text_renders_the_verdict_on_any_verdict_kind(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\naudit = { verdict = true }\n')
    run("add", "--kind", "audit", "--type", "project", "--checked", "x", "--result", "y",
        store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    out = capsys.readouterr().out.splitlines()
    assert out[2].startswith("    checked") and out[2].endswith(": x") and out[3] == "    result: y", out


def test_seed_kind_mints_it_and_an_ineligible_one_is_a_clean_error(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\nfollowup = { status = true }\nfact = {}\n')
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    capsys.readouterr()
    assert run("arc", "seed", aid, "--scope", "item", "--name", "a", "--kind", "followup",
               store_dir=tmp_path) == 0
    assert "seeded 1 new followup(s)" in capsys.readouterr().err
    rc = run("arc", "seed", aid, "--scope", "item", "--name", "b", "--kind", "fact",
             store_dir=tmp_path)
    assert rc == 1 and "status bit" in capsys.readouterr().err
    rc = run("arc", "seed", aid, "--scope", "item", "--name", "c", store_dir=tmp_path)
    assert rc == 1 and "--kind" in capsys.readouterr().err, "no task declared: name the flag"


def test_reconcile_reports_a_prediction_as_needing_a_result(tmp_path, capsys):
    (tmp_path / "symbion.toml").write_text(
        '[catalogs]\nthing = "echo live1"\n' + PREREG)
    run("arc", "create", "--name", "x", "--scope", "thing", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "thing", "--name", "stale1", store_dir=tmp_path)
    run("add", "--kind", "prediction", "--type", "thing", "--name", "stale2", "--arc-id", aid,
        store_dir=tmp_path)
    capsys.readouterr()
    assert run("arc", "reconcile", aid, "--resolve-stale", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "STALE    thing:stale2" in out and "needs --result" in out
    assert "applied: 0 re-targeted and 0 re-pointed (renamed, whole store), 1 resolved (stale), 1 skipped (needs --result)" in out


def test_reconcile_names_what_a_stale_target_lacks_in_the_store_s_words(tmp_path, capsys):
    """`stale_target_noun` names a catalog's members in the STALE line; a
    store that catalogs atlas entries says so, not "catalog entry"."""
    (tmp_path / "symbion.toml").write_text(
        'stale_target_noun = "live atlas entry"\n[catalogs]\nthing = "echo live1"\n')
    run("arc", "create", "--name", "x", "--scope", "thing", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "thing", "--name", "stale1", store_dir=tmp_path)
    capsys.readouterr()
    assert run("arc", "reconcile", aid, store_dir=tmp_path) == 0
    assert "STALE    thing:stale1  (no live atlas entry)" in capsys.readouterr().out


def test_reconcile_apply_alone_reports_zero_skipped(tmp_path, capsys):
    """`--apply` without `--resolve-stale` never attempts a stale row, so
    `skipped` (a count of ATTEMPTS that needed --result) must read 0 --
    not the count of stale/needs_result rows in the batch, which is what
    `skipped` used to report even when no attempt was made on them."""
    (tmp_path / "symbion.toml").write_text(
        '[catalogs]\nthing = "echo live1"\n' + PREREG)
    run("arc", "create", "--name", "x", "--scope", "thing", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    run("arc", "seed", aid, "--scope", "thing", "--name", "stale1", store_dir=tmp_path)
    run("add", "--kind", "prediction", "--type", "thing", "--name", "stale2", "--arc-id", aid,
        store_dir=tmp_path)
    capsys.readouterr()
    assert run("arc", "reconcile", aid, "--apply", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "applied: 0 re-targeted and 0 re-pointed (renamed, whole store), 0 resolved (stale), 0 skipped (needs --result)" in out


def test_schema_text_and_json(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true, when = "off the curve" }\n')
    run("add", "--kind", "anomaly", "--type", "project", store_dir=tmp_path)
    capsys.readouterr()
    assert run("schema", store_dir=tmp_path) == 0
    text = capsys.readouterr().out
    assert text.startswith("kinds  (symbion.toml [kinds])") and "anomaly" in text
    assert run("schema", "--json", store_dir=tmp_path) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["declared"] is True and d["kinds"][0]["rows"] == 1


def test_schema_on_an_absent_store_names_it_in_text_and_prints_defaults_in_json(repo, tmp_path, capsys):
    """Silence was for the hook, which stopped running schema; after that it
    read as a store with no kinds."""
    assert run("schema", store_dir=tmp_path / "nowhere") == 1
    assert capsys.readouterr().err == f"symbion: no store at {tmp_path / 'nowhere'}; run `symbion init`\n"
    assert run("schema", "--json", store_dir=tmp_path / "nowhere") == 0
    d = json.loads(capsys.readouterr().out)
    assert d["declared"] is False and [k["label"] for k in d["kinds"]] == list(K.DEFAULT_KINDS)


def test_schema_toml_prints_the_table_to_paste(tmp_path, capsys):
    """A `[kinds]` table replaces the defaults, so a store adding a kind
    copies the ones it keeps; with no table in its symbion.toml there was
    nothing to copy, and 3 of 4 fresh agents guessed the `when` key
    (2026-09-29). Both shapes: no table prints the defaults, a declared
    table prints itself."""
    store.ensure_store(tmp_path)
    assert run("schema", "--toml", store_dir=tmp_path) == 0
    text = capsys.readouterr().out
    assert text.startswith("[kinds]\n") and "when = " in text
    assert K.parse_kinds(tomllib.loads(text)["kinds"]) == K.DEFAULT_KINDS
    _declare(tmp_path, PREREG)
    assert run("schema", "--toml", store_dir=tmp_path) == 0
    got = K.parse_kinds(tomllib.loads(capsys.readouterr().out)["kinds"])
    assert got == K.read_kinds(tmp_path) and "prediction" in got


# ---- resolvers, end to end ----
from _resolvers import reading_store, calls


def _reading_add(store_dir, name, *extra):
    return run("add", "--kind", "note", "--type", "reading", "--name", name,
               "--body", "x", *extra, store_dir=store_dir)


def test_add_stores_the_resolvers_answer(repo, tmp_path, capsys):
    s = reading_store(tmp_path)
    assert _reading_add(s, "40.40") == 0
    assert _reading_add(s, "40.45") == 0
    assert {n.target.name for n in store.load(s)} == {"40.40"}


def test_add_from_json_batch_of_neighbours_stores_one_target(repo, tmp_path, capsys, monkeypatch):
    s = reading_store(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": {"type": "reading", "name": "40.40"}}\n'
        '{"kind": "note", "target": {"type": "reading", "name": "40.45"}}\n'))
    assert run("add", "--from-json", "-", store_dir=s) == 0
    assert [n.target.name for n in store.load(s)] == ["40.40", "40.40"]


def test_list_name_resolves_through_the_resolver(repo, tmp_path, capsys):
    s = reading_store(tmp_path)
    _reading_add(s, "40.40")
    capsys.readouterr()
    assert run("list", "--type", "reading", "--name", "40.45", store_dir=s) == 0
    assert "40.40" in capsys.readouterr().out


def test_supersede_ref_hands_the_original_query_to_the_resolver(repo, tmp_path, capsys):
    s = reading_store(tmp_path)
    (tmp_path / "resolve_reading.py").write_text(
        "import sys\nq = sys.stdin.readline().rstrip('\\n')\n"
        f"open('{tmp_path.as_posix()}/seen', 'a').write(q + '\\n')\nprint(q)\n")
    _reading_add(s, "40.40")
    nid = capsys.readouterr().out.strip()
    assert run("supersede", nid, "--ref", "reading:41.25", store_dir=s) == 0
    assert (tmp_path / "seen").read_text().splitlines()[-1] == "41.25"


def test_seed_explicit_names_resolve_and_a_sweep_runs_the_resolver_zero_times(repo, tmp_path, capsys):
    s = reading_store(tmp_path, counter=True)
    _reading_add(s, "40.40")
    _reading_add(s, "44.00")
    run("arc", "create", "--name", "x", "--scope", "reading", store_dir=s)
    aid = capsys.readouterr().out.strip().splitlines()[-1]
    before = calls(s)
    assert run("arc", "seed", aid, "--scope", "reading", store_dir=s) == 0
    assert "seeded 2" in capsys.readouterr().err and calls(s) == before
    assert run("arc", "seed", aid, "--scope", "reading", "--name", "40.45", store_dir=s) == 0
    assert "seeded 0" in capsys.readouterr().err and calls(s) == before + 1
    assert run("arc", "seed", aid, "--scope", "reading", "--dry-run", store_dir=s) == 0
    assert capsys.readouterr().out.splitlines()[1:] == ["40.40", "44.00"]


def test_seed_sweep_over_an_empty_catalog_fails_even_with_a_resolver(repo, tmp_path, capsys):
    s = reading_store(tmp_path)
    run("arc", "create", "--name", "x", "--scope", "reading", store_dir=s)
    aid = capsys.readouterr().out.strip().splitlines()[-1]
    assert run("arc", "seed", aid, "--scope", "reading", store_dir=s) == 1
    assert "produced no names" in capsys.readouterr().err
    assert store.load(s) == []


def test_a_failing_resolver_stores_nothing_and_says_so(repo, tmp_path, capsys):
    s = reading_store(tmp_path)
    (s / "symbion.toml").write_text((s / "symbion.toml").read_text().replace(
        "[resolvers]\nreading = ", '[resolvers]\nreading = "exit 1" # '))
    assert _reading_add(s, "40.40") == 1
    assert "error:" in capsys.readouterr().err
    assert store.load(s) == []


def test_a_piped_catalog_checks_its_producer_or_reads_it_as_empty(repo, tmp_path, capsys):
    """Both directions of the trap the starter toml warns about. The check is
    `out=$(…) &&`, not `set -o pipefail`: dash, Debian's and Ubuntu's `sh`,
    refuses that, so on Linux this test passed on the refusal (2026-10-14)."""
    s = tmp_path / "s"
    s.mkdir()
    (s / "symbion.toml").write_text(
        '[catalogs]\nt = "false | cat"\n[resolvers]\nt = "head -1"\n')
    assert run("add", "--kind", "note", "--type", "t", "--name", "q", "--body", "x",
               store_dir=s) == 0, "sh reports cat's status: the failure is silent"
    (s / "symbion.toml").write_text(
        '[catalogs]\nt = \'out=$(false) && printf "%s\\n" "$out" | cat\'\n[resolvers]\nt = "head -1"\n')
    assert run("add", "--kind", "note", "--type", "t", "--name", "q2", "--body", "x",
               store_dir=s) == 1
    assert [n.target.name for n in store.load(s)] == ["q"]


def test_a_resolver_without_its_catalog_is_a_config_error(repo, tmp_path, capsys):
    s = tmp_path / "s"
    s.mkdir()
    (s / "symbion.toml").write_text('[catalogs]\nt = "echo a"\n[resolvers]\nu = "head -1"\n')
    assert run("summary", store_dir=s) == 1
    err = capsys.readouterr().err
    assert "u" in err and "[catalogs]" in err


def test_summary_json_names_the_store_or_null(repo, tmp_path, capsys):
    """The gate a session-end tool needs:
    an absent store and an empty one printed byte-identical JSON, both exit 0,
    so nothing could tell "no store" from "nothing open". `store` is the
    resolved path when one exists and null when none does."""
    store_dir = tmp_path / "store"
    assert run("summary", "--json", store_dir=store_dir) == 0
    assert json.loads(capsys.readouterr().out)["store"] is None
    store.ensure_store(store_dir)
    assert run("summary", "--json", store_dir=store_dir) == 0
    assert json.loads(capsys.readouterr().out)["store"] == str(store_dir)


def test_summary_text_on_an_absent_store_names_it_and_exits_0(repo, tmp_path, capsys):
    """Silence here read as "no store yet" twice in the field (a `.symbion`
    pointer to nowhere, a renamed repo) while the next `add` forked a second
    store. Exit 0 keeps the hook harmless."""
    store_dir = tmp_path / "store"
    assert run("summary", store_dir=store_dir) == 0
    out, err = capsys.readouterr()
    assert out.strip() == f"symbion: no store at {store_dir}; run `symbion init`"
    assert err == ""
    # The existing-but-empty direction is unchanged: the zero-counts line.
    store.ensure_store(store_dir)
    assert run("summary", store_dir=store_dir) == 0
    assert "open outside arcs" in capsys.readouterr().out


def test_arc_archive_and_rename_print_the_id(tmp_path, capsys):
    """Every other write prints; silence on success read as "did nothing"
    and had to be confirmed with a second command (measured 2026-09-08)."""
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    capsys.readouterr()
    assert run("arc", "rename", aid, "y", store_dir=tmp_path) == 0
    assert capsys.readouterr().out.strip() == aid
    assert run("arc", "archive", aid, store_dir=tmp_path) == 0
    assert capsys.readouterr().out.strip() == aid
    assert run("arc", "archive", "nope", store_dir=tmp_path) == 1
    assert "no arc 'nope'" in capsys.readouterr().err


def test_add_says_when_a_catalog_name_is_stored_as_typed(tmp_path, capsys):
    """A catalog disambiguates, it does not whitelist -- so a typo'd path is
    stored verbatim by design. It used to be stored SILENTLY, and two rows
    about one file then sat under two names (measured 2026-09-20). Both
    directions: a miss says so on stderr, an exact hit says nothing, and a
    substring hit names the pick."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "echo src/parser.py"\n')
    assert run("add", "--kind", "bug", "--type", "file", "--name", "src/parsre.py",
               store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert "'src/parsre.py' matches nothing in the file catalog; taken as typed" in err
    # the likely fix on the same line, as `rename` and arc misses give it
    assert _sans_pending(err).rstrip().endswith("; did you mean 'src/parser.py'?"), err
    run("list", "--type", "file", "--name", "src/lexer.py", store_dir=tmp_path)    # 0.72
    assert "did you mean" not in capsys.readouterr().err, "a new file is no typo"
    run("list", "--type", "file", "--name", "src/parser2.py", store_dir=tmp_path)  # 0.96
    assert "did you mean" not in capsys.readouterr().err, "a numbered sibling is a series"
    # case is no evidence of a new name: 'Src/Parsre.py' scored 0.77 as typed
    run("list", "--type", "file", "--name", "Src/Parsre.py", store_dir=tmp_path)
    assert capsys.readouterr().err.splitlines()[0].endswith("; did you mean 'src/parser.py'?")
    assert out.strip() == store.load(tmp_path)[-1].id, "stdout is still just the id"
    assert run("add", "--kind", "bug", "--type", "file", "--name", "src/parser.py",
               store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert _sans_pending(err) == "", err
    assert run("add", "--kind", "bug", "--type", "file", "--name", "parser",
               store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    # line 2 names the bug added above on the same file (_name_open_neighbours)
    assert err.splitlines()[0] == ("note: 'parser' resolved to 'src/parser.py' "
                                   "(unique substring in the file catalog)"), err
    assert store.load(tmp_path)[-1].target.name == "src/parser.py"


def test_a_read_that_misses_the_catalog_does_not_claim_a_write(tmp_path, capsys):
    """catalog.match is the one picker for both verbs, so its miss note must be
    true on both: on a read nothing is stored, and an agent that believed
    "stored as typed" on a `list` read a phantom write into a read-only
    command (adopter report, 2026-09-28). The other direction is the test
    above: on `add` the row really is stored under the typed name."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "echo src/parser.py"\n')
    run("add", "--kind", "bug", "--type", "file", "--name", "src/parser.py",
        store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--type", "file", "--name", "src/parsre.py",
               store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert "'src/parsre.py' matches nothing in the file catalog; taken as typed" in err
    assert "stored" not in err, err
    assert out.startswith("0 of 1 match --type file --name src/parsre.py")
    assert len(store.load(tmp_path)) == 1, "list writes nothing"


# ---- commit: a write is not off this disk until a push ----
def _store_with_origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    _git(store_dir, "remote", "add", "origin", str(bare))
    return store_dir


def test_commit_reports_the_unpushed_count_when_the_store_has_a_remote(tmp_path, capsys):
    """SKILL.md's footgun ('a write is not in git history until symbion
    commit') had a second half nothing stated: not off this disk until a
    push. Measured 2026-09-22, the first session with an origin on the
    store: commit said 'committed' and the commit sat unpushed."""
    store_dir = _store_with_origin(tmp_path)
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=store_dir)
    capsys.readouterr()
    assert run("commit", store_dir=store_dir) == 0
    out = capsys.readouterr().out
    assert "1 commit" in out and f"symbion --dir {store_dir.as_posix()} push" in out, out
    assert "no remote" not in out


def test_push_sends_the_store_to_origin_and_names_what_it_leaves(tmp_path, capfd):
    """commit's hint was `git -C <store> push`: a path to paste, where the
    store is one symbion already resolves. A note written after the commit
    is not in that push, so push names it."""
    store_dir = _store_with_origin(tmp_path)
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=store_dir)
    run("commit", store_dir=store_dir)
    run("add", "--kind", "note", "--type", "project", "--body", "y", store_dir=store_dir)
    capfd.readouterr()
    assert run("push", store_dir=store_dir) == 0
    err = capfd.readouterr().err
    assert gitref.unpushed(store_dir) == 0
    assert re.search(r"not pushed: 1 entry not yet in the store's git \(symbion --dir \S+ commit\)\n",
                     err), err


def test_push_on_a_store_with_no_remote_says_it_is_on_one_disk(tmp_path, capfd):
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    run("commit", store_dir=tmp_path)
    capfd.readouterr()
    assert run("push", store_dir=tmp_path) == 1
    assert "no remote: this store exists on one disk" in capfd.readouterr().err


def test_commit_counts_its_rows_by_author(tmp_path, capsys, monkeypatch):
    """`commit` took every pending row under the caller's message, so with
    two agents on one store a commit named work it did not do (2026-10-02).
    A `Rows:` trailer says whose rows it holds, and so does the CLI."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")   # every row is this session's
    run("add", "--kind", "note", "--type", "project", "--body", "x", "--author", "codex",
        store_dir=tmp_path)
    for b in ("y", "z"):
        run("add", "--kind", "note", "--type", "project", "--body", b, "--author", "ada",
            store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "-m", "ada's work", store_dir=tmp_path) == 0
    assert "committed: entries ada 2, codex 1" in capsys.readouterr().out
    msg = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%B"],
                         capture_output=True, text=True).stdout
    assert msg.strip() == "ada's work\n\nRows: ada 2, codex 1", msg


def test_commit_names_the_files_it_takes_beside_the_rows(tmp_path, capsys, monkeypatch):
    """`add -A` takes every path in the store. A plan went in with 7 rows
    under a line that named only the rows (2026-10-02), and a script did the
    same (2026-10-05)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")   # every row is this session's
    run("add", "--kind", "note", "--type", "project", "--body", "w", store_dir=tmp_path)
    run("commit", store_dir=tmp_path)            # the store's own first files
    run("add", "--kind", "note", "--type", "project", "--body", "x", "--author", "ada",
        store_dir=tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "measure.py").write_text("print(1)\n")
    capsys.readouterr()
    assert run("commit", store_dir=tmp_path) == 0
    assert "committed: entries ada 1; 1 file: tools/measure.py\n" in capsys.readouterr().out
    for name in ("a ä.md", "b.md", "c.md", "d.md"):
        (tmp_path / "tools" / name).write_text("plan\n")
    assert run("commit", store_dir=tmp_path) == 0
    assert ("committed: 4 files: tools/a ä.md, tools/b.md, tools/c.md, +1 more\n"
            in capsys.readouterr().out)


def test_commit_names_arcs_jsonl_as_its_dry_run_does(tmp_path, capsys, monkeypatch):
    """The dry run listed `arcs.jsonl` and the commit left it out, so the arc
    a session created went unnamed in the line it read after (2026-10-15)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")   # every row is this session's
    run("add", "--kind", "note", "--type", "project", "--body", "w", store_dir=tmp_path)
    run("commit", store_dir=tmp_path)
    run("arc", "create", "--name", "a", "--scope", "item", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "would commit: 1 file\n  arcs.jsonl\n"
    assert run("commit", store_dir=tmp_path) == 0
    assert capsys.readouterr().out.startswith("committed: 1 file: arcs.jsonl\n")

def test_commit_dry_run_lists_what_commit_would_take_and_takes_nothing(tmp_path, capsys, monkeypatch):
    """A careful commit read `git diff` by hand first, since `commit` took
    every writer's pending rows (dogfood, 2026-10-06)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")   # every row is this session's
    run("add", "--kind", "note", "--type", "project", "--body", "w", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    out = capsys.readouterr().out                 # before the first commit
    assert out.startswith("would commit: entries ") and "note  w\n" in out, out
    run("commit", store_dir=tmp_path)
    run("add", "--kind", "bug", "--type", "project", "--body", "**Login fails.** detail",
        "--author", "ada", store_dir=tmp_path)
    run("add", "--kind", "note", "--type", "project", "--body", "x", "--author", "codex",
        store_dir=tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "measure.py").write_text("print(1)\n")
    ada, codex = (n.id for n in store.newest_first(store.load(tmp_path))[1::-1])
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout
    capsys.readouterr()
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == (
        "would commit: entries ada 1, codex 1; 1 file\n"
        f"  {ada[-10:]}  ada  bug  Login fails. detail\n"
        f"  {codex[-10:]}  codex  note  x\n"
        "  tools/measure.py\n")
    assert subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == head
    run("commit", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "nothing to commit\n"


def test_a_write_ends_naming_whose_rows_commit_would_take(tmp_path, capsys, monkeypatch):
    """`add` wrote two rows and said nothing of their being uncommitted: the
    session found them by `git status` in the store at its end (dogfood,
    2026-10-11). Each write ends as `commit` does, with the command, and
    names whose rows they are."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")   # every row is this session's
    pending = re.compile(r"note: (.*) not committed \((.*)\): symbion --dir \S+ commit\n\Z")
    run("add", "--kind", "bug", "--type", "project", "--body", "b", "--author", "ada",
        store_dir=tmp_path)
    assert pending.search(capsys.readouterr().err).groups() == ("1 entry", "ada 1")
    nid = store.load(tmp_path)[0].id
    run("supersede", nid, "--body", "c", "--author", "codex", store_dir=tmp_path)
    assert pending.search(capsys.readouterr().err).groups() == ("1 entry", "ada 1, codex 1")
    _commit(tmp_path, capsys)
    run("resolve", nid, "--author", "ada", store_dir=tmp_path)
    assert pending.search(capsys.readouterr().err).groups() == ("1 entry", "ada 1")


def test_an_entry_edited_twice_before_a_commit_counts_once(tmp_path, capsys):
    """Two supersedes of a cited entry in one session read as two entries
    with one lead, in the write's note and the dry run (2026-10-14): the
    version the second edit kept, because another entry cites it, is a
    version of the entry, not a second one. An entry counts toward the
    author of its newest pending version."""
    run("add", "note", "--target", "item:a", "--body", "lead", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("add", "note", "--target", "item:b", "--body", f"cites {nid[-10:]}", store_dir=tmp_path)
    run("commit", "--all", store_dir=tmp_path)
    run("supersede", nid, "--append", "--body", "one", store_dir=tmp_path)
    capsys.readouterr()
    run("supersede", nid, "--append", "--body", "two", store_dir=tmp_path)
    assert "note: 1 entry not committed (user 1)" in capsys.readouterr().err
    assert len(store.pending_rows(tmp_path)) == 2, "the kept version is pending too"
    assert gitref.uncommitted(tmp_path) == (1, False)
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert re.fullmatch(r"would commit: entries user 1\n  \S+  user  note  lead one two\n", out), out
    assert run("commit", store_dir=tmp_path) == 0
    assert capsys.readouterr().out.startswith("committed: entries user 1\n")


def test_a_revision_names_the_base_its_commit_takes(tmp_path, capsys, monkeypatch):
    """A commit takes a revision's pending base, another session's too: a
    commit must not hold a revision of a row it lacks. The write's note and
    the dry run named that base as left, and the commit then took it
    (2026-10-13)."""
    run("add", "--kind", "note", "--type", "project", "--body", "w", store_dir=tmp_path)
    run("commit", "--all", store_dir=tmp_path)
    capsys.readouterr()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    run("add", "question", "--target", "project", "--body", "q", store_dir=tmp_path)
    q = capsys.readouterr().out.strip()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    run("resolve", q, "--body", "answer", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert "1 entry not committed" in err and "it leaves" not in err, err
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert out.startswith("would commit: entries user 1\n") and "would leave" not in out, out


def test_commit_takes_this_sessions_rows_and_names_the_ones_it_leaves(
        tmp_path, capsys, monkeypatch):
    """A commit took 9 of a live session's rows under a message about one
    (2026-10-09). It takes this session's rows and every file, says
    whose rows it left and how old the newest is, and `--all` takes them."""
    run("add", "--kind", "note", "--type", "project", "--body", "w", store_dir=tmp_path)
    run("commit", "--all", store_dir=tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    run("add", "--kind", "note", "--type", "project", "--body", "theirs", "--author", "claude",
        store_dir=tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    run("add", "--kind", "note", "--type", "project", "--body", "mine", "--author", "claude",
        store_dir=tmp_path)
    run("add", "--kind", "idea", "--type", "project", "--body", "as ada", "--author", "ada",
        store_dir=tmp_path)
    assert re.search(r"note: 2 entries not committed \(ada 1, claude 1\): symbion --dir \S+ "
                     r"commit; it leaves 1 entry of other sessions \(claude 1\)\n",
                     capsys.readouterr().err)
    (tmp_path / "plan.md").write_text("plan\n")
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert out.startswith("would commit: entries ada 1, claude 1; 1 file\n"), out
    assert re.search(r"would leave, other sessions': entries claude 1\n  \S+  claude  note  theirs\n\Z",
                     out), out
    assert run("commit", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "committed: entries ada 1, claude 1; 1 file: plan.md\n" in out, out
    assert re.search(r"left pending, other sessions': entries claude 1, the newest written just now: "
                     r"symbion --dir \S+ commit --all takes them\n", out), out
    assert [r["body"] for r in store.pending_rows(tmp_path)] == ["theirs"]
    # `symbion commit`, named beside rows that are all another session's,
    # commits none of them (2026-10-12).
    run("summary", store_dir=tmp_path)
    assert re.search(r"1 entry not yet in the store's git \(symbion --dir \S+ commit --all once "
                     r"their session has ended\)\n", capsys.readouterr().out)
    assert run("commit", store_dir=tmp_path) == 1
    assert capsys.readouterr().out.startswith("nothing to commit\nleft pending")
    assert run("commit", "--all", store_dir=tmp_path) == 0
    assert store.pending_rows(tmp_path) == []


def test_commit_on_a_store_with_no_remote_says_it_is_on_one_disk(tmp_path, capsys):
    """The other direction, which printed only `committed` -- the same as a
    backed-up store (2026-09-23). No push
    command either: there is nowhere to push. Said on 'nothing to commit'
    too, so it reports the state, not the change."""
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "no remote: this store exists on one disk" in out and "push" not in out, out
    assert run("commit", store_dir=tmp_path) == 1
    assert "no remote" in capsys.readouterr().out


def test_commit_on_a_local_only_store_says_nothing_of_a_remote(tmp_path, capsys):
    """A store kept on one disk on purpose read the nag at every commit
    (2026-10-03). `local_only = true` quiets it; push still
    refuses, since there is nowhere to push."""
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    (tmp_path / "symbion.toml").write_text("local_only = true\n")
    capsys.readouterr()
    assert run("commit", store_dir=tmp_path) == 0
    assert "no remote" not in capsys.readouterr().out
    assert run("push", store_dir=tmp_path) == 1


def test_commit_a_hook_refuses_says_so_with_the_hooks_words(tmp_path, capsys,
                                                             refusing_hook):
    """It printed 'nothing to commit' and dropped the hook's output, so a
    secret scanner's catch read as an idle store (2026-09-28)."""
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    words = refusing_hook(tmp_path)
    capsys.readouterr()
    assert run("commit", store_dir=tmp_path) == 1
    out, err = capsys.readouterr()
    assert words in err and "refused" in err, err
    assert "nothing to commit" not in out, out


def test_commit_names_the_archive_paths_git_ignores(tmp_path, capsys):
    """adoption.md archives a scratch directory verbatim, and such a
    directory can carry its own `.gitignore` of `*`. The copy brings it
    along, `git add -A` stages none of the tree, and the step's own `cmp`
    passes against the working copy while the store's git holds nothing
    (2026-09-28). archive/ exists to be committed, so an ignored path there
    is the bug. Reported as STATE: it fires again on the next commit while
    the paths are still ignored, and goes quiet only once they are in."""
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    scratch = tmp_path / "archive" / "sdd"
    scratch.mkdir(parents=True)
    (scratch / ".gitignore").write_text("*\n")
    (scratch / "plan.md").write_text("the plan\n")
    capsys.readouterr()

    # The dry run lists what commit takes, and said nothing of these (2026-10-13).
    assert run("commit", "--dry-run", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "so this commit would not hold them: " in err and "archive/sdd/plan.md" in err, err
    assert run("commit", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "archive/sdd/plan.md" in err and "add -f archive" in err, err
    assert run("commit", store_dir=tmp_path) == 1        # nothing changed
    assert "archive/sdd/plan.md" in capsys.readouterr().err, "state, not change"

    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "archive"], check=True)
    assert run("commit", store_dir=tmp_path) == 0
    assert "gitignored" not in capsys.readouterr().err


# ---- --dir names the store completely; the cwd need not be a git repo ----
def _store_with_a_check_and_a_note(tmp_path):
    """Written from inside the repo fixture, so the check carries a real
    provenance sha. The system temp dir sits in no git repo, so a chdir
    to tmp_path/elsewhere is a cwd outside any repository."""
    store_dir = tmp_path / "s"
    run("add", "--kind", "check", "--type", "project", "--checked", "suite",
        "--result", "ok", store_dir=store_dir)
    run("add", "--kind", "note", "--type", "project", "--body", "b", store_dir=store_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return store_dir, elsewhere


def test_dir_read_outside_any_git_repo_degrades_instead_of_aborting(repo, tmp_path, monkeypatch, capsys):
    """Measured 2026-09-20 from a cwd outside any repo: the `--dir`
    listings all exited 1 with 'not a git repository'. A read needs the repo
    only for check state, which already degrades to `unverifiable` for a
    departed sha -- a missing repo is the same shape and reads the same."""
    store_dir, elsewhere = _store_with_a_check_and_a_note(tmp_path)
    capsys.readouterr()
    monkeypatch.chdir(elsewhere)

    assert run("list", "--all", "--json", store_dir=store_dir) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    check = next(r for r in rows if r["kind"] == "check")
    assert check["state"] == "unverifiable" and check["distance"] is None

    assert run("list", store_dir=store_dir) == 0
    assert "unverifiable (commit unavailable)" in capsys.readouterr().out
    assert run("summary", store_dir=store_dir) == 0
    assert run("context", "--target", "project:", store_dir=store_dir) == 0


def test_a_verdict_write_outside_any_git_repo_refuses_and_names_the_cwd(repo, tmp_path, monkeypatch, capsys):
    """Provenance is stamped at write; an unstamped check would be a lie, so
    the write refuses rather than storing provenance: null. A plain note
    carries no stamp and goes through -- the other direction."""
    store_dir, elsewhere = _store_with_a_check_and_a_note(tmp_path)
    capsys.readouterr()
    monkeypatch.chdir(elsewhere)

    assert run("add", "--kind", "check", "--type", "project", "--checked", "x",
               "--result", "y", store_dir=store_dir) == 1
    err = capsys.readouterr().err
    assert "git repository" in err and str(elsewhere) in err and "provenance" in err
    assert len(store.load(store_dir)) == 2, "a refused write appends nothing"

    assert run("add", "--kind", "note", "--type", "project", "--body", "c",
               store_dir=store_dir) == 0
    assert len(store.load(store_dir)) == 3


@pytest.mark.parametrize("shape", ["fresh repo", "orphan branch"])
def test_a_verdict_write_on_an_unborn_branch_refuses_and_names_it(repo, tmp_path, monkeypatch,
                                                                   capsys, shape):
    """`git rev-parse HEAD` on a branch with no commits prints `HEAD` and exits
    128. That string was stored as the sha: the row read `unverifiable`, then
    `behind 0` for good once commits existed. Both shapes of an unborn
    branch; an external check and a plain note need no commit."""
    if shape == "fresh repo":
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        _git(fresh, "init", "-q", "-b", "new")
        monkeypatch.chdir(fresh)
    else:
        _git(repo, "checkout", "-q", "--orphan", "new")
    store_dir = tmp_path / "s"
    assert run("add", "check", "--target", "project", "--checked", "x", "--result", "y",
               store_dir=store_dir) == 1
    err = capsys.readouterr().err
    assert "no commits yet on new" in err and "--external" in err, err
    assert store.load(store_dir) == [], "a refused write appends nothing"

    assert run("add", "check", "--target", "item:dns", "--external", "--checked", "dig",
               "--result", "ok", store_dir=store_dir) == 0
    assert run("add", "note", "--target", "project", "--body", "b", store_dir=store_dir) == 0
    assert len(store.load(store_dir)) == 2



def test_an_external_check_needs_no_repo(repo, tmp_path, monkeypatch, capsys):
    """The refusal above is for a tree stamp. An external check has no tree
    to stamp from, so it writes from anywhere."""
    store_dir, elsewhere = _store_with_a_check_and_a_note(tmp_path)
    capsys.readouterr()
    monkeypatch.chdir(elsewhere)
    assert run("add", "check", "--target", "item:dns", "--external", "--checked", "dig",
               "--result", "ok", store_dir=store_dir) == 0
    nid = capsys.readouterr().out.strip()
    assert store.load(store_dir)[-1].id == nid


def test_an_external_check_lists_its_age_not_a_tree_state(repo, tmp_path, capsys):
    run("add", "check", "--target", "item:dns", "--external", "--checked", "dig",
        "--result", "ok", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", store_dir=tmp_path) == 0
    assert "state=external (<1d ago)" in capsys.readouterr().out
    run("list", "--json", store_dir=tmp_path)
    row = json.loads(capsys.readouterr().out)[0]
    assert (row["state"], row["distance"]) == ("external", None)

    store.add(tmp_path, kind="check", target={"type": "item", "name": "old"}, author="t",
              checked="x", result="y",
              provenance={"external": True, "at": "2026-01-01T00:00:00+00:00"})
    run("list", "--name", "old", store_dir=tmp_path)
    days = summ.age_days("2026-01-01T00:00:00+00:00")
    assert f"state=external ({days}d ago)" in capsys.readouterr().out


def test_supersede_takes_external_only_where_the_row_already_has_it(repo, tmp_path, capsys):
    """A supersede keeps the row's stamp, so `--external` copied from the
    `add` that wrote the row failed as an unrecognized argument (2026-10-06).
    On an external row it changes nothing; on a commit-stamped one it refuses,
    naming the verb that makes an external reading."""
    ext = _add(tmp_path, capsys, "check", "--target", "item:dns", "--external",
               "--checked", "dig", "--result", "ok")
    assert run("supersede", ext, "--external", "--result", "ok, re-read",
               store_dir=tmp_path) == 0
    capsys.readouterr()
    sha = _add(tmp_path, capsys, "check", "--target", "item:t", "--checked", "pytest",
               "--result", "ok")
    assert run("supersede", sha, "--external", "--result", "x", store_dir=tmp_path) == 1
    assert "add --external" in capsys.readouterr().err
    rows = {n.id: n for n in store.load(tmp_path)}
    assert (rows[ext].result, rows[ext].provenance["external"]) == ("ok, re-read", True)
    assert rows[sha].result == "ok" and len(rows) == 2


def test_from_json_takes_external(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "check", "target": {"type": "item", "name": "dns"}, "external": true}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    assert store.load(tmp_path)[0].provenance["external"] is True

def test_init_says_whether_the_session_start_hook_is_registered(capsys):
    """A re-run printed a line for the skill link and nothing for the hook, so
    "hook checked and present" and "hook not checked" read the same (adopter
    report, 2026-09-28). And presence was a substring match over the whole
    file, which a command under another event key -- or the path quoted in a
    note -- satisfies. Three states, each asserted: registered, not
    registered, and unknown because the file is not readable JSON."""
    settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    entry = cli._hook_entry(cli._HOOK_COMMAND)
    settings.write_text(json.dumps({"hooks": {"SessionStart": [entry]}}))
    cli._link_user_skill()
    out = capsys.readouterr().out
    assert f"kept hook in {settings}" in out, out
    elsewhere = {"hooks": {"PreToolUse": [entry]}}
    settings.write_text(json.dumps(elsewhere))
    cli._link_user_skill()
    out = capsys.readouterr().out
    assert "does not register the symbion SessionStart hook" in out, out
    assert "kept hook" not in out, "the command is there, the SessionStart hook is not"
    settings.write_text("{ not json")
    cli._link_user_skill()
    out = capsys.readouterr().out
    assert "unknown" in out and "cannot be read" in out, out
    assert "does not register" not in out, "unreadable is not the negative case"


def test_the_printed_hook_entry_appends_beside_other_session_start_hooks(capsys):
    """init printed a whole `{"hooks": {"SessionStart": [...]}}` object. Pasted
    into a file that already has SessionStart hooks, it replaces them or is
    not JSON. Do what the message says, and the hook is registered and the
    other hook kept."""
    settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    other = {"matcher": "startup", "hooks": [{"type": "command", "command": "other-status"}]}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [other]}}))
    cli._link_user_skill()
    out = capsys.readouterr().out
    assert "append" in out and "SessionStart" in out, out
    entry, _ = json.JSONDecoder().raw_decode(out, out.index("{"))
    data = json.loads(settings.read_text())
    data["hooks"]["SessionStart"].append(entry)
    settings.write_text(json.dumps(data))
    cli._link_user_skill()
    assert f"kept hook in {settings}" in capsys.readouterr().out
    assert json.loads(settings.read_text())["hooks"]["SessionStart"][0] == other


def test_init_outside_any_git_repo_refuses(tmp_path, monkeypatch, capsys):
    """init installs files beside the project, and there is no project here
    to put them beside."""
    monkeypatch.chdir(tmp_path)
    assert run("init", "--yes", store_dir=tmp_path / "s") == 1
    assert "git repository" in capsys.readouterr().err
    assert not (tmp_path / "s" / "symbion.toml").exists()


# ---- list is bounded: a page is never mistaken for the whole ----
_ID_AT_END = re.compile(r"  \d{8}-\d{6}-\d{6}-[0-9a-f]{3}$")


def _add_notes(store_dir, n):
    for i in range(n):
        run("add", "--kind", "note", "--type", "item", "--name", f"t{i}",
            "--body", f"body {i}", store_dir=store_dir)


def test_list_is_bounded_by_default_and_the_header_says_so(tmp_path, capsys):
    """Hundreds of lines on one store (2026-09-21): an agent running it bare
    got a mess. Line 1 carries the total and what is shown, so a page never
    reads as the whole; --limit 0 lifts it. The created_at prefix is gone --
    the id IS the timestamp, and the row printed the same fact twice."""
    n = summ.LIST_CAP + 5
    _add_notes(tmp_path, n)
    capsys.readouterr()
    assert run("list", store_dir=tmp_path) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"{n} entries, showing {summ.LIST_CAP} newest"), lines[0]
    assert "--limit" in lines[0]
    assert len([l for l in lines[1:] if _ID_AT_END.search(l)]) == summ.LIST_CAP
    assert not re.match(r"20\d\d-\d\d-\d\dT", lines[1]), lines[1]
    assert f"    body {n - 1}" in lines and "    body 0" not in lines, "newest first"

    assert run("list", "--limit", "0", store_dir=tmp_path) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{n} entries"
    assert len([l for l in lines[1:] if _ID_AT_END.search(l)]) == n


def test_list_json_is_whole_unless_limit_is_given(tmp_path, capsys):
    """The array is the agent's default and every consumer indexes it whole;
    a silent default cap there would be a 0 that reads as 'nothing exists'.
    An explicit --limit caps it and says so on stderr, never in the array."""
    n = summ.LIST_CAP + 5
    _add_notes(tmp_path, n)
    capsys.readouterr()
    assert run("list", "--json", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert len(json.loads(out)) == n and err == ""
    assert run("list", "--json", "--limit", "5", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert len(json.loads(out)) == 5
    assert f"showing 5 of {n}" in err


def test_list_names_what_the_view_hides(tmp_path, capsys):
    """The rule: a 0 must never read as 'nothing exists' when it means
    'nothing matches the filter'. Each hidden set is a count plus the flag
    that shows it."""
    for i in range(3):
        run("add", "--kind", "bug", "--type", "item", "--name", f"b{i}", store_dir=tmp_path)
    ids = capsys.readouterr().out.split()
    _commit(tmp_path, capsys)
    run("resolve", ids[0], store_dir=tmp_path)
    run("supersede", ids[1], "--body", "edited", store_dir=tmp_path)
    capsys.readouterr()

    run("list", "--status", "open", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head.startswith("2 of 3 match --status open;"), head
    assert "+1 resolved (--status resolved)" in head and "+2 superseded (--all)" in head

    run("list", "--status", "resolved", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head.startswith("1 of 3 match --status resolved;") and "+2 open (--status open)" in head, head

    run("list", "--all", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head == "5 entries; --all: 2 superseded included", head


def test_list_clips_bodies_to_one_line_unless_full(tmp_path, capsys):
    """One clipped line per body by default; --full and --id print it as
    stored. context keeps printing whole bodies: it is pull-based detail."""
    body = "first paragraph " * 20 + "\n\nsecond paragraph"
    run("add", "--kind", "note", "--type", "project", "--body", body, store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()

    run("list", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "second paragraph" not in out and "…" in out
    run("list", "--full", store_dir=tmp_path)
    assert "second paragraph" in capsys.readouterr().out
    run("list", "--id", nid, store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "second paragraph" in out and not out.startswith("1 entry"), "--id is the entry itself, no header"
    run("context", "--target", "project:", store_dir=tmp_path)
    assert "second paragraph" in capsys.readouterr().out


def test_list_zero_names_the_denominator_and_the_filters(tmp_path, capsys):
    """`0 rows` against a full store read as 0-of-0 (measured 2026-09-22). A
    zero names what was scanned and every filter that dropped rows, spelled
    as flags so the line pastes back; the same head with matches says N of M."""
    for i in range(3):
        run("add", "--kind", "bug", "--type", "item", "--name", f"b{i}", store_dir=tmp_path)
    run("add", "--kind", "task", "--type", "item", "--name", "sp ace", store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--kind", "bug", "--tag", "no-such-tag", store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert out == "0 of 4 match --kind bug --tag no-such-tag\n", out
    run("list", "--kind", "bug", store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[0] == "3 of 4 match --kind bug"
    run("list", "--name", "sp ace", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head == "1 of 4 match --name 'sp ace'", head
    run("list", store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[0] == "4 entries", "no filter, no denominator"


def test_list_all_echoes_that_it_included_nothing(tmp_path, capsys):
    """`--all` on a store with no superseded rows printed a header
    byte-identical to bare `list`, so the flag read as dropped (a widening
    flag that changed nothing says so)."""
    run("add", "--kind", "bug", "--type", "project", store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    bare = capsys.readouterr().out.splitlines()[0]
    run("list", "--all", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head != bare and head == "1 entry; --all: 0 superseded included", head


def test_arc_todo_names_the_open_count_and_the_resolved_set(tmp_path, capsys):
    """A finished arc printed nothing at exit 0 (measured 2026-09-22), the
    same as an arc nobody started, and nothing said the resolved rows
    exist. A count with its denominator, and the hidden set named with the
    command that shows it.
    --json stays a bare array: the consumer contract shared with list."""
    run("arc", "create", "--name", "A", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    assert run("arc", "todo", aid, store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "0 open of 0 items\n"
    for i in range(2):
        run("add", "--kind", "task", "--type", "item", "--name", f"i{i}", "--arc-id", aid,
            store_dir=tmp_path)
    ids = capsys.readouterr().out.split()
    run("resolve", ids[0], store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"1 open of 2 items; +1 resolved (list --arc {aid})", lines[0]
    assert lines[1].endswith(ids[1]) and len(lines) == 2
    run("resolve", ids[1], store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "todo", aid, store_dir=tmp_path)
    assert capsys.readouterr().out == f"0 open of 2 items; +2 resolved (list --arc {aid})\n"
    run("arc", "todo", aid, "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out) == []


def test_arc_todo_lines_carry_kind_and_body_like_session_start(tmp_path, capsys):
    """A hand-written checklist puts many boxes on one target, so a line of
    target and id alone told a reader nothing about which box it was, and a
    project target printed as `project:None`. Each line is the session-start
    head line: kind, target, clipped body, id."""
    run("arc", "create", "--name", "A", "--scope", "item", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    run("add", "--kind", "task", "--type", "project", "--arc-id", aid,
        "--body", "write the **README**\n\nthen the rest", store_dir=tmp_path)
    run("add", "--kind", "bug", "--type", "item", "--name", "x", "--arc-id", aid,
        store_dir=tmp_path)
    ids = capsys.readouterr().out.split()
    run("arc", "todo", aid, store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == f"[task] project:  write the README then the rest  {ids[0]}", lines[1]
    assert lines[2] == f"[bug] item:x    {ids[1]}", lines[2]


def test_context_names_its_count_and_what_it_is_for(tmp_path, capsys):
    """`context --target X` on a target with no notes printed nothing on
    stdout (2026-09-22). The first line carries the count and the subject;
    the bare view names the heads it leaves out with the verb that lists
    them, so its 0 never reads as an empty store."""
    store.ensure_store(tmp_path)
    assert run("context", "--target", "item:no/such.py", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "0 entries for item:no/such.py\n"
    run("add", "--kind", "note", "--type", "item", "--name", "a.py", "--body", "b", store_dir=tmp_path)
    run("add", "--kind", "bug", "--type", "item", "--name", "a.py", "--body", "c", store_dir=tmp_path)
    ids = capsys.readouterr().out.split()
    run("resolve", ids[1], store_dir=tmp_path)
    capsys.readouterr()
    run("context", "--target", "item:a.py", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "2 entries for item:a.py", lines[0]
    run("context", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "1 entry; +1 other entry (list)", lines[0]
    run("context", "--json", store_dir=tmp_path)
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"store", "gui", "notes"}, "no header in json"
    assert data["store"] == str(tmp_path)


def test_context_names_the_target_it_resolved(repo, tmp_path, capsys):
    """`context --target doc:parser-design.md` headed its page with the name
    as typed, over an entry whose ref reads doc:docs/parser-design.md (a
    fresh agent, 2026-10-14). The header and --json give the stored name."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "docs").mkdir(exist_ok=True)
    (store_dir / "docs" / "parser-design.md").write_text("# design\n")
    assert run("add", "note", "--target", "item:x", "--ref", "doc:parser-design.md",
               "--body", "b", store_dir=store_dir) == 0
    capsys.readouterr()
    run("context", "--target", "doc:parser", store_dir=store_dir)
    assert capsys.readouterr().out.splitlines()[0] == "1 entry for doc:parser-design.md"
    run("context", "--target", "doc:parser", "--json", store_dir=store_dir)
    assert json.loads(capsys.readouterr().out)["target"] == "doc:parser-design.md"


def test_a_list_filter_on_context_names_the_list_command(tmp_path, capsys):
    """`context --target item:X --status open` exited 2, unrecognized: asked
    what is open on one object, an agent took `context`, the verb SKILL.md
    names for one object (dogfood, 2026-10-15). The refusal gives `list`
    with the same words; a flag list lacks gets no such line."""
    store.ensure_store(tmp_path)
    assert run("context", "--target", "item:a b", "--status", "open", "--kind", "bug",
               store_dir=tmp_path) == 2
    assert ("`list --target 'item:a b' --status open --kind bug` filters them"
            in capsys.readouterr().err)
    assert run("context", "--target", "item:x", "--bogus", store_dir=tmp_path) == 2
    assert "list --target" not in capsys.readouterr().err


def test_list_and_arc_list_on_an_empty_store_name_the_next_verb(tmp_path, capsys):
    """`0 rows` and a silent `arc list` on a fresh store (2026-09-22): the
    empty state names the verb that fills it, once, while it is empty."""
    store.ensure_store(tmp_path)
    assert run("list", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == f"0 entries; {summ.FIRST_CONTACT}\n"
    assert run("arc", "list", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "0 arcs (arc create --name NAME --scope item|file|mixed|project)\n"
    run("add", "--kind", "task", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[0] == "1 entry"


# ---- a named store resolves in ITS project, not the cwd's ----
@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """`own` with its sibling store `own-notes`, beside a second repo `other`,
    cwd `other`. The trees hold different .py files, so a catalog's answer
    says which tree computed it. Measured 2026-09-23: `--dir ../own-notes
    add` from `other` ran other's catalog."""
    def mk(name):
        r = tmp_path / name
        r.mkdir()
        _git(r, "init", "-q", "-b", "main")
        (r / f"{name}_only.py").write_text("x")
        _git(r, "add", "-A")
        _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
        return r
    own, other = mk("own"), mk("other")
    s = tmp_path / "own-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text("[catalogs]\nfile = \"git ls-files '*.py'\"\n")
    monkeypatch.chdir(other)
    return own, other, s


def _head(r):
    return subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_a_sibling_store_named_from_another_repo_resolves_in_its_own_project(two_projects, capsys):
    own, other, s = two_projects
    assert run("arc", "seed", "--scope", "file", "--dry-run", store_dir=s) == 0
    out, err = capsys.readouterr()
    assert "own_only.py" in out and "other_only.py" not in out, out
    assert str(own) in err and str(other) in err, "say which repo computed it"
    assert run("add", "--kind", "check", "--type", "project", "--checked", "x",
               "--result", "y", store_dir=s) == 0
    assert store.load(s)[0].provenance["sha"] == _head(own) != _head(other)


def test_a_sibling_store_named_from_outside_any_repo_resolves_in_its_project(two_projects, tmp_path, monkeypatch, capsys):
    """From outside any repo this degraded to `unverifiable`: with a project to find, find it."""
    own, _, s = two_projects
    monkeypatch.chdir(tmp_path)
    assert run("arc", "seed", "--scope", "file", "--dry-run", store_dir=s) == 0
    assert "own_only.py" in capsys.readouterr().out


def test_a_repo_that_points_at_the_store_keeps_its_own_tree(two_projects, capsys):
    """`other` shares own's store on purpose: its `.symbion` says so."""
    own, other, s = two_projects
    (other / ".symbion").write_text("../own-notes\n")
    assert run("arc", "seed", "--scope", "file", "--dry-run", store_dir=s) == 0
    out, err = capsys.readouterr()
    assert "other_only.py" in out and "own_only.py" not in out, out
    assert err == ""


def test_a_store_that_names_no_project_resolves_in_the_cwd(two_projects, tmp_path, capsys):
    """A scratch store probing this tree's catalog: nothing to follow."""
    _, _, s = two_projects
    scratch = tmp_path / "scratch"
    store.ensure_store(scratch)
    (scratch / "symbion.toml").write_text((s / "symbion.toml").read_text())
    assert run("arc", "seed", "--scope", "file", "--dry-run", store_dir=scratch) == 0
    out, err = capsys.readouterr()
    assert "other_only.py" in out and err == ""


def test_init_with_a_foreign_store_installs_in_the_cwd_and_points_there(two_projects):
    """`--dir X init` is how a project is pointed at a store it shares, so
    init never follows the store to its owner."""
    own, other, s = two_projects
    assert run("init", "--yes", store_dir=s) == 0
    assert not (other / ".claude").exists(), "init writes no agent files into a project"
    assert not (own / ".claude").exists()
    assert (other / ".symbion").read_text().strip() == "../own-notes"


def test_init_that_repoints_a_project_says_what_it_replaced(two_projects, capsys):
    """Re-pointing a project's `.symbion` leaves the old store unread from
    there; the line said `wrote .symbion -> X` and not what X replaced. A
    pointer to a store that is gone is broken already, so no --repoint."""
    own, other, s = two_projects
    (other / ".symbion").write_text("../elsewhere-notes\n")
    assert run("init", "--yes", store_dir=s) == 0
    gone = other.parent.resolve() / "elsewhere-notes"
    assert f"(was {gone}, where no store is)" in capsys.readouterr().out
    assert (other / ".symbion").read_text() == "../own-notes\n"


@pytest.mark.parametrize("argv", [["list"], ["schema"], ["arc", "list"], ["tags"],
                                  ["context"], ["arc", "todo", "a"]])
def test_a_read_on_an_absent_store_names_it_rather_than_the_verb_that_forks_it(tmp_path, capsys, argv):
    """An absent store is never first contact: init creates it. A mistyped
    --dir, a renamed repo, a cwd inside a store all land here, and `0 rows;
    no notes yet: symbion add` sent the reader to the write that starts a
    second store (2026-09-23). An error, so exit 1 on stderr:
    summary alone keeps stdout and 0, for the hook."""
    nowhere = tmp_path / "nowhere"
    assert run(*argv, store_dir=nowhere) == 1
    out, err = capsys.readouterr()
    assert (out, err) == ("", f"symbion: no store at {nowhere}; run `symbion init`\n")
    assert not nowhere.exists()


def test_a_renamed_repo_is_shown_the_store_no_project_names(repo, tmp_path, capsys):
    """A repo renamed without a `.symbion` reads `no store at <new>-notes;
    run symbion init`, and init starts a second store. Its old store sits
    beside it with no project of its name. A store some sibling's pointer
    names is claimed, though its name matches nothing (`claimed-notes`)."""
    store.ensure_store(tmp_path / "old-notes")
    store.ensure_store(tmp_path / "claimed-notes")
    (tmp_path / "plain-notes").mkdir()                   # not a store
    q = tmp_path / "q"
    q.mkdir()
    _git(q, "init", "-q", "-b", "main")
    (q / ".symbion").write_text("../claimed-notes\n")
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert err == (f"symbion: no store at {tmp_path.resolve() / 'p-notes'}; run `symbion init`\n"
                   "note: if this repo was renamed, its store may be ../old-notes, which no "
                   "project names: `echo ../old-notes > .symbion` points this repo at it, "
                   "instead of init\n"), err
    shutil.rmtree(tmp_path / "old-notes")
    assert cli.main(["list"]) == 1
    assert "note:" not in capsys.readouterr().err, "no orphan, no note"


@pytest.mark.parametrize("argv, code", [
    (["context", "--target", "widget:x", "--json"], 0),
    (["list", "--type", "widget"], 1),
    (["list", "--kind", "prediction", "--json"], 0),
    (["add", "prediction", "--target", "widget:x"], 1),
    (["arc", "create", "--name", "x", "--scope", "widget"], 1),
])
def test_an_absent_store_is_named_before_a_type_or_kind_its_config_would_declare(
        tmp_path, capsys, argv, code):
    """The types, kinds and scopes past the built-in ones are declared in the
    store's symbion.toml, so with the store absent, argparse refused a catalog
    type as `invalid type 'widget'`, and a client that shows stderr told its
    user the type was wrong (2026-10-03). The missing store is the error."""
    nowhere = tmp_path / "nowhere"
    assert cli.main(["--dir", str(nowhere), *argv]) == code
    err = capsys.readouterr().err
    assert err == f"symbion: no store at {nowhere}; run `symbion init`\n", err


def test_the_summary_header_counts_what_the_arcs_hold(tmp_path, capsys):
    """A cold bootstrap filed its one known bug in an arc, and the header
    read `bug 0`: correct under "outside arcs", read as bug-free at a
    glance (2026-09-27). The header says how many open rows the arcs hold."""
    run("arc", "create", "--name", "Cache", store_dir=tmp_path)
    aid = capsys.readouterr().out.strip()
    run("add", "bug", "--target", "item:x", "--arc", aid, store_dir=tmp_path)
    run("add", "task", "--target", "item:y", "--arc", aid, store_dir=tmp_path)
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    # A --dir store is labelled (test_summary_names_a_store_...); the counts
    # are this test's claim.
    assert head.endswith(": open outside arcs: bug 0, task 0, question 0, prediction 0; "
                         "2 open in arcs"), head


def test_the_summary_header_counts_parked_entries(tmp_path, capsys):
    """After parking a task as an idea, fresh agents found no sign in the
    summary that a parked entry exists until its date (2026-10-14). The
    user: a count is enough. A resolved one is not counted."""
    for body in ("a", "b", "c"):
        run("add", "idea", "--target", "project", "--body", body, store_dir=tmp_path)
    run("resolve", capsys.readouterr().out.split()[-1], store_dir=tmp_path)
    capsys.readouterr()
    run("summary", store_dir=tmp_path)
    head = capsys.readouterr().out.splitlines()[0]
    assert head.endswith(": open outside arcs: bug 0, task 0, question 0, prediction 0; "
                         "2 parked"), head
    run("summary", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)["parked"] == 2


def test_summary_says_nothing_when_the_store_is_the_one_the_tree_names(repo, capsys):
    """The label is for a store that was NAMED. The ordinary case -- standing
    in the project, reading its own sibling store -- must stay bare, or every
    session start in every project pays for the multi-store reader."""
    store.ensure_store(repo.parent / "p-notes")
    store.add(repo.parent / "p-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")
    assert cli.main(["summary"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith(
        "symbion: open outside arcs:")


def test_summary_names_a_store_the_cwd_tree_does_not(repo, tmp_path, capsys):
    """A hub's session reads its satellite's store beside its own, and the two
    summaries were byte-identical in their headers: nothing said which project
    either was about (2026-09-28). The header carries the flag that reaches
    it, since whoever reads the second summary must also write to it."""
    store.ensure_store(tmp_path / "sat-notes")
    store.add(tmp_path / "sat-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")
    assert run("summary", store_dir=tmp_path / "sat-notes") == 0
    assert capsys.readouterr().out.splitlines()[0].startswith(
        "symbion --dir ../sat-notes: open outside arcs:")


def test_a_named_store_is_spelled_from_the_cwd(repo, tmp_path, capsys, monkeypatch):
    """From a subdirectory the header read `--dir ../sat-notes`, which names
    nothing there (2026-10-13). `../NAME` only where it reaches the store."""
    store.ensure_store(tmp_path / "sat-notes")
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert run("summary", store_dir=tmp_path / "sat-notes") == 0
    head = capsys.readouterr().out.splitlines()[0]
    assert head.startswith(f"symbion --dir {(tmp_path / 'sat-notes').resolve().as_posix()}:"), head


def test_summary_names_a_store_set_by_the_environment(repo, tmp_path, capsys,
                                                      monkeypatch):
    """SYMBION_DIR is the one pointer the summary's reader cannot see, so the
    label is keyed on what the TREE names, not on config.store_dir -- which
    answers with the env var and would leave this case bare."""
    monkeypatch.setenv("SYMBION_DIR", str(tmp_path / "env-notes"))
    store.ensure_store(tmp_path / "env-notes")
    store.add(tmp_path / "env-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")
    assert cli.main(["summary"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith(
        "symbion --dir ../env-notes: open outside arcs:")


@pytest.mark.parametrize("argv", [
    ["add", "note", "--target", "project", "--body", "b"],
    ["add", "--from-json", "-"],
    ["resolve", "x", "--body", "b"],
    ["supersede", "x", "--body", "b"],
    ["commit"],
    ["rename", "a", "b", "--type", "item"],
    ["arc", "create", "--name", "a", "--scope", "item"],
    ["arc", "seed", "a", "--scope", "item", "--name", "n"],
    ["arc", "rename", "a", "b"],
    ["arc", "archive", "a"],
    ["serve"],
], ids=lambda a: " ".join(a[:2]))
def test_a_write_refuses_a_store_init_never_made(tmp_path, capsys, monkeypatch, argv):
    """A write created a missing store, so a renamed repo or a mistyped --dir
    started a second store and `add` printed an ordinary id (measured
    2026-09-11: two stores, two histories, no warning on either). Only `init`
    creates one now; the refusal names the path and the verb that makes it."""
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "note", "target": {"type": "project"}}\n'))
    fresh = tmp_path / "fresh-notes"
    assert cli.main(["--dir", str(fresh), *argv]) == 1
    assert capsys.readouterr().err == f"symbion: no store at {fresh}; run `symbion init`\n"
    assert not fresh.exists()


def test_no_git_on_path_is_named_not_a_traceback(tmp_path, monkeypatch, capsys):
    """A hook's PATH that held no git (the packages' dir left out, OpenBSD,
    2026-10-05) printed a FileNotFoundError traceback into the session."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cli.main(["--dir", str(tmp_path / "s"), "summary"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: symbion runs git, and no `git` is on PATH") and "Traceback" not in err


def test_the_store_refuses_a_write_the_cli_did_not_screen(tmp_path):
    """The GUI and api write through store, not the CLI's guard: the store's
    own check is what keeps them from starting one."""
    fresh = tmp_path / "fresh-notes"
    ctx = api.resolve(str(fresh))
    with pytest.raises(ValueError, match="no store at .*run `symbion init`"):
        api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "body": "b"}, author="t")
    assert not fresh.exists()
    store.ensure_store(fresh)
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "b"}, author="t")
    assert len(store.load(fresh)) == 1, "the same write lands once init made it"


def test_serve_on_an_absent_store_names_it_and_starts_nothing(tmp_path, capsys):
    """`serve` with no store served an empty page, and the first add there
    created a store beside the one the project uses (the gui spec: print the
    init hint and exit nonzero; spec drift audit, 2026-09-25)."""
    nowhere = tmp_path / "nowhere"
    assert run("serve", "--no-browser", store_dir=nowhere) == 1
    assert capsys.readouterr().err == f"symbion: no store at {nowhere}; run `symbion init`\n"
    assert not nowhere.exists()


def test_a_read_on_an_absent_store_keeps_its_json_shape(tmp_path, capsys):
    run("list", "--json", store_dir=tmp_path / "nowhere")
    assert json.loads(capsys.readouterr().out) == []
    run("arc", "list", "--json", store_dir=tmp_path / "nowhere")
    assert json.loads(capsys.readouterr().out) == []


# ---- a cwd inside a store: the store is the tree, not a project ----
def _bare(*argv):
    """No --dir: resolution comes from the cwd alone."""
    return cli.main(list(argv))


def test_a_cwd_inside_a_store_reads_that_store_in_its_own_project(two_projects, monkeypatch, capsys):
    """Measured 2026-09-23: the store repo was taken as a project, so every
    bare command read `<store>-notes` -- `list` said `0 rows` against a
    populated store, `schema` printed nothing. `cd` into the store to edit
    symbion.toml is what bootstrapping says to do."""
    own, other, s = two_projects
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    run("add", "--kind", "task", "--type", "project", "--body", "x", store_dir=s)
    (s / "archive").mkdir()
    for cwd in (s, s / "archive"):
        monkeypatch.chdir(cwd)
        capsys.readouterr()
        assert _bare("list") == 0
        out, err = capsys.readouterr()
        assert out.splitlines()[0] == "1 entry", out
        # On every read it was noise, so a real mismatch was skipped with it
        # (2026-10-01): a read is right for the store it reads.
        assert "belongs to" not in err, err
        assert _bare("arc", "seed", "--scope", "file", "--dry-run") == 0
        c = capsys.readouterr()
        assert "own_only.py" in c.out and f"{s} belongs to {own}" in c.err, "a catalog run says whose"
    assert _bare("add", "--kind", "check", "--type", "project",
                 "--checked", "c", "--result", "r") == 0
    assert f"{s} belongs to {own}" in capsys.readouterr().err, "a write says whose repo stamps it"
    assert store.load(s)[-1].provenance["sha"] == _head(own)
    # A write the user's repo decides nothing in is as quiet as a read: all
    # 6 writes of one session warned, on `item:` rows and a commit (2026-10-12).
    assert _bare("add", "task", "--target", "file:own_only.py", "--body", "y") == 0
    assert f"{s} belongs to {own}" in capsys.readouterr().err, "a catalog name says whose"
    assert _bare("add", "task", "--target", "item:x", "--body", "y") == 0
    tid = capsys.readouterr().out.strip()
    for argv in (("resolve", tid, "--body", "done"), ("commit", "-m", "m")):
        assert _bare(*argv) == 0, argv
        assert "belongs to" not in capsys.readouterr().err, argv
    assert not (s.parent / "own-notes-notes").exists()


def test_a_cwd_inside_a_store_no_project_claims_runs_no_catalog_there(tmp_path, monkeypatch, capsys):
    """A store reached only by a pointer has no owner to follow. Its repo is
    not a project either: a catalog run there would list the store's files."""
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    s = tmp_path / "shared"
    store.ensure_store(s)
    (s / "symbion.toml").write_text("[catalogs]\nfile = \"git ls-files\"\n")
    _git(s, "add", "-A")
    _git(s, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "s")
    monkeypatch.chdir(s)
    assert _bare("add", "--kind", "task", "--type", "project", "--body", "x") == 0
    capsys.readouterr()
    assert _bare("list") == 0
    assert capsys.readouterr().out.splitlines()[0] == "1 entry"
    assert _bare("arc", "seed", "--scope", "file", "--dry-run") != 0
    assert "notes.jsonl" not in capsys.readouterr().out
    assert not (tmp_path / "shared-notes").exists()


def test_a_project_holding_a_notes_jsonl_is_still_a_project(repo, monkeypatch, capsys):
    """The marker is the PAIR ensure_store writes. `notes.jsonl` alone is a
    common filename; mistaking the project for its store would write rows
    into the project's own file."""
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    (repo / "notes.jsonl").write_text('{"mine": 1}\n')
    store.ensure_store(repo.parent / "p-notes")
    assert _bare("add", "--kind", "task", "--type", "project", "--body", "x") == 0
    assert (repo / "notes.jsonl").read_text() == '{"mine": 1}\n'
    assert len(store.load(repo.parent / "p-notes")) == 1


def test_init_inside_a_store_refuses_and_writes_nothing(two_projects, monkeypatch):
    """init installs into the cwd's project. Inside a store that is the store
    itself: before, it made `own-notes-notes`; unguarded, it would put the
    skill and hook into the store repo."""
    _, _, s = two_projects
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    monkeypatch.chdir(s)
    before = sorted(p.name for p in s.iterdir())
    assert _bare("init", "--yes") != 0
    assert sorted(p.name for p in s.iterdir()) == before
    assert not (s.parent / "own-notes-notes").exists()


# ---- --due ----
def _add(tmp_path, capsys, *a):
    assert run("add", *a, store_dir=tmp_path) == 0
    return capsys.readouterr().out.strip()


def test_list_overdue_is_open_and_past_due_only(repo, tmp_path, capsys):
    """Both directions: the past-due open row is listed; a future one, an
    undated one and a past-due RESOLVED one are not."""
    late = _add(tmp_path, capsys, "--kind", "task", "--type", "item", "--name", "late",
                "--due", "2000-01-01")
    _add(tmp_path, capsys, "--kind", "task", "--type", "item", "--name", "later",
         "--due", "2999-01-01")
    _add(tmp_path, capsys, "--kind", "task", "--type", "item", "--name", "undated")
    done = _add(tmp_path, capsys, "--kind", "task", "--type", "item", "--name", "done",
                "--due", "2000-01-01")
    run("resolve", done, store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--overdue", "--json", store_dir=tmp_path) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["id"], r["due"]) for r in rows] == [(late, "2000-01-01")]
    assert run("list", "--overdue", store_dir=tmp_path) == 0
    text = capsys.readouterr().out
    assert "match --overdue" in text.splitlines()[0]
    assert "due=2000-01-01 (overdue " in text


def test_add_names_a_row_resolved_at_a_commit_this_branch_lacks(repo, tmp_path, capsys):
    """A fix on an unmerged branch read as done: an agent on main searched
    `--status open` and filed the bug again (demo run, 2026-10-10). A row
    resolved at a commit HEAD reaches stays quiet, as does one no branch
    holds (a squash merge), and this one once its branch merges."""
    git = lambda *a: _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", *a)
    c0 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                        capture_output=True).stdout.strip()
    git("switch", "-q", "-c", "fix")
    git("commit", "-q", "--allow-empty", "-m", "fix")
    fixed = _add(tmp_path, capsys, "bug", "--target", "item:lint", "--body", "ruff fails")
    run("resolve", fixed, "--ref", "commit:HEAD", store_dir=tmp_path)
    capsys.readouterr()
    old = _add(tmp_path, capsys, "bug", "--target", "item:lint", "--body", "an older one")
    run("resolve", old, "--ref", f"commit:{c0}", store_dir=tmp_path)
    git("switch", "-q", "-c", "squashed", "main")
    git("commit", "-q", "--allow-empty", "-m", "squashed away")
    capsys.readouterr()
    gone = _add(tmp_path, capsys, "bug", "--target", "item:lint", "--body", "merged by squash")
    run("resolve", gone, "--ref", "commit:HEAD", store_dir=tmp_path)
    git("switch", "-q", "main")
    git("branch", "-q", "-D", "squashed")
    capsys.readouterr()
    assert run("add", "bug", "--target", "item:lint", "--body", "ruff fails on main",
               store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    fix = store.heads(store.load(tmp_path))
    sha = next(r.name for n in fix if n.id == fixed for r in n.refs)
    assert (f"note: {fixed} on item:lint was resolved at {sha[:7]}, which fix holds and "
            f"this branch lacks: if this entry is the same fault, its fix is there\n  ruff fails"
            ) in err, err
    assert old not in err and gone not in err, err
    git("merge", "-q", "fix")
    run("add", "bug", "--target", "item:lint", "--body", "again", store_dir=tmp_path)
    assert "this branch lacks" not in capsys.readouterr().err


def test_list_due_by_takes_rows_due_through_that_day(repo, tmp_path, capsys):
    """An hourly loop needed the rows due through a date and piped `--json`
    through a script (dogfood, 2026-10-13). A date is due through the end of
    its day, so DATE is in; a past-due row is due by then too. No status is
    implied: the loop wanted the predictions that had closed."""
    def task(name, *due):
        return _add(tmp_path, capsys, "--kind", "task", "--type", "item", "--name", name,
                    *(("--due", due[0]) if due else ()))
    want = [task("late", "2000-01-01"), task("on the day", "2500-06-15"),
            task("that evening", "2500-06-15T23:00Z")]
    task("the day after", "2500-06-16")
    task("undated")
    run("resolve", want[0], store_dir=tmp_path)
    capsys.readouterr()
    assert run("list", "--due-by", "2500-06-15", "--json", store_dir=tmp_path) == 0
    assert sorted(r["id"] for r in json.loads(capsys.readouterr().out)) == sorted(want)
    assert run("list", "--due-by", "2500-06-15", "--status", "open", store_dir=tmp_path) == 0
    text = capsys.readouterr().out
    assert "--due-by 2500-06-15" in text.splitlines()[0]
    assert want[0] not in text and want[1] in text, text
    assert run("list", "--due-by", "soon", store_dir=tmp_path) == 2
    assert "--due-by: 'soon' is not a date: use YYYY-MM-DD" in capsys.readouterr().err


def test_supersede_sets_and_clears_due(repo, tmp_path, capsys):
    tid = _add(tmp_path, capsys, "--kind", "task", "--type", "item", "--name", "t")
    assert run("supersede", tid, "--due", "2026-10-01T22:30Z", store_dir=tmp_path) == 0
    capsys.readouterr()
    assert store.heads(store.load(tmp_path))[0].due == "2026-10-01T22:30:00+00:00"
    assert run("supersede", tid, "--due", "", store_dir=tmp_path) == 0
    capsys.readouterr()
    assert store.heads(store.load(tmp_path))[0].due is None


def test_due_on_a_kind_without_status_is_refused_by_name(tmp_path, capsys):
    assert run("add", "--kind", "note", "--type", "project", "--due", "2026-10-01",
               store_dir=tmp_path) != 0
    assert "status bit" in capsys.readouterr().err
    assert store.load(tmp_path) == []


def test_from_json_takes_due(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"kind": "task", "target": {"type": "project", "name": null}, "due": "2026-10-01"}\n'))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    assert store.load(tmp_path)[0].due == "2026-10-01"


_DOCS = [Path(cli.__file__).parent / "data" / "skill" / f for f in ("SKILL.md", "adoption.md", "catalogs.md", "wrap.md", "mechanics.md")] \
    + [Path(__file__).parent.parent / f for f in ("README.md", "docs/guide.md", "docs/reference.md")]


def _doc_flags(text):
    """(command, --flag) for each flag a code span names after a verb: the
    words `symbion <verb> [<arc verb>] … --flag`, or a table row's `<verb> …`.
    A fenced block is read line by line. An inline span may wrap within its
    paragraph, so it is read as one line; a table row is its own paragraph.
    A quoted argument is one word, and is read again as a line of its own:
    `--checked "pytest --count 50"` names no `--count`, and a toml string
    that runs `symbion list --json` names its flag. A span whose quotes do
    not pair is split on spaces."""
    verbs = {"init", "add", "list", "show", "resolve", "supersede", "commit", "rename",
             "tags", "summary", "schema", "context", "arc"}
    arcverbs = {"create", "seed", "list", "todo", "rename", "archive", "reconcile"}
    lines = [ln for f in re.findall(r"```.*?```", text, re.S) for ln in f.strip("`").splitlines()]
    for para in re.split(r"\n\s*\n", re.sub(r"```.*?```", "\n\n", text, flags=re.S)):
        rows = [ln for ln in para.splitlines() if ln.lstrip().startswith("|")]
        for unit in rows + ["\n".join(ln for ln in para.splitlines() if ln not in rows)]:
            lines += re.findall(r"`([^`]+)`", unit)
    pairs = set()
    while lines:
        line = lines.pop()
        try:
            words = shlex.split(line.replace("|", " "))
        except ValueError:
            words = line.replace("|", " ").split()
        lines += [w for w in words if " " in w]
        for i, w in enumerate(words):
            if w not in verbs or (i and not words[i - 1].endswith("symbion")):
                continue
            cmd = (w, words[i + 1]) if w == "arc" and words[i + 1:i + 2] and \
                words[i + 1] in arcverbs else (w,)
            for f in words[i + 1:]:
                m = re.match(r"\[?(--[a-z][a-z-]*)", f)
                if m:
                    pairs.add((cmd, m.group(1)))
    return pairs


def test_the_doc_scan_reads_a_code_span_that_wraps_across_lines():
    """A rewrap that breaks a span across lines left its flag unchecked, and
    its two backticks paired with the wrong neighbours, so the next span in
    the paragraph went unread too (2026-09-29)."""
    assert _doc_flags("Close it with `resolve <id>\n--bogus`, then `list --all`.") == \
        {(("resolve",), "--bogus"), (("list",), "--all")}
    # A table row never wraps: an odd backtick in one must not pair with the next row.
    assert _doc_flags("| `a` ` | odd |\n| `list --all` | x |") == {(("list",), "--all")}


def test_the_doc_scan_reads_a_quoted_argument_as_one_word():
    assert _doc_flags('`symbion add check --checked "pytest --count 50"`') == \
        {(("add",), "--checked")}
    assert _doc_flags("""`x = "set -e; symbion list --json | jq"`""") == {(("list",), "--json")}
    # catalogs.md's reading catalog: `out=$(symbion` hid its flags (80fc24f).
    assert _doc_flags("""`x = 'out=$(symbion list --all) && y'`""") == {(("list",), "--all")}


@pytest.mark.parametrize("doc", _DOCS, ids=lambda p: p.name)
def test_every_flag_a_doc_names_is_one_the_parser_takes(doc, tmp_path, capsys):
    """SKILL.md documented `arc reconcile --json` from 2026-09-04 to 09-24;
    the parser never had it, and the agent who tried it hit `unrecognized
    arguments`. Each doc is read through `-h` of the command
    it names, so a flag that exists only on another verb still fails."""
    pairs = _doc_flags(doc.read_text())
    assert len(pairs) >= {"SKILL.md": 8, "README.md": 3, "guide.md": 10, "reference.md": 18, "catalogs.md": 6}.get(doc.name, 1), \
        f"the scan read {len(pairs)} pairs: the pattern broke, not the docs"
    parser = cli._build_parser(frozenset(), frozenset(), frozenset(), tmp_path,
                               K.DEFAULT_KINDS)
    helps, missing = {}, []
    for cmd, flag in sorted(pairs):
        if cmd not in helps:
            with pytest.raises(SystemExit):
                parser.parse_args([*cmd, "-h"])
            helps[cmd] = capsys.readouterr().out
        if flag != "--dir" and not re.search(re.escape(flag) + r"\b", helps[cmd]):
            missing.append(f"{' '.join(cmd)} {flag}")
    assert missing == []


@pytest.mark.parametrize("doc", _DOCS, ids=lambda p: p.name)
def test_every_link_a_doc_makes_names_a_file_and_heading_there(doc):
    """The skill rewrite renamed catalogs.md's headings, and reference.md's
    links to them led nowhere until 2026-10-16. A heading's
    anchor is GitHub's: lowercase, punctuation but `-` and `_` dropped,
    spaces as hyphens."""
    def anchors(md):
        return {re.sub(r"[^\w\- ]", "", m.strip().lower()).replace(" ", "-")
                for m in re.findall(r"(?m)^#+\s+(.*)$", md.read_text(encoding="utf-8"))}
    bad = []
    for path, frag in re.findall(r"\]\(([^)#\s:]+)(?:#([^)\s]+))?\)", doc.read_text(encoding="utf-8")):
        to = doc.parent / path
        if not to.exists() or frag and to.suffix == ".md" and frag not in anchors(to):
            bad.append(f"{path}#{frag}" if frag else path)
    assert bad == []


def test_version_runs_outside_any_repo(tmp_path, monkeypatch, capsys):
    """`--version` answers before the store resolves, so it works where
    no repo is (checking which symbion is installed is a first step)."""
    from importlib.metadata import version
    monkeypatch.chdir(tmp_path)
    for flag in ("--version", "-V"):
        assert cli.main([flag]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"symbion {version('symbion')} from "
                              f"{Path(cli.__file__).parent}"), out


def test_python_dash_m_symbion_runs_the_cli(tmp_path):
    """`python -m symbion` said "No module named symbion.__main__", and a diff
    of two runs, that error on both sides, read as the same text (2026-10-15)."""
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).parents[1])}
    r = subprocess.run([sys.executable, "-m", "symbion", "-V"], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("symbion "), (r.stdout, r.stderr)


def test_version_names_the_commit_only_of_a_tracked_checkout(tmp_path):
    """The metadata version never moves in a checkout, so a checkout adds
    its commit. A package dir inside some OTHER repo (a venv in a project)
    is untracked there, and that repo's HEAD is not symbion's build."""
    r = tmp_path / "r"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "cli.py").write_text("x")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c0")
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    assert cli._version(r / "pkg").endswith(f"(git {sha})")
    (r / "pkg" / "cli.py").write_text("y")
    assert cli._version(r / "pkg").endswith(f"(git {sha}-dirty)")

    venv = r / "venv"
    venv.mkdir()
    (venv / "cli.py").write_text("x")
    assert "(git" not in cli._version(venv)


def test_an_edit_before_a_commit_says_it_changed_the_row_itself(tmp_path, capsys):
    """An edit by the row's author before `symbion commit` keeps the id
    (store.rewritable), and the id alone on stdout read as a no-op. The
    replace note looked up the superseded row, which an edit in place of a
    first version does not have: StopIteration."""
    run("add", "task", "--target", "project", "--body", "v1", "--tag", "a", store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    assert run("supersede", nid, "--body", "v2", "--tag", "b", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert out.strip() == nid
    assert f"note: {nid} was not yet committed, so it was edited in place" in err
    assert "(`symbion commit` before an edit keeps one)" in err
    assert "--tag replaced the tags, dropping 'a'" in err
    _commit(tmp_path, capsys)
    assert run("supersede", nid, "--body", "v3", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert out.strip() == nid and "edited in place" not in err, err
    kept = _earlier(tmp_path, capsys, nid)
    assert run("show", kept, "--all", "--json", store_dir=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)[0]["body"] == "v2", "the committed version"
    # Now only the newest version is uncommitted: named as the row, the note
    # read as if the committed row never was (2026-10-12).
    assert run("supersede", nid, "--append", "--body", "v4", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert (f"note: {nid}'s newest version, not yet committed, was edited in place; "
            f"the version before it stays as {kept}") in err, err
    assert "no earlier version" not in err and _earlier(tmp_path, capsys, nid) == kept


def test_an_edit_a_citation_keeps_from_the_row_itself_says_why(tmp_path, capsys):
    """An uncommitted edit of the caller's own row changes that row unless
    another row cites it. The cited case printed a new id and nothing else,
    so the caller, expecting the id given, could not tell why (2026-10-05)."""
    mine = _add(tmp_path, capsys, "bug", "--target", "item:x", "--body", "v1")
    theirs = _add(tmp_path, capsys, "bug", "--target", "item:z", "--body", "v1")
    shut = _add(tmp_path, capsys, "bug", "--target", "item:w", "--body", "v1")
    cite = _add(tmp_path, capsys, "note", "--target", "item:y",
                "--body", f"see {mine[-10:]}, {theirs} and {shut}")
    assert run("supersede", mine, "--body", "v2", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert out.strip() == mine and "edited in place" not in err, err
    assert run("show", _earlier(tmp_path, capsys, mine), "--all", "--json",
               store_dir=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)[0]["body"] == "v1", "the version cited"
    # Another author's edit, and a resolve, keep a version whether or not a row cites them.
    assert run("supersede", theirs, "--body", "v2", "--author", "sam", store_dir=tmp_path) == 0
    assert "edited in place" not in capsys.readouterr().err
    assert run("resolve", shut, store_dir=tmp_path) == 0
    assert "edited in place" not in capsys.readouterr().err
    assert run("supersede", cite, "--body", "v2", store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert out.strip() == cite and "edited in place" in err


def test_a_serve_on_the_store_links_ids_at_a_terminal_and_summary_names_it(tmp_path, capsys,
                                                                           tty):
    """The user, 2026-10-01: a running GUI gives links, for the person and
    for an agent to hand them. At a terminal each id `list`, `show` and
    `context` print opens its row's page (OSC 8); `summary` names the URL
    and a row's page. Only the serve on this store counts, and a pipe's
    line never changes."""
    import os
    from symbion.gui import servers
    store_dir = tmp_path / "store"
    nid = _add(store_dir, capsys, "task", "--target", "project", "--body", "b")
    url, page = "http://127.0.0.1:1111", f"http://127.0.0.1:1111/notes?id={nid}"
    # An imperative: as a URL alone, a briefing agent never linked a row.
    line = f"  symbion serve: {url}; when you point the user at an entry, link it: {url}/notes?id=<id>"

    assert run("summary", store_dir=store_dir) == 0               # no serve yet
    assert "symbion serve" not in capsys.readouterr().out
    assert run("summary", "--json", store_dir=store_dir) == 0
    assert json.loads(capsys.readouterr().out)["gui"] is None
    assert run("context", "--json", store_dir=store_dir) == 0
    assert json.loads(capsys.readouterr().out)["gui"] is None

    servers.record(url, store_dir)
    other = servers._dir() / "other.json"                         # started earlier, elsewhere
    other.write_text(json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:2222",
                                 "store": str(tmp_path.resolve()), "started": 0}))
    assert run("summary", store_dir=store_dir) == 0
    assert line in capsys.readouterr().out.splitlines()
    assert run("summary", "--json", store_dir=store_dir) == 0
    assert json.loads(capsys.readouterr().out)["gui"] == url
    # a client that shows an object's rows links them without a second process
    assert run("context", "--target", "project", "--json", store_dir=store_dir) == 0
    assert json.loads(capsys.readouterr().out)["gui"] == url
    assert run("list", store_dir=store_dir) == 0                  # a pipe: no link
    assert url not in capsys.readouterr().out

    tty()
    for verb in (("list",), ("show", nid), ("context", "--target", "project")):
        assert run(*verb, store_dir=store_dir) == 0
        raw = _ANSI.sub("", capsys.readouterr().out)              # the OSC 8 link stays
        assert f";{page}\x1b\\{nid[-10:]}\x1b]8;;\x1b\\" in raw, (verb, raw)
        assert raw.count(page) == 1, ("the id alone, not its whole line", verb, raw)
        assert "127.0.0.1:2222" not in raw, (verb, raw)      # an id may hold 2222


def test_a_row_on_a_file_counts_the_commits_that_changed_it_since(tmp_path, repo, capsys):
    """`add` stamps the file's content, a supersede keeps that stamp (a tag
    added later is no re-reading of the file), and the reads count from it."""
    _declare(tmp_path, '[catalogs]\nfile = "git ls-files"\n')
    (repo / "f").write_text("seen")             # the row is written over this edit
    run("add", "note", "--target", "file:f", "--body", "on f", store_dir=tmp_path)
    run("add", "note", "--target", "item:x", "--body", "on x", store_dir=tmp_path)
    capsys.readouterr()

    def since():
        run("list", "--json", store_dir=tmp_path)
        return {r["target"]["name"]: r["commits_since"] for r in json.loads(capsys.readouterr().out)}

    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "carries it")
    assert since() == {"f": 0, "x": None}
    (repo / "f").write_text("later")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "later")
    assert since() == {"f": 1, "x": None}

    _commit(tmp_path, capsys)
    run("list", "--type", "file", "--json", store_dir=tmp_path)
    nid = json.loads(capsys.readouterr().out)[0]["id"]
    run("supersede", nid, "--add-tag", "priority", store_dir=tmp_path)
    capsys.readouterr()
    assert since() == {"f": 1, "x": None}, "the revision keeps the stamp"

    run("list", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert any("file:f" in ln and "commits_since=1" in ln for ln in lines), lines
    assert not any("item:x" in ln and "commits_since" in ln for ln in lines), lines
    run("context", "--target", "file:f", "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)["notes"][0]["commits_since"] == 1


def test_a_seeded_row_stamps_its_file_as_add_does(tmp_path, repo, capsys):
    """`arc seed` built its rows with no stamp, so a box counted every commit
    dated after it, the one carrying its own seed-time edit included (the
    0.3.0 review, 2026-10-05). A sweep and named seeds both stamp."""
    _declare(tmp_path, '[catalogs]\nfile = "git ls-files"\n')
    (repo / "g").write_text("g")
    _git(repo, "add", "g")
    (repo / "f").write_text("seen")             # the seed is written over this edit
    for how in (["--scope", "file"], ["--scope", "file", "--name", "g"]):
        run("arc", "create", "--name", "a" + str(len(how)), "--scope", "file", store_dir=tmp_path)
        aid = capsys.readouterr().out.strip()
        assert run("arc", "seed", aid, *how, store_dir=tmp_path) == 0
        capsys.readouterr()
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "carries it")
    capsys.readouterr()
    run("list", "--json", store_dir=tmp_path)
    rows = json.loads(capsys.readouterr().out)
    assert sorted((r["target"]["name"], r["commits_since"]) for r in rows) == \
        [("f", 0), ("g", 0), ("g", 0)], rows


def test_a_revision_of_an_unstamped_row_counts_from_the_row_it_revises(tmp_path, repo, capsys):
    """A row written before stamping counts the commits dated after it. Its
    revision is written later, but an amendment or a tag is no re-reading of
    the file: counted from the revision's own date, the count fell to 0."""
    _declare(tmp_path, '[catalogs]\nfile = "git ls-files"\n')
    rid = "20200101-000000-000000-abc"
    with store.notes_path(tmp_path).open("a") as fh:
        fh.write(json.dumps({"id": rid, "kind": "task", "target": {"type": "file", "name": "f"},
                             "created_at": "2020-01-01T00:00:00+00:00", "author": "t",
                             "body": "on f", "status": "open"}) + "\n")
    (repo / "f").write_text("later")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "changes f")
    _commit(tmp_path, capsys)

    def since():
        run("list", "--json", store_dir=tmp_path)
        return [r["commits_since"] for r in json.loads(capsys.readouterr().out)]

    assert since() == [2], "c0 and `changes f`, both after 2020"
    run("supersede", rid, "--append", "--body", "more", store_dir=tmp_path)
    run("supersede", rid, "--add-tag", "priority", store_dir=tmp_path)
    capsys.readouterr()
    assert since() == [2]


def test_commit_wrap_marks_the_commit_in_the_trailer_block(tmp_path, capsys, monkeypatch):
    """`--wrap` says this commit is a session's end: its message is the handoff
    the next session reads, and `Wrap: <author>` beside `Rows:` is how summary
    finds it. One trailer block: a second `-m` paragraph would leave `Rows:`
    outside it, and `committed()` would stop reading it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setenv("SYMBION_AUTHOR", "ada")
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "--wrap", "-m", "the gist\n\nthe day, in a paragraph",
               store_dir=tmp_path) == 0
    assert "committed: entries ada 1" in capsys.readouterr().out
    msg = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%B"],
                         capture_output=True, text=True).stdout
    assert msg.strip() == "the gist\n\nthe day, in a paragraph\n\nRows: ada 1\nWrap: ada", msg


def test_commit_wrap_with_nothing_pending_commits_the_message_alone(tmp_path, capsys,
                                                                    monkeypatch):
    """A session that changed only the repo still hands off: with nothing
    pending, `--wrap` commits its message alone, and the next summary prints
    it. It exited 1 with `nothing to commit`, and the previous wrap stood
    (2026-10-13). A plain commit with nothing pending still exits 1."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setenv("SYMBION_AUTHOR", "ada")
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    run("commit", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "-m", "plain", store_dir=tmp_path) == 1
    assert "nothing to commit" in capsys.readouterr().out
    assert run("commit", "--wrap", "-m", "the gist\n\nthe day", store_dir=tmp_path) == 0
    assert "committed: the message alone, no entries or files" in capsys.readouterr().out
    msg = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%B"],
                         capture_output=True, text=True).stdout
    assert msg.strip() == "the gist\n\nthe day\n\nWrap: ada", msg
    run("summary", store_dir=tmp_path)
    assert "last wrap by ada" in capsys.readouterr().out


def test_commit_reads_its_message_from_stdin_with_m_dash(tmp_path, capsys, monkeypatch):
    """wrap.md's `-m "$(cat <<'EOF' … EOF)"` broke in bash 3.2, macOS's
    /bin/bash: one apostrophe in the body was a syntax error, and a mixed one
    committed a mangled message at exit 0 (2026-10-13). A heredoc on stdin is
    never inside `$( )`. Blank stdin is refused, as `--body-file -` refuses."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setenv("SYMBION_AUTHOR", "ada")
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(" \n"))
    assert run("commit", "--wrap", "-m", "-", store_dir=tmp_path) == 2
    assert "stdin" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("it's the gist\n\n`x` and $HOME\n"))
    assert run("commit", "--wrap", "-m", "-", store_dir=tmp_path) == 0
    msg = subprocess.run(["git", "-C", str(tmp_path), "log", "-1", "--format=%B"],
                         capture_output=True, text=True).stdout
    assert msg.strip() == "it's the gist\n\n`x` and $HOME\n\nRows: ada 1\nWrap: ada", msg


def test_commit_wrap_without_a_message_is_refused(tmp_path, capsys, monkeypatch):
    """A wrap's message is the handoff; `update notes` would hand the next
    session nothing. Nothing is committed."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    run("add", "--kind", "note", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    assert run("commit", "--wrap", store_dir=tmp_path) == 2
    assert "--wrap needs -m" in capsys.readouterr().err
    assert subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True).returncode != 0


def test_undo_takes_back_only_your_own_last_write_and_says_what_it_was(tmp_path, capsys,
                                                                        monkeypatch):
    """`undo ID` by the rule `commit` takes rows by: this session's write, or
    with no session, a write of your author's that no session made."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    nid = _add(tmp_path, capsys, "task", "--target", "project", "--body", "v1")
    assert run("undo", nid[-10:], store_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"undone: the add of {nid} by ") and \
        out.rstrip().endswith("; the store reads as it did before it"), out
    nid = _add(tmp_path, capsys, "task", "--target", "project", "--body", "v1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    assert run("undo", nid, store_dir=tmp_path) == 1
    assert f"undo: the last write is not yours: it was the add of {nid}" in \
        capsys.readouterr().err
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    nid = _add(tmp_path, capsys, "note", "--target", "project", "--author", "ann")
    assert run("undo", nid, "--author", "bob", store_dir=tmp_path) == 1
    capsys.readouterr()
    assert run("undo", nid, "--author", "ann", store_dir=tmp_path) == 0
    assert run("show", nid, store_dir=tmp_path) == 1


def test_doc_is_a_built_in_type_over_the_stores_docs(repo, tmp_path, capsys):
    """A row's long detail lives in a file under the store's docs/, and the
    row names it, `--ref doc:NAME`, in every store (the user, 2026-10-14).
    With none written yet, the write says to write it first, not to declare
    a resolver. A name is its path within docs/ (`doc:docs/x.md` said docs
    twice, the user, 2026-10-15), and one typed as git prints it still
    finds it. The project's own file at that path is not the row's: no
    blob is stamped."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "docs").rmdir()                 # a clone's: git keeps no empty dir
    assert run("context", "--target", "doc:recipe", store_dir=store_dir) == 1
    assert "the store has no file under docs/ yet" in capsys.readouterr().err
    assert run("add", "note", "--target", "item:x", "--ref", "doc:recipe", "--body", "b",
               store_dir=store_dir) == 1
    err = capsys.readouterr().err
    assert "the store has no file under docs/ yet: write " in err
    assert "then name it with doc:NAME" in err, err
    (store_dir / "docs").mkdir(exist_ok=True)
    (store_dir / "docs" / "release-recipe.md").write_text("# recipe\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "release-recipe.md").write_text("the project's, not the store's\n")
    for where in (["--target", "doc:recipe"],
                  ["--target", "item:x", "--ref", "doc:docs/release-recipe.md"]):
        assert run("add", "note", *where, "--body", "b", store_dir=store_dir) == 0
    capsys.readouterr()
    on, ref = store.load(store_dir)
    assert on.target.name == ref.refs[0].name == "release-recipe.md"
    assert on.target_blob is None
    run("schema", store_dir=store_dir)
    assert "  doc " in (out := capsys.readouterr().out) and "built in catalog: " in out, out


def test_a_catalogs_doc_entry_replaces_the_built_in(repo, tmp_path, capsys):
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text('[catalogs]\ndoc = "printf \'wiki/a\\\\n\'"\n')
    assert run("add", "note", "--target", "doc:a", "--body", "b", store_dir=store_dir) == 0
    assert store.load(store_dir)[0].target.name == "wiki/a"
    run("schema", store_dir=store_dir)
    assert "built in" not in capsys.readouterr().out.split("\n  doc", 1)[1].splitlines()[0]


@pytest.mark.parametrize("in_place", [True, False])
def test_a_body_rewrite_that_drops_added_below_text_says_so(tmp_path, capsys, in_place):
    """`supersede --body`, meant to correct one sentence, replaced a resolved
    row's report and the resolution added below it, at exit 0 (dogfood,
    2026-10-15). A rewrite that loses an added-below section, as `show`
    marks them, names the loss; one that keeps it with an edit says nothing.
    Both as a new version and as an edit in place of an uncommitted one."""
    nid = _add(tmp_path, capsys, "bug", "--target", "project", "--author", "ada",
               "--body", "The parser drops the last line of every file it reads.")
    _commit(tmp_path, capsys)
    assert run("resolve", nid, "--author", "bob", "--body",
               "Fixed in the reader: the loop now flushes its buffer at EOF.",
               store_dir=tmp_path) == 0
    _commit(tmp_path, capsys)
    assert run("supersede", nid, "--author", "bob", "--body",
               "The parser drops the last line of every file it reads.\n\n"
               "Fixed in the reader: the loop now flushes its buffer at end of file.",
               store_dir=tmp_path) == 0
    assert "added-below" not in capsys.readouterr().err
    if not in_place:
        _commit(tmp_path, capsys)
    assert run("supersede", nid, "--author", "bob", "--body",
               "The parser drops the final line of every file it reads.",
               store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert ("note: --body replaced the whole text, dropping 1 added-below section; "
            "--append adds below and keeps them") in err, err


def test_a_rewrite_that_also_adds_below_says_it_edited_the_text_above(tmp_path, capsys):
    """A correction rewrote a decision's first paragraph and added a second,
    and `show` printed the new wording above `-- sam added below --`, so the
    rewrite read as the original (a fresh agent, 2026-10-14)."""
    nid = _add(tmp_path, capsys, "decision", "--target", "project", "--author", "ada",
               "--body", "We keep json.loads over literal_eval, since the config is JSON.")
    assert run("supersede", nid, "--author", "sam", "--body",
               "We keep yaml.safe_load over literal_eval, since the config is JSON.\n\n"
               "Corrected: the first wording named json.loads.", store_dir=tmp_path) == 0
    capsys.readouterr()
    run("show", nid, store_dir=tmp_path)
    assert "-- sam edited the text above and added below, " in capsys.readouterr().out
    run("supersede", nid, "--append", "--author", "kim", "--body", "and more", store_dir=tmp_path)
    capsys.readouterr()
    run("show", nid, store_dir=tmp_path)
    assert "\n    -- kim added below, " in capsys.readouterr().out, "an append edits nothing"


def test_context_prints_an_open_pre_registration_as_not_yet_checked(repo, tmp_path, capsys):
    """`list` read `to check, registered at <sha>`, and `context` read
    `checked at <sha>` for the same open prediction (a fresh agent,
    2026-10-14). A resolved one has run."""
    pid = _add(tmp_path, capsys, "prediction", "--target", "item:p", "--checked", "the sweep",
               "--body", "x rises")
    run("context", "--target", "item:p", store_dir=tmp_path)
    out = capsys.readouterr().out
    assert "    to check, registered at " in out and "    checked at " not in out, out
    run("resolve", pid, "--result", "x rose", store_dir=tmp_path)
    capsys.readouterr()
    run("context", "--target", "item:p", store_dir=tmp_path)
    assert "    checked at " in capsys.readouterr().out


def test_option_stores_answers_in_order_and_shows_them(tmp_path, capsys):
    """A question's answers are data, the default first (the user,
    2026-10-16), printed wherever its measurements would be, with ` | `
    between them since an answer may hold a comma."""
    assert run("add", "question", "--target", "project", "--body", "Ship it?",
               "--option", "ship", "--option", "hold, for now", "--option", "ask",
               store_dir=tmp_path) == 0
    nid = capsys.readouterr().out.strip()
    assert store.load(tmp_path)[0].options == ("ship", "hold, for now", "ask")
    assert run("show", nid, store_dir=tmp_path) == 0
    assert "    options: ship | hold, for now | ask\n" in capsys.readouterr().out
    assert run("list", store_dir=tmp_path) == 0
    assert "    options: ship | hold, for now | ask\n" in capsys.readouterr().out
    with pytest.MonkeyPatch.context() as mp:                  # and at a terminal
        mp.setattr(sys.stdout, "isatty", lambda: True)
        assert run("show", nid, store_dir=tmp_path) == 0
    assert "options: ship | hold, for now | ask" in _ANSI.sub("", capsys.readouterr().out)
    assert run("show", nid, "--json", store_dir=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)[0]["options"] == ["ship", "hold, for now", "ask"]
    run("add", "-h", store_dir=tmp_path)                      # --from-json names the keys
    assert "options" in capsys.readouterr().out


def test_option_refuses_a_fourth_and_any_kind_but_question(tmp_path, capsys):
    assert run("add", "question", "--target", "project", "--body", "?", "--option", "a",
               "--option", "b", "--option", "c", "--option", "d", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "at most three" in err and "in prose" in err and "Traceback" not in err, err
    assert run("add", "task", "--target", "project", "--body", "?", "--option", "a",
               store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "options on kind 'task': only a question takes answers" in err \
        and "Traceback" not in err, err
    # `--option ""` on add is an empty answer, not supersede's clear (review, 2026-10-17)
    assert run("add", "question", "--target", "project", "--body", "?", "--option", "",
               store_dir=tmp_path) == 1
    assert "option 1 is empty" in capsys.readouterr().err
    assert store.load(tmp_path) == []


def test_a_long_options_line_clips_in_the_list_and_prints_whole_in_show(tmp_path, capsys):
    opts = [f"{c} " * 29 + c for c in "abc"]                 # three answers of 59 characters
    run("add", "question", "--target", "project", "--body", "?",
        *(f for o in opts for f in ("--option", o)), store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("list", store_dir=tmp_path)
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if "options:" in ln]
    assert len(line) < 200 and line.endswith("…"), line
    run("show", nid, store_dir=tmp_path)
    assert f"    options: {' | '.join(opts)}\n" in capsys.readouterr().out
    run("list", "-h", store_dir=tmp_path)
    assert "and each option" in capsys.readouterr().out     # --grep says it searches them
    store.ensure_store(tmp_path)
    run("schema", store_dir=tmp_path)
    assert "--option gives each answer, the default first" in capsys.readouterr().out


def test_a_write_names_the_options_a_renamed_kind_drops(tmp_path, capsys):
    """A legacy row's field its kind cannot hold leaves the head on the next
    write; the note says which (review, 2026-10-17: `options` printed as
    "has no  bit")."""
    store.ensure_store(tmp_path)
    (tmp_path / "symbion.toml").write_text('[kinds]\nask = { status = true }\n')
    with open(store.notes_path(tmp_path), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": "legacy-1", "kind": "ask", "created_at": "2020-01-01T00:00:00",
                            "target": {"type": "project", "name": None}, "author": "sam",
                            "body": "?", "status": "open", "options": ["a", "b"]}) + "\n")
    assert run("supersede", "legacy-1", "--add-tag", "t", store_dir=tmp_path) == 0
    err = capsys.readouterr().err
    assert "kind 'ask' is not a question, so" in err and "drops options" in err, err
    assert store.heads(store.load(tmp_path))[0].options is None


def test_supersede_option_replaces_clears_and_survives_add_tag(tmp_path, capsys):
    """An option list is one datum, ordered, so a correction gives it whole;
    `--option ""` alone clears it, as `--due ""` clears a due."""
    run("add", "question", "--target", "project", "--body", "?", "--option", "a",
        store_dir=tmp_path)
    qid = capsys.readouterr().out.strip()
    head = lambda: store.heads(store.load(tmp_path))[0]
    assert run("supersede", qid, "--option", "x", "--option", "y", store_dir=tmp_path) == 0
    assert head().options == ("x", "y")
    # --add-tag alone takes a tags-only path; an option beside it must not drop there
    assert run("supersede", qid, "--add-tag", "t", "--option", "z", store_dir=tmp_path) == 0
    assert (head().options, set(head().tags)) == (("z",), {"t"})
    assert run("supersede", qid, "--option", "", store_dir=tmp_path) == 0
    assert head().options is None
    assert run("supersede", qid, "--option", "", "--option", "a", store_dir=tmp_path) == 1
    assert "option 1 is empty" in capsys.readouterr().err
    assert run("resolve", qid, "--option", "a", store_dir=tmp_path) == 2   # argparse: unrecognized


def test_list_grep_reads_options(tmp_path, capsys):
    run("add", "question", "--target", "project", "--body", "Ship it?",
        "--option", "ship", "--option", "hold for a check", store_dir=tmp_path)
    run("add", "note", "--target", "project", "--body", "nothing", store_dir=tmp_path)
    capsys.readouterr()
    run("list", "--grep", "hold", "--json", store_dir=tmp_path)
    assert [r["body"] for r in json.loads(capsys.readouterr().out)] == ["Ship it?"]


def test_a_bad_symbion_now_stops_every_command(tmp_path, capsys, monkeypatch):
    store.ensure_store(tmp_path)
    monkeypatch.setenv("SYMBION_NOW", "2026-10-22T19:47:54")
    assert run("list", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "SYMBION_NOW" in err and "2026-10-22T19:47:54Z" in err      # names the form
