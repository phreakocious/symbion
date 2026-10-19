import inspect
import subprocess
from datetime import datetime, timedelta
import sys
import unicodedata
from pathlib import Path

import pytest

from symbion import api, catalog, cli, config, gitref, store

pytestmark = pytest.mark.usefixtures("tmp_store")


def test_resolve_derives_type_sets_from_catalogs(repo, tmp_path):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    (store_dir / "symbion.toml").write_text('[catalogs]\nthing = "echo a"\n')

    ctx = api.resolve(str(store_dir))

    assert ctx.store_dir == store_dir.resolve()
    assert ctx.cfg.catalogs == {"thing": "echo a", "doc": catalog.DOC_CATALOG}   # doc: built in
    # builtins are always legal targets; the catalog extends them
    assert {"thing", "project", "commit", "item", "arc"} <= set(ctx.target_types)
    # scopes differ from target types, and from each other
    assert set(ctx.arc_scopes) == {"thing", "doc", "item", "project", "mixed"}
    assert set(ctx.seed_scopes) == {"thing", "doc", "item"}


def test_resolve_without_dir_uses_the_sibling_default(repo, monkeypatch):
    monkeypatch.delenv("SYMBION_DIR", raising=False)
    ctx = api.resolve()
    assert ctx.store_dir == (repo.parent / f"{repo.name}-notes").resolve()


def test_author_rules_differ_on_claudecode(monkeypatch):
    """The CLI attributes to claude inside an agent shell; the GUI never does.
    `symbion serve` is launched FROM that shell, so an unmodified rule would
    stamp every human button click as claude, permanently, append-only."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("SYMBION_AUTHOR", raising=False)
    monkeypatch.setattr(api, "_git_user_name", lambda: "ada")

    assert api.author_default() == "claude"
    assert api.gui_author() == "ada"


def test_a_codex_shell_writes_as_codex_and_its_gui_as_the_person(monkeypatch):
    """CODEX_THREAD_ID is Codex's CLAUDECODE (measured in a Codex 0.160 shell,
    2026-10-02). A Codex launched from a Claude Code session carries both."""
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(api, "_git_user_name", lambda: "ada")

    assert api.author_default() == "codex"
    assert api.gui_author() == "ada"


def test_a_hermes_shell_writes_as_hermes_and_its_gui_as_the_person(monkeypatch):
    """HERMES_AGENT is Hermes's CLAUDECODE: it exports it on each terminal
    command (measured in a Hermes CLI shell, 2026-10-06, with
    HERMES_SESSION_ID too, which gateway contexts strip). A Hermes launched
    from a Claude Code session carries CLAUDECODE too."""
    monkeypatch.setenv("HERMES_AGENT", "true")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(api, "_git_user_name", lambda: "ada")

    assert api.author_default() == "hermes"
    assert api.gui_author() == "ada"


def test_symbion_author_wins_for_both(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SYMBION_AUTHOR", "sam")
    assert api.author_default() == "sam"
    assert api.gui_author() == "sam"


def test_gui_author_falls_back_to_user(monkeypatch):
    monkeypatch.delenv("SYMBION_AUTHOR", raising=False)
    monkeypatch.setattr(api, "_git_user_name", lambda: "")
    assert api.gui_author() == "user"


def test_check_refs_rejects_an_unknown_type(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="unknown ref type"):
        api.check_refs(ctx.target_types, [{"type": "nope", "name": "x"}])

    # The positive direction: a legal type is accepted, its name NFC'd. No
    # resolution runs here: HEAD comes back as typed, not peeled to a sha --
    # peeling happens under the lock, in canonicalize_rows; see
    # test_add_peels_a_commit_ref_under_the_lock for that half.
    assert api.check_refs(ctx.target_types, [{"type": "commit", "name": "HEAD"}]) == \
        [{"type": "commit", "name": "HEAD"}]


def test_add_peels_a_commit_ref_under_the_lock(repo, tmp_path):
    """A ref's commit name is resolved the same as a target's: full sha, not
    the short one a human types. Resolution used to be a cli helper called
    directly; it moved under the lock (canonicalize_rows) and check_refs only
    checks shape/type/NFC, so this asserts on the stored note via api.add."""
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "refs": [{"type": "commit", "name": full[:7]}]}, author="t")
    assert n.refs[0].name == full


def test_fields_from_row_stamps_provenance_on_checks_only(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    check = api.fields_from_row(
        ctx, {"kind": "check", "target": {"type": "project", "name": None}}, "t")
    note = api.fields_from_row(
        ctx, {"kind": "note", "target": {"type": "project", "name": None}}, "t")
    assert check["provenance"] is not None and "sha" in check["provenance"]
    assert note["provenance"] is None


def test_add_canonicalizes_a_commit_target(repo, tmp_path):
    """The short sha a human types is not what the read side matches on.
    fields_from_row now only NFCs the name; resolution (peeling a commit ref
    to its full sha) happens under the lock, in canonicalize_rows -- so this
    asserts on the stored note via api.add rather than on fields_from_row's
    own output."""
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "commit", "name": full[:7]}}, author="t")
    assert n.target.name == full


def test_seed_names_raises_valueerror_not_systemexit(repo, tmp_path):
    """SystemExit is a CLI concept; a GUI caller must get an exception it can
    render in a dialog."""
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="at least one"):
        api.seed_names(ctx, "item", [])


def test_why_unverifiable_separates_the_three_causes():
    assert api.why_unverifiable({"runs": 3}) == "stamp names no commit"
    assert api.why_unverifiable({"sha": "abc", "dirty": True}) == "dirty tree"
    assert api.why_unverifiable({"sha": "abc"}) == "commit unavailable"


def test_cli_seed_errors_keep_their_old_text_and_route(repo, tmp_path, capsys):
    """Regression for a lift bug that slipped through green because nothing
    asserted stderr: cli's `arc seed` branch must not wrap
    api.seed_names in `except ValueError`, because catalog.AmbiguousName
    subclasses ValueError and would then be intercepted before it reaches
    main()'s dedicated (CatalogError, AmbiguousName) clause -- rerouting its
    "error: " prefix to a "seed: " one, and losing the exact old
    item-scope message (which named the flag: "--name", not "name")."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nthing = "echo ab1; echo ab2"\n')

    # item scope, no names: bare SystemExit(str) text, unprefixed, exit 1.
    assert cli.main(["--dir", str(tmp_path), "arc", "seed", "x",
                     "--scope", "item"]) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("item scope requires at least one --name"), err
    assert "[catalogs] in symbion.toml" in err, "the refusal must be actionable"

    # ambiguous catalog name: propagates to main()'s catalog-error clause,
    # "error: " prefixed, NOT the seed branch's own "seed: " prefix. The arc
    # must exist first: the id check runs before resolution, so a missing
    # arc reports "no arc" before the catalog ever runs.
    assert cli.main(["--dir", str(tmp_path), "arc", "create", "--name", "x",
                     "--scope", "thing"]) == 0
    aid = capsys.readouterr().out.strip()
    assert cli.main(["--dir", str(tmp_path), "arc", "seed", aid,
                     "--scope", "thing", "--name", "ab"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: "), err
    assert "seed:" not in err


def test_retag_adds_and_removes(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  body="b", tags=["keep", "drop"])

    out = api.retag(ctx, n.id, add=["new"], rm=["drop"], author="t")

    assert set(out.tags) == {"keep", "new"}
    assert out.author == "t"
    assert out.id == n.id and out.supersedes not in (None, n.id)


def test_retag_reads_tags_from_the_chain_TIP_not_the_named_row(repo, tmp_path):
    """cli.py's inline merge read the named row's tags while supersede wrote to
    the tip, so a tag added after the id you name was silently dropped."""
    ctx = api.resolve(str(tmp_path))
    first = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                      body="b", tags=["a"])
    tip = store.supersede(tmp_path, first.id, author="t", tags=["a", "b"])
    api.commit(ctx, "c")                     # so the retag appends (store.rewritable)

    out = api.retag(ctx, tip.supersedes, add=["c"], author="t")     # the version first wrote

    assert set(out.tags) == {"a", "b", "c"}, "tip's tags must survive"
    assert out.id == tip.id == first.id


def test_retag_on_a_missing_id_raises_keyerror(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(KeyError):
        api.retag(ctx, "nope", add=["x"], author="t")


def test_cli_add_tag_with_body_stays_one_row(repo, tmp_path):
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  body="old", tags=["a"])
    before = len(store.load(tmp_path))

    rc = cli.main(["--dir", str(tmp_path), "supersede", n.id,
                   "--add-tag", "b", "--body", "new"])

    assert rc == 0
    assert len(store.load(tmp_path)) == before + 1, "one entry, not two"
    head = store.heads(store.load(tmp_path))[0]
    assert set(head.tags) == {"a", "b"} and head.body == "new"


def test_retag_on_a_cyclic_chain_raises_valueerror_instead_of_hanging(repo, tmp_path):
    """add() doesn't validate id/supersedes, so a hand-edited notes.jsonl (or
    two raw add() calls) can produce a cycle -- same setup as
    tests/test_store.py's test_supersede_raises_on_a_cyclic_chain_instead_of_hanging."""
    ctx = api.resolve(str(tmp_path))
    target = {"type": "project", "name": None}
    store.add(tmp_path, id="aaa", kind="note", target=target, supersedes="bbb")
    store.add(tmp_path, id="bbb", kind="note", target=target, supersedes="aaa")

    with pytest.raises(ValueError, match="cycle in supersede chain"):
        api.retag(ctx, "aaa", add=["x"], author="t")


def test_chain_tip_returns_the_tip_not_the_named_row(repo, tmp_path):
    target = {"type": "project", "name": None}
    first = store.add(tmp_path, kind="note", target=target, body="b")
    tip = store.supersede(tmp_path, first.id, author="t", body="c")

    out = store.chain_tip(store.load(tmp_path), first.id)

    assert out.id == tip.id


def test_chain_tip_on_a_missing_id_raises_keyerror(repo, tmp_path):
    with pytest.raises(KeyError):
        store.chain_tip(store.load(tmp_path), "nope")


def test_cli_combined_add_tag_on_a_cyclic_chain_errors_instead_of_hanging(repo, tmp_path):
    """Regression for the inline tip-walk in cli._dispatch's combined branch,
    which originally had no cycle guard (unlike api.retag and
    store._supersede_unlocked) and hung forever. Run in a subprocess with a
    timeout so a regression is a real test failure, not a hung suite."""
    target = {"type": "project", "name": None}
    store.add(tmp_path, id="aaa", kind="note", target=target, supersedes="bbb")
    store.add(tmp_path, id="bbb", kind="note", target=target, supersedes="aaa")

    r = subprocess.run([sys.executable, "-c",
                        "from symbion import cli; raise SystemExit(cli.main("
                        f"['--dir', {str(tmp_path)!r}, 'supersede', 'aaa', "
                        "'--add-tag', 'x', '--body', 'new']))"],
                       capture_output=True, text=True, timeout=15)
    assert r.returncode == 1
    assert "cycle in supersede chain" in r.stderr


# api functions whose first parameter is `ctx` but which write no authored
# note row. Everything else taking `ctx` is a writer and must demand `author`.
# Naming the exceptions rather than the writers is what makes this fail CLOSED:
# a writer added later is a candidate by construction and fails the assertion
# below unless it is compliant, while a new non-writer has to be added here
# deliberately. The earlier version derived the set from COMPLIANT signatures,
# so a non-compliant writer vanished from both sides of an equality and the
# test stayed green -- measured by injecting two.
_NON_WRITERS = {
    "fields_from_row",     # pure: builds fields, writes nothing
    "seed_names",          # pure: expands a catalog
    "canonicalize_rows",   # pure: resolves names; add_many/supersede append
    "canonicalize_names",  # pure: resolves names; seed_arc appends
    "check_arc",           # pure: refuses an arc id that names no arc
    "check_arc_targets",   # pure: the same, for arc: targets and refs
    "check_target",        # pure: a target's shape, type and name
    "rewritable",          # a read: whether an edit by `author` would rewrite
    "add_fields",   # takes fields, not a row -- no single keyword-only author
                    # for this audit to see; it demands one per row itself
                    # (raises ValueError, not TypeError) -- see
                    # test_add_fields_demands_an_author_per_row
    "rename_arc",   # writes, but to the arc record -- no note row
    "archive_arc",  # ditto
    "commit",            # writes git, not a note
    "own_rows",          # a read: which pending rows `commit` would take
    "push",              # ditto
    "object_path",       # a read: where a target's file sits
    "known_files",       # a read: which catalog file a dropped file is
    "save_doc",          # writes a docs/ file, never a row; a doc has no author
    "free_doc_name",     # a read: the first doc name save_doc takes
}


def _ctx_taking_functions():
    """Every public function DEFINED in api whose first parameter is `ctx`."""
    out = {}
    for name, fn in inspect.getmembers(api, inspect.isfunction):
        if fn.__module__ != api.__name__ or name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        if params and params[0] == "ctx":
            out[name] = fn
    return out


def test_every_write_requires_author_by_keyword(repo, tmp_path):
    """Forgetting author must be a TypeError at the call, not a wrong row on
    disk.

    Two halves. The signature half below is the fail-closed one: it scans
    api for ctx-taking functions and demands a keyword-only, defaultless
    `author` on each one not named in _NON_WRITERS. The call half then proves
    the signature actually bites at the call site, matched on "author"
    specifically (not a bare TypeError) so an unrelated arity failure cannot
    masquerade as this guarantee."""
    writers = {n: f for n, f in _ctx_taking_functions().items()
               if n not in _NON_WRITERS}
    assert writers, "no writers found -- the scan itself is broken"
    for name, fn in sorted(writers.items()):
        p = inspect.signature(fn).parameters.get("author")
        assert p is not None, f"api.{name} takes ctx but no author"
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, \
            f"api.{name}'s author must be keyword-only"
        assert p.default is inspect.Parameter.empty, \
            f"api.{name}'s author must have no default"

    # _NON_WRITERS must not rot: every name in it still has to exist.
    assert _NON_WRITERS <= set(_ctx_taking_functions())

    ctx = api.resolve(str(tmp_path))
    row = {"kind": "note", "target": {"type": "project", "name": None}}
    cases = [
        (api.add,             (ctx, row)),
        (api.add_many,        (ctx, [row])),
        (api.supersede,       (ctx, "someid")),
        (api.retag,           (ctx, "someid")),
        (api.seed,            (ctx, "actid", "item", ["x"])),
        (api.create_arc, (ctx, "n", "d", "item")),
        (api.rename_target,   (ctx, "item", "old", "new")),
        (api.retype,          (ctx, "file", "item")),
        (api.undo,            (ctx, "someid")),
    ]
    assert sorted(fn.__name__ for fn, _ in cases) == sorted(writers), \
        "cases must cover every author-required writer api.py actually has"

    with pytest.raises(KeyError):
        api.retag(ctx, "someid", author="t")   # well-formed call: fails on the id, not arity

    for fn, args in cases:
        with pytest.raises(TypeError, match="author"):
            fn(*args)


def test_add_applies_canonicalization_and_provenance(repo, tmp_path):
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    ctx = api.resolve(str(tmp_path))

    n = api.add(ctx, {"kind": "check", "target": {"type": "commit", "name": full[:7]},
                      "checked": "suite", "result": "412 passed"}, author="ada")

    assert n.target.name == full
    assert n.provenance and n.provenance["sha"]
    assert n.author == "ada"


def test_add_many_is_atomic_on_a_bad_row(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    good = {"kind": "note", "target": {"type": "project", "name": None}}
    bad = {"kind": "nonsense", "target": {"type": "project", "name": None}}
    with pytest.raises(ValueError):
        api.add_many(ctx, [good, bad], author="t")
    assert store.load(tmp_path) == []   # load() returns [] for an absent store too


def test_add_fields_demands_an_author_per_row(repo, tmp_path):
    """add_fields takes fields, not rows, so it has no keyword-only author for
    the structural audit (test_every_write_requires_author_by_keyword) to see
    -- this is the guard that stands in its place. Both directions: a row
    missing author writes nothing, one carrying it writes."""
    ctx = api.resolve(str(tmp_path))
    row = {"kind": "note", "target": {"type": "project", "name": None}, "body": "b"}
    with pytest.raises(ValueError, match="^author is required"):
        api.add_fields(ctx, [row])
    with pytest.raises(ValueError, match="^entry 2: author is required"):
        api.add_fields(ctx, [dict(row, author="ada"), row])     # counted from 1
    assert store.load(tmp_path) == []

    made = api.add_fields(ctx, [dict(row, author="ada")])
    assert made[0].author == "ada"


def test_arc_passthroughs_round_trip(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "project", author="ada")
    assert act.author == "ada"

    api.rename_arc(ctx, act.id, "camp2")
    assert [a.name for a in store.load_arcs(tmp_path)] == ["camp2"]

    api.archive_arc(ctx, act.id)
    gone = store.load_arcs(tmp_path)[0]
    assert gone.archived is True and gone.archived_at.endswith("+00:00"), gone   # UTC


def test_commit_reports_nothing_to_commit_when_clean(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}},
            author="t")
    assert api.commit(ctx, "first") is True
    assert api.commit(ctx, "again") is False


def test_commit_raises_with_the_hooks_words_when_a_hook_refuses(repo, tmp_path,
                                                                refusing_hook):
    """A refusal read as 'nothing to commit', with git's output dropped
    (2026-09-28): a secret scanner in the store's pre-commit hook caught a
    token, and the caller heard that nothing had changed. The notes stay
    staged, so the next commit past the hook takes them."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}},
            author="t")
    words = refusing_hook(tmp_path)
    with pytest.raises(RuntimeError, match=words):
        api.commit(ctx, "refused")
    (tmp_path / ".git" / "hooks" / "pre-commit").unlink()
    assert api.commit(ctx, "after") is True


def test_commit_raises_when_git_cannot_stage(repo, tmp_path):
    """The other shape: `git add -A` fails (a held index.lock), stages
    nothing, and the empty index read as 'nothing to commit'."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}},
            author="t")
    (tmp_path / ".git" / "index.lock").touch()
    with pytest.raises(RuntimeError, match="index.lock"):
        api.commit(ctx, "locked")


def test_supersede_moves_an_entry_to_another_target(repo, tmp_path):
    """A misfiled entry had no fix that kept its id: supersede refused a
    target, and `rename` moves every entry on a name (a tester, 2026-10-15).
    A target given is new input, checked and resolved as `add` resolves one:
    a short sha is stored full, and a bad type or a target that is one of
    the entry's refs writes nothing."""
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                      "refs": ["item:x"]}, author="t")
    before = store.load(tmp_path)

    with pytest.raises(ValueError, match="unknown target type 'bogus'"):
        api.supersede(ctx, n.id, author="t", target={"type": "bogus", "name": "x"})
    with pytest.raises(ValueError, match="own target"):
        api.supersede(ctx, n.id, author="t", target="item:x")
    assert store.load(tmp_path) == before, "a refused supersede writes nothing"

    out = api.supersede(ctx, n.id, author="t", target={"type": "commit", "name": full[:7]})
    assert (out.target.type, out.target.name) == ("commit", full)


def test_a_moved_entry_is_stamped_with_its_new_files_blob(repo, tmp_path):
    """`target_blob` is what an entry's commits-since count starts from: a
    moved entry counts from the new file as it is now. An edit that keeps
    the target keeps the stamp, and an entry moved off a file has none."""
    (tmp_path / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    (repo / "g").write_text("y")
    subprocess.run(["git", "add", "g"], cwd=repo, check=True)
    blob = lambda p: subprocess.run(["git", "hash-object", p], cwd=repo,   # noqa: E731
                                    capture_output=True, text=True).stdout.strip()
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": "file:f"}, author="t")
    assert n.target_blob == blob("f")
    assert api.supersede(ctx, n.id, author="t", body="edited").target_blob == blob("f")
    moved = api.supersede(ctx, n.id, author="t", target="file:g")
    assert (moved.target.name, moved.target_blob) == ("g", blob("g"))
    assert api.supersede(ctx, n.id, author="t", target="item:x").target_blob is None


def test_an_edit_that_names_no_target_never_resolves_it_again(repo, tmp_path):
    """The departed-target rule (2026-09-04 spec): an entry whose target
    left the catalog stays editable, and its name is not resolved again. A
    resolve now would find `alpha2` by substring."""
    (tmp_path / "symbion.toml").write_text("""[catalogs]\nthing = 'printf "alpha\\n"'\n""")
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "task", "target": "thing:alpha"}, author="t")
    (tmp_path / "symbion.toml").write_text("""[catalogs]\nthing = 'printf "alpha2\\n"'\n""")
    ctx = api.resolve(str(tmp_path))
    out = api.supersede(ctx, n.id, author="t", body="still here")
    assert out.target.name == "alpha"


def test_supersede_refuses_a_provenance(repo, tmp_path):
    """Provenance is the check's own claim about the tree it was made against.
    Re-stamping it through a correction would let a later row re-date the
    evidence; passing None would erase it silently."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "check", "target": {"type": "project", "name": None},
                      "checked": "suite", "result": "ok"}, author="t")

    with pytest.raises(ValueError, match="provenance"):
        api.supersede(ctx, n.id, author="t", provenance=None)
    assert len(store.load(tmp_path)) == 1


def test_supersede_canonicalizes_refs(repo, tmp_path):
    """The one field supersede DOES re-resolve: refs are new input, not
    inherited. Short sha in, full 40-char sha out -- the read side matches on
    the full one."""
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "decision", "target": {"type": "project", "name": None}},
                author="t")

    out = api.supersede(ctx, n.id, author="t",
                        refs=[{"type": "commit", "name": full[:7]}])

    assert [(r.type, r.name) for r in out.refs] == [("commit", full)]

    with pytest.raises(ValueError, match="unknown ref type"):
        api.supersede(ctx, out.id, author="t", refs=[{"type": "bogus", "name": "x"}])


def test_retag_treats_a_bare_string_as_one_tag(repo, tmp_path):
    """set("priority") is six characters. The GUI star button passes a bare
    string, and six single-letter tags on a note is not recoverable by any
    read path."""
    ctx = api.resolve(str(tmp_path))
    n = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                  body="b", tags=["drop"])

    out = api.retag(ctx, n.id, add="priority", rm="drop", author="t")

    assert set(out.tags) == {"priority"}


def test_cli_list_author_filters(repo, tmp_path, capsys):
    """`list --author` had no test at all: the flag parsed and was wired into
    store.query, but nothing proved the wiring. Both directions -- the matching
    author's note is listed, the other's is not."""
    ctx = api.resolve(str(tmp_path))
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "by-ada"}, author="ada")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "by-bob"}, author="bob")

    assert cli.main(["--dir", str(tmp_path), "list", "--author", "ada"]) == 0
    out = capsys.readouterr().out
    assert "by-ada" in out and "by-bob" not in out


# ---- hashtags in a body ----
def test_harvest_pulls_hashtags_out_of_the_body():
    assert api.harvest_hashtags("#gui replace the art") == (["gui"], "replace the art")
    assert api.harvest_hashtags("be creative :) #gui") == (["gui"], "be creative :)")
    assert api.harvest_hashtags("do #gui the thing") == (["gui"], "do the thing")


def test_harvest_keeps_the_body_s_own_spacing():
    """A traceback pasted into a fence was stored with each run of spaces cut
    to one: the tidy after a harvest ran over the whole body, code included,
    and the GUI's edit dialog runs the harvest on every save."""
    fenced = '```\n  File "x.py", line 1\n    ctx = f()\n\n\n\ndef g():\n```'
    nested = "- a\n  - b\n\nline  \nbreak"
    for body in (fenced, nested):
        assert api.harvest_hashtags(body) == ([], body)
        assert api.harvest_hashtags(body + " #gui") == (["gui"], body)


def test_harvest_leaves_a_markdown_heading_alone():
    """`# Title` has a space after the hash; a tag does not. Without this the
    harvest would eat every heading and store its words as tags."""
    assert api.harvest_hashtags("# A real heading") == ([], "# A real heading")


def test_harvest_ignores_hex_colours_and_url_fragments():
    body = "colour is #0a0a0a and #eee8d5, see http://x/page#frag"
    assert api.harvest_hashtags(body) == ([], body)


def test_harvest_ignores_hashes_inside_code():
    body = "run `#include <stdio.h>` first"
    assert api.harvest_hashtags(body) == ([], body)
    fenced = "```\n#!/bin/sh\n#define X\n```"
    assert api.harvest_hashtags(fenced) == ([], fenced)


def test_harvest_dedupes_and_keeps_first_seen_order():
    assert api.harvest_hashtags("#b #a #b x") == (["b", "a"], "x")


def test_harvest_of_a_body_with_no_hashtags_changes_nothing():
    """The quiet direction. A harvest that mangled ordinary prose would only
    show up on notes nobody tagged."""
    body = "plain prose, no hashes at all"
    assert api.harvest_hashtags(body) == ([], body)
    assert api.harvest_hashtags("") == ([], "")
    assert api.harvest_hashtags(None) == ([], "")


def test_a_bang_names_the_kind_only_for_a_declared_kind():
    kinds = ("note", "task", "bug")
    assert api.harvest_kind("!task fix the header", kinds) == ("task", "fix the header")
    assert api.harvest_kind("fix it !bug !task", kinds) == ("bug", "fix it")
    # prose, code, a markdown image and a word with a bang inside stay put
    for body in ("this is !important", "run `!task` here", "![alt](x.png)", "wow!task"):
        assert api.harvest_kind(body, kinds) == (None, body)


def test_a_bang_takes_a_kind_cut_short_while_it_names_one():
    """The user, 2026-10-01: `!b` is enough, and with task and tame both
    declared, nothing until `!tas` or `!tam`."""
    kinds = ("note", "notebook", "task", "tame", "bug")
    assert api.harvest_kind("!b login fails", kinds) == ("bug", "login fails")
    assert api.harvest_kind("!tas fix it", kinds) == ("task", "fix it")
    assert api.harvest_kind("!ta fix it", kinds) == (None, "!ta fix it")
    assert api.harvest_kind("!note x", kinds) == ("note", "x")     # whole beats prefix
    assert api.harvest_kind("!bugs x", kinds) == (None, "!bugs x")


from symbion import kinds as K

PREREG = ('[kinds]\nprediction = { status = true, verdict = true }\n'
          'check = { verdict = true }\ntask = { status = true }\nnote = {}\n')


def _declare(store_dir, text):
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(text)


def _commit_empty(repo, msg):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", msg], check=True)


def test_ctx_carries_the_declared_kinds(repo, tmp_path):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\n')
    assert api.resolve(str(tmp_path)).kinds == {"anomaly": K.Kind(status=True)}
    assert api.resolve(str(tmp_path / "other")).kinds == K.DEFAULT_KINDS


def test_fields_from_row_validates_against_the_ctx_table(repo, tmp_path):
    _declare(tmp_path, '[kinds]\nanomaly = { status = true }\n')
    ctx = api.resolve(str(tmp_path))
    ok = api.fields_from_row(ctx, {"kind": "anomaly",
                                   "target": {"type": "project", "name": None}}, "t")
    assert ok["kind"] == "anomaly"
    with pytest.raises(ValueError, match=r"unknown kind 'bug'.*anomaly.*\[kinds\]"):
        api.fields_from_row(ctx, {"kind": "bug",
                                  "target": {"type": "project", "name": None}}, "t")


def test_fields_from_row_refuses_a_verdict_field_on_a_plain_kind(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="no verdict"):
        api.fields_from_row(ctx, {"kind": "note", "result": "x",
                                  "target": {"type": "project", "name": None}}, "t")


def test_resolving_a_prediction_restamps_provenance_and_keeps_the_commitment(repo, tmp_path):
    """The original row keeps the sha HEAD had before the data existed; the
    resolving row carries the sha the verdict was made at."""
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "project", "name": None},
                      "checked": "the sweep", "body": "falsified if the control shows it too"},
                author="ada")
    assert p.provenance and p.provenance["sha"]
    _commit_empty(repo, "the data landed")
    r = api.supersede(ctx, p.id, author="ada", status="resolved", result="HELD")
    assert r.provenance["sha"] != p.provenance["sha"]
    assert store.load(tmp_path)[0].provenance == p.provenance, "the commitment stays on disk"


def test_a_plain_correction_to_a_prediction_inherits_provenance(repo, tmp_path):
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "project", "name": None}},
                author="ada")
    _commit_empty(repo, "moved on")
    c = api.supersede(ctx, p.id, author="ada", body="sharpened the falsifier")
    assert c.provenance == p.provenance
    assert store.read_status(c) == "open"


def test_correcting_a_check_verdict_still_inherits_provenance(repo, tmp_path):
    """The re-stamp is for the open->resolved transition of a status+verdict
    row only; a verdict-only kind has no transition, and a correction is
    about the same run."""
    ctx = api.resolve(str(tmp_path))
    c = api.add(ctx, {"kind": "check", "target": {"type": "project", "name": None},
                      "checked": "suite", "result": "wrong"}, author="ada")
    _commit_empty(repo, "moved on")
    fixed = api.supersede(ctx, c.id, author="ada", result="right")
    assert fixed.provenance == c.provenance


def test_an_external_check_reads_external_whatever_the_tree_does(repo, tmp_path):
    """A check on something outside the tree (DNS, a host's logs) is stamped
    with when it ran, not HEAD: a dirty tree or a later commit says nothing
    about it. The same write without `external` on the same dirty tree reads
    unverifiable -- the other direction."""
    ctx = api.resolve(str(tmp_path))
    (repo / "f").write_text("unrelated edit")
    row = {"kind": "check", "target": {"type": "item", "name": "dns"},
           "checked": "dig", "result": "CNAME ok"}
    ext = api.add(ctx, {**row, "external": True}, author="ada")
    tree = api.add(ctx, row, author="ada")
    assert set(ext.provenance) == {"external", "at"} and ext.provenance["external"] is True
    lag = datetime.fromisoformat(ext.created_at) - datetime.fromisoformat(ext.provenance["at"])
    assert timedelta(0) <= lag < timedelta(seconds=5), "stamped just before the entry is minted"
    assert gitref.check_state(ctx.cfg, ext.provenance) == ("external", None)
    assert gitref.check_state(ctx.cfg, tree.provenance) == ("unverifiable", None)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", "f"], check=True)
    _commit_empty(repo, "moved on")
    assert gitref.check_state(ctx.cfg, ext.provenance) == ("external", None)


def test_external_is_refused_off_a_verdict_kind_and_off_a_boolean(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="verdict"):
        api.fields_from_row(ctx, {"kind": "note", "external": True,
                                  "target": {"type": "project", "name": None}}, "t")
    with pytest.raises(ValueError, match="true or false"):
        api.fields_from_row(ctx, {"kind": "check", "external": "yes",
                                  "target": {"type": "project", "name": None}}, "t")
    tree = api.fields_from_row(ctx, {"kind": "check", "external": False,
                                     "target": {"type": "project", "name": None}}, "t")
    assert "sha" in tree["provenance"], "false is the default, a tree stamp"


def test_an_external_prediction_restamps_external_and_a_correction_inherits(repo, tmp_path):
    """Resolving re-stamps with the time the verdict was made; it must not
    turn into a tree stamp. A plain correction keeps the run's time."""
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))
    assert api.fields_from_row(ctx, {"kind": "prediction", "external": True, "target":
                                     {"type": "project", "name": None}}, "t")["provenance"]["external"]
    # Written dated, since a stamp is to the second and the fixture is fast.
    p = store.add(tmp_path, kind="prediction", target={"type": "project", "name": None},
                  checked="the host's log", author="ada",
                  provenance={"external": True, "at": "2026-01-01T00:00:00+00:00"})
    c = api.supersede(ctx, p.id, author="ada", body="sharpened")
    assert c.provenance == p.provenance
    r = api.supersede(ctx, c.id, author="ada", status="resolved", result="HELD")
    assert set(r.provenance) == {"external", "at"} and r.provenance["at"] != p.provenance["at"]


def test_resolving_a_prediction_without_a_result_is_refused_at_the_api(repo, tmp_path):
    _declare(tmp_path, PREREG)
    ctx = api.resolve(str(tmp_path))
    p = api.add(ctx, {"kind": "prediction", "target": {"type": "project", "name": None}},
                author="ada")
    with pytest.raises(ValueError, match="--result"):
        api.supersede(ctx, p.id, author="ada", status="resolved")
    assert store.read_status(store.heads(store.load(tmp_path))[0]) == "open"


def test_api_seed_passes_the_kind_through(repo, tmp_path):
    _declare(tmp_path, '[kinds]\nfollowup = { status = true }\n')
    ctx = api.resolve(str(tmp_path))
    act = api.create_arc(ctx, "camp", "d", "item", author="ada")
    made = api.seed(ctx, act.id, "item", ["a"], author="ada", kind="followup")
    assert made[0].kind == "followup"


# ---- resolvers: write-time resolution under the lock, pending names ----
import threading
import time

from symbion import catalog
from _resolvers import reading_store, calls


def _row(name, typ="reading", **kw):
    return {"kind": "note", "target": {"type": typ, "name": name}, "body": "x", **kw}


def test_a_batch_of_neighbours_resolves_the_second_against_the_first(repo, tmp_path):
    """Row order is the rule: the first name stands, the second lands on it."""
    ctx = api.resolve(str(reading_store(tmp_path)))
    notes = api.add_many(ctx, [_row("40.40"), _row("40.45")], author="t")
    assert [n.target.name for n in notes] == ["40.40", "40.40"]


def test_the_reverse_order_stores_the_other_name_for_both(repo, tmp_path):
    ctx = api.resolve(str(reading_store(tmp_path)))
    notes = api.add_many(ctx, [_row("40.45"), _row("40.40")], author="t")
    assert [n.target.name for n in notes] == ["40.45", "40.45"]


def test_a_batch_resolution_error_names_its_row(repo, tmp_path):
    """`row 2:` prefixes the SAME AmbiguousName raised by the resolver, so a
    `--from-json` batch of many rows says which one failed instead of making
    the caller bisect. Both directions: a two-row batch prefixes row 2 and
    writes nothing; a one-row add names no row, since nothing was numbered."""
    store_dir = reading_store(tmp_path)
    ctx = api.resolve(str(store_dir))
    api.add(ctx, _row("40.00"), author="t")
    api.add(ctx, _row("41.25"), author="t")
    before = {n.target.name for n in store.load(store_dir)}
    with pytest.raises(catalog.AmbiguousName) as e:
        api.add_many(ctx, [_row("44.00"), _row("40.55")], author="t")
    assert str(e.value).startswith("entry 2:")
    assert {n.target.name for n in store.load(store_dir)} == before, \
        "entry 1 resolved cleanly but nothing from this batch should land"

    with pytest.raises(catalog.AmbiguousName) as e:
        api.add(ctx, _row("40.55"), author="t")
    assert str(e.value).startswith("'40.55' matches")


def test_an_ambiguous_ref_says_it_is_the_ref(repo, tmp_path):
    """It read as the target's error, so the writer went to fix the target."""
    ctx = api.resolve(str(reading_store(tmp_path)))
    api.add(ctx, _row("40.00"), author="t")
    api.add(ctx, _row("41.25"), author="t")
    with pytest.raises(catalog.AmbiguousName) as e:
        api.add(ctx, _row("40.00", refs=[{"type": "reading", "name": "40.55"}]), author="t")
    assert str(e.value).startswith("ref '40.55' matches")


def test_keep_refs_compares_the_refs_once_their_names_resolve(repo, tmp_path):
    """Another spelling of a ref the entry holds is no drop: the refs are
    compared after they resolve, under the lock. Both directions, and with
    keep_refs off (the GUI's edit dialog) a replace still replaces."""
    ctx = api.resolve(str(reading_store(tmp_path)))
    api.add(ctx, _row("40.40"), author="t")
    n = api.add(ctx, _row("x", typ="item", refs=[{"type": "reading", "name": "40.40"}]),
                author="t")
    with pytest.raises(store.RefsDropped) as e:
        api.supersede(ctx, n.id, author="t", keep_refs=True,
                      refs=[{"type": "item", "name": "z"}])
    assert [(r.type, r.name) for r in e.value.dropped] == [("reading", "40.40")]
    got = api.supersede(ctx, n.id, author="t", keep_refs=True,
                        refs=[{"type": "reading", "name": "40.45"}, {"type": "item", "name": "z"}])
    assert [(r.type, r.name) for r in got.refs] == [("reading", "40.40"), ("item", "z")]
    got = api.supersede(ctx, got.id, author="t", refs=[{"type": "item", "name": "w"}])
    assert [(r.type, r.name) for r in got.refs] == [("item", "w")]


def test_sequential_adds_see_the_store(repo, tmp_path):
    ctx = api.resolve(str(reading_store(tmp_path)))
    api.add(ctx, _row("40.40"), author="t")
    assert api.add(ctx, _row("40.45", body="y"), author="t").target.name == "40.40"
    assert api.add(ctx, _row("44.00"), author="t").target.name == "44.00", \
        "3.6 away: outside the 1.0 window, its own target"


def test_pending_never_duplicates_a_name_already_in_the_catalog(repo, tmp_path):
    """Two rows naming `parser` against a catalog of src/parser.py, no
    resolver. Both must store src/parser.py."""
    store_dir = tmp_path / "s"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text('[catalogs]\nfile = "echo src/parser.py"\n')
    ctx = api.resolve(str(store_dir))
    notes = api.add_many(ctx, [_row("parser", "file"), _row("parser", "file")], author="t")
    assert [n.target.name for n in notes] == ["src/parser.py", "src/parser.py"]


def test_the_catalog_runs_once_per_write_not_once_per_row(repo, tmp_path):
    store_dir = tmp_path / "s"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(
        f'[catalogs]\nfile = "echo run >> {tmp_path.as_posix()}/cat.calls; echo src/parser.py"\n')
    ctx = api.resolve(str(store_dir))
    api.add_many(ctx, [_row("parser", "file")] * 3, author="t")
    assert (tmp_path / "cat.calls").read_text().count("run") == 1


def test_a_commit_target_is_still_peeled_under_the_lock(repo, tmp_path):
    store.ensure_store(tmp_path / "s")
    ctx = api.resolve(str(tmp_path / "s"))
    n = api.add(ctx, _row("HEAD", "commit"), author="t")
    assert len(n.target.name) == 40


def test_refs_resolve_against_pending_too(repo, tmp_path):
    ctx = api.resolve(str(reading_store(tmp_path)))
    _, n = api.add_many(ctx, [_row("40.40"),
                              _row("50.00", refs=[{"type": "reading", "name": "40.45"}])],
                        author="t")
    assert n.refs[0].name == "40.40"


def test_supersede_resolves_only_the_refs_supplied(repo, tmp_path):
    """The original query reaches the resolver, and a body-only supersede on a
    row carrying refs runs it zero times and keeps them verbatim."""
    store_dir = reading_store(tmp_path, counter=True)
    ctx = api.resolve(str(store_dir))
    api.add(ctx, _row("40.40"), author="t")
    n = api.add(ctx, _row("50.00"), author="t")
    before = calls(store_dir)
    m = api.supersede(ctx, n.id, author="t", refs=[{"type": "reading", "name": "41.25"}])
    assert m.refs[0].name == "40.40" and calls(store_dir) == before + 1
    k = api.supersede(ctx, m.id, author="t", body="edited")
    assert [r.name for r in k.refs] == ["40.40"] and calls(store_dir) == before + 1
    assert k.target.name == "50.00"


def test_check_refs_validates_without_running_a_catalog(repo, tmp_path):
    store_dir = reading_store(tmp_path, counter=True)
    ctx = api.resolve(str(store_dir))
    assert api.check_refs(ctx.target_types, [{"type": "reading", "name": "1"}]) == \
        [{"type": "reading", "name": "1"}]
    with pytest.raises(ValueError):
        api.check_refs(ctx.target_types, [{"type": "bogus", "name": "1"}])
    assert calls(store_dir) == 0


def test_seed_sweep_passes_the_catalog_through_and_runs_the_resolver_zero_times(repo, tmp_path):
    store_dir = reading_store(tmp_path, counter=True)
    ctx = api.resolve(str(store_dir))
    api.add(ctx, _row("40.40"), author="t")
    api.add(ctx, _row("44.00"), author="t")
    act = api.create_arc(ctx, "camp", "d", "reading", author="t")
    before = calls(store_dir)
    made = api.seed(ctx, act.id, "reading", None, author="t")
    assert sorted(n.target.name for n in made) == ["40.40", "44.00"]
    assert calls(store_dir) == before
    assert api.seed(ctx, act.id, "reading", ["40.45"], author="t") == [], \
        "explicit: resolved to 40.40, which the sweep already seeded"
    assert calls(store_dir) == before + 1


def test_seed_sweep_over_an_empty_catalog_is_an_error_even_with_a_resolver(repo, tmp_path):
    ctx = api.resolve(str(reading_store(tmp_path)))
    act = api.create_arc(ctx, "camp", "d", "reading", author="t")
    with pytest.raises(catalog.CatalogError):
        api.seed(ctx, act.id, "reading", None, author="t")
    made = api.seed(ctx, act.id, "reading", ["90.00", "90.20"], author="t")
    assert [n.target.name for n in made] == ["90.00"], \
        "explicit names on the same empty catalog mint, and the second lands on the first"


def test_seed_names_previews_what_a_real_seed_would_store(repo, tmp_path):
    ctx = api.resolve(str(reading_store(tmp_path)))
    api.add(ctx, _row("40.40"), author="t")
    assert api.seed_names(ctx, "reading", ["40.45"]) == ["40.40"]
    assert api.seed_names(ctx, "reading", None) == ["40.40"]


def test_a_write_waiting_on_the_lock_resolves_against_what_landed_first(repo, tmp_path):
    """The falsifier is today's code: resolving before the lock would run the
    catalog while the holder still holds it, see an empty store, and mint."""
    store_dir = reading_store(tmp_path)
    ctx = api.resolve(str(store_dir))
    held = threading.Event()

    def holder():
        with store._lock(store_dir):
            held.set()
            time.sleep(0.5)
            n = store.note_from_dict({"id": "x", "kind": "note", "created_at": "2026-01-01T00:00:00+00:00",
                                      "target": {"type": "reading", "name": "40.40"}})
            store._append_note_unlocked(store_dir, n)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(timeout=5)
    assert held.is_set()
    n = api.add(ctx, _row("40.45"), author="t")
    t.join(timeout=10)
    assert n.target.name == "40.40"
    assert {x.target.name for x in store.load(store_dir)} == {"40.40"}


def test_the_catalog_runs_once_per_supersede_not_once_per_ref(repo, tmp_path):
    """add_many's twin (test_the_catalog_runs_once_per_write_not_once_per_row):
    supersede's new refs go through the same _canonicalizer and must share
    its one-run pool."""
    store_dir = tmp_path / "s"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text(
        f'[catalogs]\nfile = "echo run >> {tmp_path.as_posix()}/cat.calls; echo src/parser.py; echo src/lexer.py"\n')
    ctx = api.resolve(str(store_dir))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}}, author="t")
    assert not (tmp_path / "cat.calls").exists(), "a project target runs no catalog"

    new = api.supersede(ctx, n.id, author="t",
                        refs=[{"type": "file", "name": "parser"}, {"type": "file", "name": "lexer"}])
    assert [r.name for r in new.refs] == ["src/parser.py", "src/lexer.py"]
    assert (tmp_path / "cat.calls").read_text().count("run") == 1


def _file_catalog(store_dir):
    """A store `init` made lists no repo file until its toml declares one."""
    (store_dir / "symbion.toml").write_text(f'[catalogs]\nfile = "{config.FILE_CATALOG}"\n')


def test_object_path_keeps_a_name_inside_its_root(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    assert api.object_path(ctx, "file", "f") == (repo / "f").resolve()
    assert api.object_path(ctx, "doc", "x.md") == (tmp_path / "docs" / "x.md").resolve()
    assert api.object_path(ctx, "file", "../escape") is None
    assert api.object_path(ctx, "doc", "../notes.jsonl") is None


def test_known_files_finds_a_repo_file_by_its_name_and_text(repo, tmp_path):
    _file_catalog(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print(1)\n", newline="\n")    # Windows wrote CRLF
    ctx = api.resolve(str(tmp_path))
    assert api.known_files(ctx, "app.py", "print(1)\n") == [("file", "src/app.py")]
    # The same name with other text is a stranger.
    assert api.known_files(ctx, "app.py", "print(2)\n") == []


def test_known_files_lists_every_identical_copy(repo, tmp_path):
    _file_catalog(tmp_path)
    for d in ("a", "b"):
        (repo / d).mkdir()
        (repo / d / "__init__.py").write_text("")
    ctx = api.resolve(str(tmp_path))
    assert api.known_files(ctx, "__init__.py", "") == [("file", "a/__init__.py"),
                                                       ("file", "b/__init__.py")]


def test_known_files_reads_a_drop_as_a_browser_does(repo, tmp_path):
    """TextDecoder drops a leading BOM and keeps CRLF: a file read any other
    way is a stranger to its own drop."""
    _file_catalog(tmp_path)
    (repo / "w.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    ctx = api.resolve(str(tmp_path))
    assert api.known_files(ctx, "w.txt", "one\r\ntwo\r\n") == [("file", "w.txt")]


def test_known_files_finds_a_doc_by_a_decomposed_name(repo, tmp_path):
    """A Mac may give a dropped file's name decomposed; catalog names are
    composed."""
    (tmp_path / "docs" / "café.md").write_text("# notes\n", newline="\n")
    ctx = api.resolve(str(tmp_path))
    nfd = unicodedata.normalize("NFD", "café.md")
    assert api.known_files(ctx, nfd, "# notes\n") == [("doc", "café.md")]


def test_save_doc_writes_once_and_never_overwrites(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    assert api.save_doc(ctx, "plans/x.md", "one\n") == "plans/x.md"
    doc = tmp_path / "docs" / "plans" / "x.md"
    assert doc.read_text() == "one\n"
    assert api.save_doc(ctx, "plans/x.md", "one\n") == "plans/x.md"    # the same text: kept
    with pytest.raises(ValueError, match="holds other text"):
        api.save_doc(ctx, "plans/x.md", "two\n")
    assert doc.read_text() == "one\n"


def test_save_doc_keeps_crlf(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.save_doc(ctx, "w.txt", "a\r\nb\r\n")
    assert (tmp_path / "docs" / "w.txt").read_bytes() == b"a\r\nb\r\n"


@pytest.mark.parametrize("name", ["", "/abs.md", "../out.md", "a/../b.md", ".gitignore",
                                  "a/.hidden/b.md", "a//b.md", "a\\b.md"])
def test_save_doc_refuses_a_name_that_leaves_docs_or_hides(repo, tmp_path, name):
    """A dropped `.gitignore` would change what git sees in docs/."""
    ctx = api.resolve(str(tmp_path))
    before = sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts)
    with pytest.raises(ValueError, match="cannot save a doc"):
        api.save_doc(ctx, name, "x")
    assert sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts) == before


def test_free_doc_name_reuses_the_same_text_and_numbers_other_text(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    api.save_doc(ctx, "x.md", "one\n")
    assert api.free_doc_name(ctx, "x.md", "one\n") == "x.md"
    assert api.free_doc_name(ctx, "x.md", "two\n") == "x-2.md"
    api.save_doc(ctx, "x-2.md", "two\n")
    assert api.free_doc_name(ctx, "x.md", "three\n") == "x-3.md"
    assert api.free_doc_name(ctx, "notes", "n") == "notes"


def test_free_doc_name_refuses_a_directory_that_leads_out_of_docs(repo, tmp_path, symlink):
    """Under a symlink out of docs/, every numbered name leads out too: the
    search for a free one never ended."""
    out = tmp_path / "elsewhere"
    out.mkdir()
    symlink(tmp_path / "docs" / "out", out, directory=True)
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="leads out of docs/"):
        api.free_doc_name(ctx, "out/x.md", "t")


def test_a_gui_commit_takes_the_docs_its_rows_name(repo, tmp_path):
    """The GUI's commit took no files, so a doc saved from the GUI waited for
    an agent's commit, and a pushed entry named a doc the remote lacked."""
    ctx = api.resolve(str(tmp_path))
    api.save_doc(ctx, "kept.md", "k\n")
    api.save_doc(ctx, "loose.md", "l\n")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": ["doc:kept.md"], "body": "see the doc"}, author="ada")
    assert api.commit(ctx, "m", author="ada")
    tracked = subprocess.run(["git", "-C", str(tmp_path), "ls-files", "docs"],
                             capture_output=True, text=True).stdout.split()
    assert tracked == ["docs/kept.md"]


def test_a_gui_commit_stages_nothing_for_a_ref_out_of_docs(repo, tmp_path):
    """`git add -- docs/../notes.jsonl` would stage every pending row. A
    write refuses that name now (2026-10-19), but an older symbion stored
    it as typed, so the row is written below the api."""
    ctx = api.resolve(str(tmp_path))
    api.save_doc(ctx, "a.md", "a\n")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "body": "theirs"}, author="sam")
    store.add(tmp_path, kind="note", target={"type": "project", "name": None},
              refs=[{"type": "doc", "name": "../notes.jsonl"}], body="mine", author="ada")
    api.commit(ctx, "m", author="ada")
    assert [r["author"] for r in store.pending_rows(tmp_path)] == ["sam"]


def _tracked_docs(store_dir):
    return subprocess.run(["git", "-C", str(store_dir), "ls-files", "docs"],
                          capture_output=True, text=True).stdout.split()


def test_a_gui_commit_leaves_another_writers_edit_to_a_tracked_doc(repo, tmp_path):
    """A resolve copies its base's refs, so a GUI commit of one took an
    agent's uncommitted edit to the doc the task named."""
    ctx = api.resolve(str(tmp_path))
    api.save_doc(ctx, "plan.md", "v1\n")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": ["doc:plan.md"], "body": "the plan"}, author="agent")
    api.commit(ctx, "m", everyone=True)
    (tmp_path / "docs" / "plan.md").write_text("v2, half written\n")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": ["doc:plan.md"], "body": "read it"}, author="ada")
    api.commit(ctx, "m", author="ada")
    head = subprocess.run(["git", "-C", str(tmp_path), "show", "HEAD:docs/plan.md"],
                          capture_output=True, text=True).stdout
    assert head == "v1\n"


def test_a_gui_commit_reads_a_doc_name_literally(repo, tmp_path):
    """`git add -- docs/[ab].md` is a glob: it took docs/a.md and docs/b.md."""
    ctx = api.resolve(str(tmp_path))
    for name in ("[ab].md", "a.md", "b.md"):
        api.save_doc(ctx, name, name)
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": ["doc:[ab].md"], "body": "see it"}, author="ada")
    api.commit(ctx, "m", author="ada")
    assert _tracked_docs(tmp_path) == ["docs/[ab].md"]


def test_a_gui_commit_skips_a_doc_the_store_ignores(repo, tmp_path):
    """`git add` of an ignored path exits 1, and every GUI commit by that
    author refused while the row was pending."""
    ctx = api.resolve(str(tmp_path))
    with open(tmp_path / ".gitignore", "a") as f:
        f.write("*.tmp\n")
    api.save_doc(ctx, "kept.md", "k\n")
    api.save_doc(ctx, "scratch.tmp", "s\n")
    api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None},
                  "refs": ["doc:kept.md", "doc:scratch.tmp"], "body": "both"}, author="ada")
    assert api.commit(ctx, "m", author="ada")
    assert _tracked_docs(tmp_path) == ["docs/kept.md"]


def test_known_files_counts_a_file_two_catalogs_list_once(repo, tmp_path):
    """init's template pairs `file` with a `test` catalog: a dropped test
    file read as two files, and never became the target."""
    (tmp_path / "symbion.toml").write_text(
        f'[catalogs]\nfile = "{config.FILE_CATALOG}"\n'
        f"test = \"{config.FILE_CATALOG} 'tests/test_*.py'\"\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test(): pass\n", newline="\n")
    ctx = api.resolve(str(tmp_path))
    assert api.known_files(ctx, "test_x.py", "def test(): pass\n") == [("file", "tests/test_x.py")]


_Q = {"kind": "question", "target": {"type": "project", "name": None}, "body": "Ship it?"}


@pytest.mark.parametrize("bad, said", [
    ([], "one to three"), ("a", "one to three"), (["a", "b", "c", "d"], "at most three"),
    (["a", " "], "option 2 is empty"), (["a", 3], "option 2 is a string"),
    (["x" * 61], "over 60 characters"), (["a\nb"], "line break"), (["a\rb"], "line break"),
    (["a\u2028b"], "line break"),
    (["Ship", "ship "], "options repeat"),
])
def test_check_options_refuses_by_name(bad, said):
    """A question's answers, judged as check_measurements judges its numbers
    (the user, 2026-10-16: follow measurements). Three at most: a question
    with more is asked in prose, and the person answers in text."""
    with pytest.raises(ValueError, match=said):
        api.check_options(bad)


def test_check_options_takes_a_stripped_list_in_order(repo, tmp_path):
    assert api.check_options([" ship ", "hold"]) is None
    ctx = api.resolve(str(tmp_path))
    assert api.fields_from_row(ctx, {**_Q, "options": [" ship ", "hold"]}, "t")["options"] \
        == ["ship", "hold"]


def test_from_json_row_options_are_judged(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    with pytest.raises(ValueError, match="one to three"):
        api.fields_from_row(ctx, {**_Q, "options": "a"}, "t")
    with pytest.raises(ValueError, match="only a question takes answers"):
        api.fields_from_row(ctx, {**_Q, "kind": "task", "options": ["a"]}, "t")


def test_supersede_judges_a_given_options_list(repo, tmp_path):
    ctx = api.resolve(str(tmp_path))
    q = api.add(ctx, {**_Q, "options": ["a"]}, author="sam")
    with pytest.raises(ValueError, match="at most three"):
        api.supersede(ctx, q.id, author="sam", options=["a", "b", "c", "d"])
    assert api.supersede(ctx, q.id, author="sam", options=[" x", "y "]).options == ("x", "y")
