import argparse
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from symbion import cli, gitref, store
from symbion import summary as summ


def run(*args, store_dir):
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
    ("note", (), "supersede"),
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


def test_reconcile_without_flags_mutates_nothing(tmp_path):
    aid = _seeded_reconcile_arc(tmp_path)
    before = store.load(tmp_path)
    assert run("arc", "reconcile", aid, store_dir=tmp_path) == 0
    assert store.load(tmp_path) == before


def test_init_writes_symbion_toml(repo, tmp_path):
    assert run("init", store_dir=tmp_path) == 0
    assert (tmp_path / "symbion.toml").exists()


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


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MD_BODY = "**shipped** at HEAD\n\n```\nraw stays\n```"


def test_list_on_a_tty_renders_the_markdown_body(repo, tmp_path, capsys, monkeypatch):
    """A body is markdown by contract. On a terminal, with the [tty] extra,
    the markers render; the words survive. Two siblings pin the other shapes:
    a tty without rich, and a pipe, both print the body as written. All three
    pass --full: the default is one clipped line, which never reaches the
    body printer (test_list_clips_bodies_to_one_line_unless_full)."""
    pytest.importorskip("rich")
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project", "--body", _MD_BODY,
        store_dir=store_dir)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    run("list", "--full", store_dir=store_dir)
    out = _ANSI.sub("", capsys.readouterr().out)
    assert "**" not in out, "bold markers must render, not print"
    assert "shipped" in out and "raw stays" in out, "the words must survive rendering"


def test_list_on_a_tty_without_rich_prints_the_body_as_written(repo, tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / "store"
    run("add", "--kind", "note", "--type", "project", "--body", _MD_BODY,
        store_dir=store_dir)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    # None in sys.modules makes the import raise. The submodules too: an
    # earlier test may have loaded rich.console, and a cached submodule is
    # returned without ever touching its blocked parent.
    for m in ["rich", *[m for m in sys.modules if m.startswith("rich.")]]:
        monkeypatch.setitem(sys.modules, m, None)
    run("list", "--full", store_dir=store_dir)
    assert "**shipped** at HEAD" in capsys.readouterr().out


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
    assert not store.exists(tmp_path)


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
    assert not store.exists(tmp_path)
    assert run("add", "--kind", "note", "--type", "project", "--ref", "item:x", store_dir=tmp_path) == 0


def test_arc_list_aligns_the_progress_column_on_the_longest_id(tmp_path, capsys):
    long = "an arc whose slug runs past thirty-two characters"
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    run("arc", "create", "--name", long, "--scope", "item", store_dir=tmp_path)
    capsys.readouterr()
    run("arc", "list", store_dir=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert len({ln.index(" 0/0") for ln in lines}) == 1


def test_dir_is_accepted_after_the_subcommand(tmp_path):
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


def test_supersede_can_file_a_note_under_an_arc(repo, tmp_path, capsys):
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


def test_supersede_can_detach_a_note_from_its_arc(repo, tmp_path, capsys):
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


def test_add_tag_fast_path_does_not_swallow_an_arc_edit(repo, tmp_path, capsys):
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
    assert err == "no note found: no-such-id (list --limit 0 --all lists every id)\n", err
    assert run("resolve", "no-such-id", store_dir=tmp_path) == 1
    assert "(list --limit 0 --all lists every id)" in capsys.readouterr().err, "same helper on every miss"


def test_a_superseded_row_names_the_head_of_its_chain(tmp_path, capsys):
    """`list --id` on a superseded bug printed `[open]` and nothing else; its
    chain ended two hops later in a resolved row (2026-09-23), and it read
    as a live bug. The head is named, not the next hop, and the head
    itself carries no marker."""
    run("add", "--kind", "bug", "--type", "project", "--body", "b", store_dir=tmp_path)
    first = capsys.readouterr().out.strip()
    run("supersede", first, "--body", "c", store_dir=tmp_path)
    mid = capsys.readouterr().out.strip()
    run("resolve", mid, store_dir=tmp_path)
    head = capsys.readouterr().out.strip()

    assert run("list", "--id", first, store_dir=tmp_path) == 0
    assert f"[open] superseded -> {head} [resolved]" in capsys.readouterr().out
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
    one command, one row."""
    run("add", "--kind", "question", "--type", "item", "--name", "q", "--body", "which?",
        "--tag", "design", store_dir=tmp_path)
    qid = capsys.readouterr().out.strip()
    assert run("resolve", qid, "--body", "decided: the first", "--add-tag", "adopted",
               store_dir=tmp_path) == 0
    head = store.heads(store.load(tmp_path))[0]
    assert (head.status, head.body, head.tags) == ("resolved", "decided: the first",
                                                   ("adopted", "design"))


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
    assert store.heads(store.load(tmp_path))[0].body == "retired: no demand\n"


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


def test_resolve_help_names_no_label(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli._build_parser(frozenset(), frozenset(), frozenset(), tmp_path,
                          K.DEFAULT_KINDS).parse_args(["resolve", "--help"])
    out = capsys.readouterr().out
    assert "open row" in out and "bug/task" not in out


def test_list_text_renders_the_verdict_on_any_verdict_kind(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\naudit = { verdict = true }\n')
    run("add", "--kind", "audit", "--type", "project", "--checked", "x", "--result", "y",
        store_dir=tmp_path)
    capsys.readouterr()
    run("list", store_dir=tmp_path)
    assert "checked='x' result='y'" in capsys.readouterr().out


def test_seed_kind_mints_it_and_an_ineligible_one_is_a_clean_error(tmp_path, capsys):
    _declare(tmp_path, '[kinds]\nfollowup = { status = true }\nfact = {}\n')
    run("arc", "create", "--name", "x", "--scope", "item", store_dir=tmp_path)
    aid = store.load_arcs(tmp_path)[0].id
    capsys.readouterr()
    assert run("arc", "seed", aid, "--scope", "item", "--name", "a", "--kind", "followup",
               store_dir=tmp_path) == 0
    assert "seeded 1 new followup(s)" in capsys.readouterr().out
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
    assert "seeded 2" in capsys.readouterr().out and calls(s) == before
    assert run("arc", "seed", aid, "--scope", "reading", "--name", "40.45", store_dir=s) == 0
    assert "seeded 0" in capsys.readouterr().out and calls(s) == before + 1
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
    assert "'src/parsre.py' matches nothing in the file catalog; stored as typed" in err
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


_DOCS = [Path(cli.__file__).parent / "data" / "skill" / f for f in ("SKILL.md", "adoption.md")] \
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
    assert len(pairs) >= {"SKILL.md": 50, "README.md": 20}.get(doc.name, 1), \
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
