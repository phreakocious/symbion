import argparse
import io
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from symbion import api, cli, gitref, store
from symbion import summary as summ


_WRITES = {"add", "resolve", "supersede", "commit", "rename"}
_ARC_WRITES = {("arc", v) for v in ("create", "seed", "rename", "archive")}


def run(*args, store_dir):
    """A write refuses a store `init` never made, so a write verb here first
    makes `store_dir` one, as the first write itself once did: a read of a
    fresh dir still finds no store. A test of the refusal calls cli.main."""
    if set(args[:1]) & _WRITES or tuple(args[:2]) in _ARC_WRITES:
        store.ensure_store(store_dir)
    return cli.main(["--dir", str(store_dir), *args])


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
    assert cli._author_default() == "envperson"
    monkeypatch.delenv("SYMBION_AUTHOR")

    # 3: CLAUDECODE, with SYMBION_AUTHOR absent.
    assert cli._author_default() == "claude"
    monkeypatch.delenv("CLAUDECODE")

    # 4: neither set -- falls through to `git config user.name`.
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="gitperson\n"))
    assert cli._author_default() == "gitperson"

    # 5: git config empty too -- the final fallback.
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=""))
    assert cli._author_default() == "user"


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
    assert run("add", "note", "--target", "project:", store_dir=tmp_path) == 0
    assert run("add", "note", "--target", "project", store_dir=tmp_path) == 0
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
    assert f"1 open on item:x: {task}" in err and "supersede" in err, err
    for target in ("item:fresh", "project"):
        run("add", "task", "--target", target, store_dir=tmp_path)
        assert "open on" not in capsys.readouterr().err, target


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
             ("resolve", nid, "--body", long))
    for argv in quiet:
        assert run(*argv, store_dir=tmp_path) == 0, argv
        assert "session start" not in capsys.readouterr().err, argv

    rows = tmp_path / "rows.jsonl"
    rows.write_text("".join(json.dumps({"kind": "task", "body": long,
                                        "target": {"type": "item", "name": f"b{i}"}}) + "\n"
                            for i in range(5)))
    run("add", "--from-json", str(rows), store_dir=tmp_path)
    err = capsys.readouterr().err
    assert err.count(head) == 5 and "more" not in err, err


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
    """Printing 're-targeted 0 note(s)' and exiting 0 reads as done."""
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
        "re-targeted 0 note(s), re-pointed 1 ref(s): item:old -> new\n"


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
    assert capsys.readouterr().out.endswith(f" -> {new}\n")
    hs = store.heads(store.load(s))
    assert [n.target.name for n in hs if n.target.type == "commit"] == [new]
    assert [r.name for n in hs for r in n.refs] == [new]


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


def test_reconcile_resolve_stale_closes_it(tmp_path):
    aid = _seeded_reconcile_arc(tmp_path)
    assert run("arc", "reconcile", aid, "--resolve-stale", store_dir=tmp_path) == 0
    items = {n.target.name: n.status for n in store.arc_items(store.load(tmp_path), aid)}
    assert items["stale1"] == "resolved"


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
    for _ in range(2):
        run("add", "note", "--target", f"item:{long}", "--body", "b", store_dir=tmp_path)
    run("add", "note", "--target", "item:unrelated", "--body", "b", store_dir=tmp_path)
    capsys.readouterr()
    for argv in (["--name", "upload retry"],
                 ["--type", "item", "--name", "Upload Retry"]):
        assert run("list", *argv, "--json", store_dir=tmp_path) == 0
        out = capsys.readouterr()
        assert json.loads(out.out) == []
        assert f"item:{long} (2 rows)" in out.err and "unrelated" not in out.err, out.err
    run("list", "--name", f"{long}", store_dir=tmp_path)
    assert "contain" not in capsys.readouterr().err, "an exact name that matches says nothing"


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


def test_a_batch_add_ends_with_its_catalog_misses(tmp_path, capsys, monkeypatch):
    """Each catalog miss prints its own note, in line order among the rows
    that hit: in a 23-row batch one typo scrolled past and minted a second
    head (2026-09-23). A batch ends with one line naming every miss."""
    _declare(tmp_path, '[catalogs]\nfile = "printf \'a.py\\\\nb.py\\\\n\'"\n')
    rows = "".join(json.dumps({"kind": "note", "target": {"type": "file", "name": nm},
                               "body": "b"}) + "\n" for nm in ("a.py", "typo.py", "b.py"))
    monkeypatch.setattr("sys.stdin", io.StringIO(rows))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    last = capsys.readouterr().err.splitlines()[-1]
    assert last == "note: 1 of 3 names not in their catalog, stored as typed: file:typo.py", last
    run("add", "note", "--target", "file:other.py", store_dir=tmp_path)
    assert "not in their catalog" not in capsys.readouterr().err, "one row: its own note suffices"


def test_a_batch_on_a_resolver_type_names_no_misses(repo, tmp_path, capsys, monkeypatch):
    """A resolver mints a new name on purpose (a first sighting), so its
    type is left out of the batch's miss line."""
    # A FIXED catalog: a store-derived one lists the rows just written, so
    # nothing would ever be a miss and the check could not fail.
    (tmp_path / "mint.py").write_text("import sys; print(sys.stdin.readline().strip())\n")
    _declare(tmp_path, f'[catalogs]\nnum = "printf 1.00"\n'
                       f'[resolvers]\nnum = "{sys.executable} {tmp_path / "mint.py"}"\n')
    rows = "".join(json.dumps({"kind": "note", "target": {"type": "num", "name": v},
                               "body": "b"}) + "\n" for v in ("5.00", "7.00"))
    monkeypatch.setattr("sys.stdin", io.StringIO(rows))
    assert run("add", "--from-json", "-", store_dir=tmp_path) == 0
    assert {n.target.name for n in store.load(tmp_path)} == {"5.00", "7.00"}, "both new"
    assert "not in their catalog" not in capsys.readouterr().err


def test_reconcile_without_flags_mutates_nothing(tmp_path):
    aid = _seeded_reconcile_arc(tmp_path)
    before = store.load(tmp_path)
    assert run("arc", "reconcile", aid, store_dir=tmp_path) == 0
    assert store.load(tmp_path) == before


def test_init_writes_symbion_toml(repo, tmp_path):
    assert run("init", store_dir=tmp_path) == 0
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
    assert run("init", store_dir=s) == 1
    assert "invalid branch name" in capsys.readouterr().err
    assert not (s / ".git").exists()
    for k in env:
        monkeypatch.delenv(k)
    assert run("init", store_dir=s) == 0                       # a re-run recovers
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
    rows = tmp_path / "rows.jsonl"
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
    assert "2 open rows in busy, now listed outside arcs" in c.err, c.err
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
    assert lines[0] == "2 rows", lines
    assert max(len(ln) for ln in lines) <= 80, lines
    head = next(ln for ln in lines if ln.startswith(tid[-10:]))
    assert "○ task" in head and head.endswith("item:flaky upload  #ci #flaky  1 ref"), head
    assert "[open]" not in raw and "refs=" not in raw and tid not in raw, raw
    body = lines[lines.index(head) + 1]
    assert body.lstrip().startswith("fails about one run in twenty"), body
    assert len(body) - len(body.lstrip()) == head.index("item:"), (head, body)
    assert "\x1b[38;2;" in raw, "the rows carry colour"


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
    assert text in capsys.readouterr().out, "the control: an open row does print it"


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
    from symbion import term
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    assert term.age((now - timedelta(**ago)).isoformat(), now) == want


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
    src = tmp_path / "rows.jsonl"
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


def test_add_from_json_names_the_line_of_a_bad_status(tmp_path, capsys):
    src = tmp_path / "rows.jsonl"
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


@pytest.mark.parametrize("argv", [
    ["add", "--kind", "note", "--type", "item", "--name", "x", "--ref", "item:x"],
    ["add", "--kind", "note", "--type", "project", "--ref", "project:"],
    ["supersede", "SELF", "--ref", "item:y"],
    ["resolve", "SELF", "--ref", "item:y"],
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
    assert capsys.readouterr().out.startswith("1 note for project:"), target


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
                     "--body", "b", f"--dir={tmp_path}"]) == 0
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

    assert run("supersede", inherited.id, "--ref", "item:two", store_dir=tmp_path) == 0
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
    """`--id` implies `--all`: you named an exact row, and the commonest reason
    a known id is not a head is that it was superseded. A miss goes to stderr
    with exit 1 -- an empty listing would be indistinguishable from a row that
    exists and matched nothing else."""
    run("add", "--kind", "note", "--type", "project", "--body", "first",
        store_dir=tmp_path)
    nid = capsys.readouterr().out.strip()
    run("supersede", nid, "--body", "second", store_dir=tmp_path)
    capsys.readouterr()

    assert run("list", "--id", nid, "--json", store_dir=tmp_path) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [nid]
    assert rows[0]["body"] == "first"

    assert run("list", "--id", "no-such-id", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    # A newcomer typed `show 6` (a tail must be a whole segment) and
    # `show "login page"` (a search): the miss names both forms.
    assert err == ("no note found: no-such-id (an id, or its tail after a dash: 123456-a1b, "
                   "a1b; list --grep TEXT searches the rows)\n"), err
    assert run("resolve", "no-such-id", store_dir=tmp_path) == 1
    assert "list --grep TEXT searches" in capsys.readouterr().err, "same helper on every miss"


def test_a_superseded_row_names_the_head_of_its_chain(tmp_path, capsys):
    """`list --id` on a superseded bug printed `[open]` and nothing else; its
    chain ended two hops later in a resolved row (2026-09-23), and it read
    as a live bug. The head is named, not the next hop, and the head
    itself carries no marker. The old row's own `[open]` and its due phrase
    are history: printed first, they still read as live (2026-09-28)."""
    run("add", "--kind", "bug", "--type", "project", "--body", "b",
        "--due", "2020-01-01", store_dir=tmp_path)
    first = capsys.readouterr().out.strip()
    run("supersede", first, "--body", "c", store_dir=tmp_path)
    mid = capsys.readouterr().out.strip()

    assert run("list", "--id", first, store_dir=tmp_path) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert f"project: superseded -> {mid} [open] due=2020-01-01 (overdue" in line, line

    run("resolve", mid, store_dir=tmp_path)
    head = capsys.readouterr().out.strip()
    assert run("list", "--id", first, store_dir=tmp_path) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert f"project: superseded -> {head} [resolved] due=2020-01-01  " in line, line
    run("list", "--id", first, "--json", store_dir=tmp_path)
    assert json.loads(capsys.readouterr().out)[0]["head"] == head
    run("list", "--all", store_dir=tmp_path)
    assert capsys.readouterr().out.count("superseded ->") == 2, "both old rows, not the head"
    run("list", "--id", head, "--json", store_dir=tmp_path)
    assert "head" not in json.loads(capsys.readouterr().out)[0]


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


def test_add_kind_choices_come_from_the_table_and_the_error_names_it(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\n')
    assert run("add", "--kind", "anomaly", "--type", "project", store_dir=tmp_path) == 0
    capsys.readouterr()
    rc = run("add", "--kind", "bug", "--type", "project", store_dir=tmp_path)
    err = capsys.readouterr().err
    assert rc == 2 and "invalid kind 'bug'" in err and "anomaly" in err and "[kinds]" in err


_VOCAB = '[kinds]\nanomaly = { status = true }\nnote = {}\n[catalogs]\nfile = "echo a.py"\n'


def test_a_bare_value_set_flag_names_its_values(tmp_path, capsys):
    """argparse's "expected one argument" named the flag but not what it
    takes; `add --type` bare is how a human asks what the types are
    (2026-09-24). One case per way a flag holds its set: config-extended
    through `type=` (kind, type) and static through `choices=` (status).
    --name holds no set and keeps the stock message."""
    _declare(tmp_path, _VOCAB)
    for argv, legal in ((["add", "--type"], "arc, commit, file, item, project"),
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
    assert "kinds: anomaly, note" in err and "types: arc, commit, file, item, project" in err
    assert store.load(tmp_path) == []


def test_from_json_unknown_target_type_names_the_types(tmp_path, capsys):
    """Its siblings -- unknown kind, unknown ref type -- already named the set."""
    _declare(tmp_path, _VOCAB)
    p = tmp_path / "rows.jsonl"
    p.write_text('{"kind": "note", "target": {"type": "fiel", "name": "a.py"}}\n')
    assert run("add", "--from-json", str(p), store_dir=tmp_path) == 1
    assert ("unknown target type 'fiel' (choose from arc, commit, file, item, project)"
            in capsys.readouterr().err)


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
    assert run("show", "nope", store_dir=tmp_path) == 1
    capsys.readouterr()
    assert run("show", "-h", store_dir=tmp_path) == 0, "was `list --id -h`: an error"
    assert capsys.readouterr().out.startswith("usage: symbion show [-h] [--json] id")


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
    assert run("list", "--id", "000001-abc", "--json", store_dir=tmp_path) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [a.id]
    assert run("show", "ef", store_dir=tmp_path) == 1, "a tail ends on a `-` boundary"
    assert "no note found: ef" in capsys.readouterr().err

    assert run("resolve", "abc", store_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "ambiguous id 'abc': 2 rows end in -abc; give more of it", err
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
    assert "invalid type 'fiel' (choose from arc, commit, file, item, project)" in err
    assert run("context", "--target", "file:a.py", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "0 notes for file:a.py\n"


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
    flaky-test` missed 5 rows tagged `flaky-tests` (measured 2026-09-26: 6
    such pairs in 2 of 8 adopter stores). A write that brings a tag no other
    head carries names the existing tag it nearly is: case, `_`/`-`, a
    trailing s."""
    for _ in range(2):
        run("add", "--kind", "note", "--type", "item", "--name", "a", "--tag", "flaky-test",
            store_dir=tmp_path)
    capsys.readouterr()

    def err(*argv):
        assert run(*argv, store_dir=tmp_path) == 0
        return capsys.readouterr().err

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
        out.err == "note: not in this array: 1 row matches only in an older version (--all)\n", out.err
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
    assert "open row" in out and "bug/task" not in out


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


@pytest.mark.parametrize("argv, says", [
    (["delete", "x"], "append-only: `resolve ID` closes"),
    (["undo"], "append-only"),
    (["edit", "x"], "`supersede ID --body"),
    (["close", "x"], "`resolve ID`"),
    (["search", "x"], "`list --grep TEXT`"),
    (["due", "--help"], "`list --overdue`"),         # an adopter's agent, 2026-09-25
    (["add", "bug", "--target", "item:x", "login times out"], 'text goes in --body "…"'),
    # an unquoted value that split (dogfood, 2026-09-27: the hint above sent
    # its author to --body when the target lacked quotes)
    (["add", "task", "--target", "item:leak", "list", "coverage"],
     'a value with spaces needs quotes: --target "item:a b"'),
    (["list", "--open"], "--status open"),
    (["add", "--type", "bug", "--body", "x"], "'bug' is a kind: `add bug --target TYPE:NAME`"),
    (["list", "--type", "task"], "'task' is a kind: `list --kind task`"),
    (["add", "bug", "--target", "login page"], "--target 'item:login page'"),
])
def test_a_first_guess_that_fails_names_the_command_that_works(tmp_path, capsys, argv, says):
    """A first-day user's guesses, each met by an error that named no way
    forward (newcomer walk-through, 2026-09-26): an invalid choice, a bare
    `unrecognized arguments`, `invalid type 'bug'`, a TYPE list for a
    target typed without its TYPE."""
    assert run(*argv, store_dir=tmp_path) == 2
    err = capsys.readouterr().err
    assert says in err, err


def test_a_write_to_an_old_id_names_the_row_it_changed(tmp_path, capsys):
    """`supersede b3d --body again` after b3d was superseded replaced the
    newer row's text, and `resolve` on a resolved row replaced its
    resolution; each printed only a new id (newcomer walk-through,
    2026-09-26). The fast-forward stays: it is what closes the race."""
    run("add", "bug", "--target", "item:x", "--body", "v1", store_dir=tmp_path)
    old = capsys.readouterr().out.strip()
    run("supersede", old, "--body", "v2", store_dir=tmp_path)
    tip = capsys.readouterr().out.strip()
    assert run("supersede", old, "--body", "v3", store_dir=tmp_path) == 0
    assert f"note: {old} was superseded by {tip}; this applies to {tip}" in \
        capsys.readouterr().err
    head = store.heads(store.load(tmp_path))[0]
    assert run("resolve", head.id, store_dir=tmp_path) == 0
    assert capsys.readouterr().err == "", "a live open head: nothing to say"
    head = store.heads(store.load(tmp_path))[0]
    assert run("resolve", head.id, store_dir=tmp_path) == 0
    assert f"note: {head.id} is already resolved" in capsys.readouterr().err


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
    assert "1 note not yet in the store's git (symbion commit)" in piped, piped
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
    the owner's `idea` in one adopter store went unseen by every session,
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
    from_alice = [ln for ln in out.splitlines() if ln.startswith("  from alice [idea]")]
    assert [ln.split("  ")[-2] for ln in from_alice] == \
        ["alice idea 3", "alice idea 2", "alice idea 1"], out
    assert from_alice[0].startswith("  from alice [idea] item:i3"), from_alice[0]
    assert "  +1 more from others (--full)" in out
    assert out.count("alice's task") == 1, "a row the heads block prints is not repeated"
    assert "  from alice [task] item:t" in out, \
        "attribution cannot depend on which side of the cap a row fell: " + out
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
    assert f"note: 1 open on project: with this title: {first}" in err, err
    # The measured pair: a bold title with its detail on the same line, then
    # the same title alone. The title is the bold lead, else the first sentence.
    lead, _ = add("**Handoffs keep symbion workarounds after the fix.** one handoff still says X.")
    _, err = add("**Handoffs keep symbion workarounds after the fix.**\n\nDone: a, b.")
    assert f"with this title: {lead}" in err, err


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
    assert line.startswith("  [prediction, registered 2026-09-20] item:p  falsified if X"), line


def test_a_row_keeps_its_labels_in_every_summary_block(tmp_path, capsys, monkeypatch):
    """`from <author>` and a pre-registration's `registered <date>` rode on
    the heads block only, so a star, or a due date coming near, moved a row
    to a block that dropped both (a fresh agent starred its prediction and
    lost the date, 2026-09-29). The labels are the row's, not the block's."""
    import datetime as dt
    monkeypatch.setenv("SYMBION_AUTHOR", "claude")
    _declare(tmp_path, PREREG)
    soon = (dt.date.today() + dt.timedelta(days=2)).isoformat()
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
        assert "from ada [prediction, registered 2026-09-20] " in line(name), line(name)
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
    head = store.heads(store.load(tmp_path))[0]
    run("show", new[-3:], store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == f"    written {head.created_at.replace('T', ' ')} by sam; revises {old}", lines
    run("list", store_dir=tmp_path)
    assert "written" not in capsys.readouterr().out, "a page stays one line per row"


def test_list_since_keeps_the_rows_written_in_a_window(tmp_store, tmp_path, capsys):
    """An adopter's agent typed `symbion list --since 2h` to see what the
    last hours wrote, and got `unrecognized arguments` (measured 2026-09-25).
    `git log --since` is the convention: a span back from now, a date, or a
    datetime. Rows written before offsets were stamped are naive: local."""
    from datetime import datetime, timedelta
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
    assert bodies("--since", "2026-09-02") >= {"naive, early September"}
    assert "naive, early September" not in bodies("--since", "2026-09-02T11:00")
    assert run("list", "--since", "yesterday", store_dir=tmp_path) == 2
    assert "30m, 2h, 3d, 1w" in capsys.readouterr().err
    run("list", "--since", "2h", store_dir=tmp_path)
    assert capsys.readouterr().out.startswith("1 of 3 match --since 2h")


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
    ("notes.jsonl", {"target": {"type": "global", "name": None}}, "row"),
    ("notes.jsonl", {"target": {"type": "activity", "name": "a"}}, "row"),
    ("notes.jsonl", {"target": {"type": "project", "name": None},
                     "refs": [{"type": "global", "name": None}]}, "row"),
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
                               "body": f"row {i}"}) + "\n" for i in range(4))
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
    assert d["declared"] is False and len(d["kinds"]) == 7


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
        f"open('{tmp_path}/seen', 'a').write(q + '\\n')\nprint(q)\n")
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


def test_pipefail_is_what_keeps_a_failed_producer_from_reading_as_empty(repo, tmp_path, capsys):
    """Both directions of the trap the starter toml warns about."""
    s = tmp_path / "s"
    s.mkdir()
    (s / "symbion.toml").write_text(
        '[catalogs]\nt = "false | cat"\n[resolvers]\nt = "head -1"\n')
    assert run("add", "--kind", "note", "--type", "t", "--name", "q", "--body", "x",
               store_dir=s) == 0, "sh reports cat's status: the failure is silent"
    (s / "symbion.toml").write_text(
        '[catalogs]\nt = "set -o pipefail; false | cat"\n[resolvers]\nt = "head -1"\n')
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
    assert out.strip() == store.load(tmp_path)[-1].id, "stdout is still just the id"
    assert run("add", "--kind", "bug", "--type", "file", "--name", "src/parser.py",
               store_dir=tmp_path) == 0
    out, err = capsys.readouterr()
    assert err == "", err
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
    assert "1 commit" in out and f"git -C {store_dir} push" in out, out
    assert "no remote" not in out


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
    settings.write_text(json.dumps(cli._HOOK_SETTINGS))
    cli._link_user_skill()
    out = capsys.readouterr().out
    assert f"kept hook in {settings}" in out, out
    elsewhere = {"hooks": {"PreToolUse": cli._HOOK_SETTINGS["hooks"]["SessionStart"]}}
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


def test_init_outside_any_git_repo_refuses(tmp_path, monkeypatch, capsys):
    """init installs files beside the project, and there is no project here
    to put them beside."""
    monkeypatch.chdir(tmp_path)
    assert run("init", store_dir=tmp_path / "s") == 1
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
    assert lines[0].startswith(f"{n} rows, showing {summ.LIST_CAP} newest"), lines[0]
    assert "--limit" in lines[0]
    assert len([l for l in lines[1:] if _ID_AT_END.search(l)]) == summ.LIST_CAP
    assert not re.match(r"20\d\d-\d\d-\d\dT", lines[1]), lines[1]
    assert f"    body {n - 1}" in lines and "    body 0" not in lines, "newest first"

    assert run("list", "--limit", "0", store_dir=tmp_path) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{n} rows"
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
    assert head == "5 rows; --all: 2 superseded included", head


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
    assert "second paragraph" in out and not out.startswith("1 row"), "--id is the row itself, no header"
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
    assert capsys.readouterr().out.splitlines()[0] == "4 rows", "no filter, no denominator"


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
    assert head != bare and head == "1 row; --all: 0 superseded included", head


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
    assert capsys.readouterr().out == "0 notes for item:no/such.py\n"
    run("add", "--kind", "note", "--type", "item", "--name", "a.py", "--body", "b", store_dir=tmp_path)
    run("add", "--kind", "bug", "--type", "item", "--name", "a.py", "--body", "c", store_dir=tmp_path)
    ids = capsys.readouterr().out.split()
    run("resolve", ids[1], store_dir=tmp_path)
    capsys.readouterr()
    run("context", "--target", "item:a.py", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "2 notes for item:a.py", lines[0]
    run("context", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "1 note; +1 other head (list)", lines[0]
    run("context", "--json", store_dir=tmp_path)
    assert set(json.loads(capsys.readouterr().out)) == {"notes"}, "json shape unchanged"


def test_list_and_arc_list_on_an_empty_store_name_the_next_verb(tmp_path, capsys):
    """`0 rows` and a silent `arc list` on a fresh store (2026-09-22): the
    empty state names the verb that fills it, once, while it is empty."""
    store.ensure_store(tmp_path)
    assert run("list", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == f"0 rows; {summ.FIRST_CONTACT}\n"
    assert run("arc", "list", store_dir=tmp_path) == 0
    assert capsys.readouterr().out == "0 arcs (arc create --name NAME --scope item|file|mixed|project)\n"
    run("add", "--kind", "task", "--type", "project", "--body", "x", store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    assert capsys.readouterr().out.splitlines()[0] == "1 row"


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
    assert run("init", store_dir=s) == 0
    assert not (other / ".claude").exists(), "init writes no agent files into a project"
    assert not (own / ".claude").exists()
    assert (other / ".symbion").read_text().strip() == "../own-notes"


def test_init_that_repoints_a_project_says_what_it_replaced(two_projects, capsys):
    """Re-pointing a project's `.symbion` leaves the old store unread from
    there; the line said `wrote .symbion -> X` and not what X replaced."""
    own, other, s = two_projects
    (other / ".symbion").write_text("../elsewhere-notes\n")
    assert run("init", store_dir=s) == 0
    assert "(was '../elsewhere-notes': that store is no longer read from here)" in \
        capsys.readouterr().out


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
    assert head.endswith(": open outside arcs: bug 0, task 0, question 0; "
                         "2 open in arcs"), head


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
        assert out.splitlines()[0] == "1 row", out
        assert f"{s} belongs to {own}" in err, "say which repo computes catalogs"
        assert _bare("arc", "seed", "--scope", "file", "--dry-run") == 0
        assert "own_only.py" in capsys.readouterr().out
    assert _bare("add", "--kind", "check", "--type", "project",
                 "--checked", "c", "--result", "r") == 0
    assert store.load(s)[-1].provenance["sha"] == _head(own)
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
    assert capsys.readouterr().out.splitlines()[0] == "1 row"
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
    assert _bare("init") != 0
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


_DOCS = [Path(cli.__file__).parent / "data" / "skill" / f for f in ("SKILL.md", "adoption.md", "catalogs.md")] \
    + [Path(__file__).parent.parent / "README.md"]


def _doc_flags(text):
    """(command, --flag) for each flag a code span names after a verb: the
    words `symbion <verb> [<arc verb>] … --flag`, or a table row's `<verb> …`."""
    verbs = {"init", "add", "list", "show", "resolve", "supersede", "commit", "rename",
             "tags", "summary", "schema", "context", "arc"}
    arcverbs = {"create", "seed", "list", "todo", "rename", "archive", "reconcile"}
    pairs = set()
    for span in re.findall(r"```.*?```|`[^`\n]+`", text, re.S):
        for line in span.strip("`").splitlines():
            words = line.replace("|", " ").split()
            for i, w in enumerate(words):
                if w not in verbs or (i and words[i - 1] != "symbion"):
                    continue
                cmd = (w, words[i + 1]) if w == "arc" and words[i + 1:i + 2] and \
                    words[i + 1] in arcverbs else (w,)
                for f in words[i + 1:]:
                    m = re.match(r"\[?(--[a-z][a-z-]*)", f)
                    if m:
                        pairs.add((cmd, m.group(1)))
    return pairs


@pytest.mark.parametrize("doc", _DOCS, ids=lambda p: p.name)
def test_every_flag_a_doc_names_is_one_the_parser_takes(doc, tmp_path, capsys):
    """SKILL.md documented `arc reconcile --json` from 2026-09-04 to 09-24;
    the parser never had it, and the agent who tried it hit `unrecognized
    arguments`. Each doc is read through `-h` of the command
    it names, so a flag that exists only on another verb still fails."""
    pairs = _doc_flags(doc.read_text())
    assert len(pairs) >= {"SKILL.md": 35, "README.md": 20, "catalogs.md": 6}.get(doc.name, 1), \
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
