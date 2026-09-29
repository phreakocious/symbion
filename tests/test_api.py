import inspect
import subprocess
from datetime import datetime, timedelta
import sys
from pathlib import Path

import pytest

from symbion import api, cli, gitref, store

pytestmark = pytest.mark.usefixtures("tmp_store")


def test_resolve_derives_type_sets_from_catalogs(repo, tmp_path):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    (store_dir / "symbion.toml").write_text('[catalogs]\nthing = "echo a"\n')

    ctx = api.resolve(str(store_dir))

    assert ctx.store_dir == store_dir.resolve()
    assert ctx.cfg.catalogs == {"thing": "echo a"}
    # builtins are always legal targets; the catalog extends them
    assert {"thing", "project", "commit", "item", "arc"} <= set(ctx.target_types)
    # scopes differ from target types, and from each other
    assert set(ctx.arc_scopes) == {"thing", "item", "project", "mixed"}
    assert set(ctx.seed_scopes) == {"thing", "item"}


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
    assert api.why_unverifiable(None) == "no provenance"
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
    assert out.supersedes == n.id


def test_retag_reads_tags_from_the_chain_TIP_not_the_named_row(repo, tmp_path):
    """cli.py's inline merge read the named row's tags while supersede wrote to
    the tip, so a tag added after the id you name was silently dropped."""
    ctx = api.resolve(str(tmp_path))
    first = store.add(tmp_path, kind="note", target={"type": "project", "name": None},
                      body="b", tags=["a"])
    tip = store.supersede(tmp_path, first.id, author="t", tags=["a", "b"])

    out = api.retag(ctx, first.id, add=["c"], author="t")

    assert set(out.tags) == {"a", "b", "c"}, "tip's tags must survive"
    assert out.supersedes == tip.id


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
    assert len(store.load(tmp_path)) == before + 1, "one row, not two"
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

    out = api._chain_tip(store.load(tmp_path), first.id)

    assert out.id == tip.id


def test_chain_tip_on_a_missing_id_raises_keyerror(repo, tmp_path):
    with pytest.raises(KeyError):
        api._chain_tip(store.load(tmp_path), "nope")


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
    "add_fields",   # takes fields, not a row -- no single keyword-only author
                    # for this audit to see; it demands one per row itself
                    # (raises ValueError, not TypeError) -- see
                    # test_add_fields_demands_an_author_per_row
    "rename_arc",   # writes, but to the arc record -- no note row
    "archive_arc",  # ditto
    "commit",            # writes git, not a note
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
    with pytest.raises(ValueError, match="author is required"):
        api.add_fields(ctx, [row])
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
    assert store.load_arcs(tmp_path)[0].archived is True


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


def test_supersede_refuses_a_target(repo, tmp_path):
    """store.supersede's internal note_from_dict(base) gets no target_types=,
    so a target forwarded through **fields lands on disk BOTH uncanonicalized
    and unvalidated -- measured before the guard: a short sha stayed short and
    type "bogus" was accepted, neither of which api.add_many allows. Target is
    inherited by design; the docstring's promise is now enforced."""
    ctx = api.resolve(str(tmp_path))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}},
                author="t")

    with pytest.raises(ValueError, match="target"):
        api.supersede(ctx, n.id, author="t",
                      target={"type": "commit", "name": "0187b02"})
    with pytest.raises(ValueError, match="target"):
        api.supersede(ctx, n.id, author="t", target={"type": "bogus", "name": "x"})
    assert len(store.load(tmp_path)) == 1, "a refused supersede writes nothing"


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
    assert timedelta(0) <= lag < timedelta(seconds=5), "stamped just before the row is minted"
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
    writes nothing; a one-row add prefixes row 1."""
    store_dir = reading_store(tmp_path)
    ctx = api.resolve(str(store_dir))
    api.add(ctx, _row("40.00"), author="t")
    api.add(ctx, _row("41.25"), author="t")
    before = {n.target.name for n in store.load(store_dir)}
    with pytest.raises(catalog.AmbiguousName) as e:
        api.add_many(ctx, [_row("44.00"), _row("40.55")], author="t")
    assert str(e.value).startswith("row 2:")
    assert {n.target.name for n in store.load(store_dir)} == before, \
        "row 1 resolved cleanly but nothing from this batch should land"

    with pytest.raises(catalog.AmbiguousName) as e:
        api.add(ctx, _row("40.55"), author="t")
    assert str(e.value).startswith("row 1:")


def test_sequential_adds_see_the_store(repo, tmp_path):
    ctx = api.resolve(str(reading_store(tmp_path)))
    api.add(ctx, _row("40.40"), author="t")
    assert api.add(ctx, _row("40.45"), author="t").target.name == "40.40"
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
        f'[catalogs]\nfile = "echo run >> {tmp_path}/cat.calls; echo src/parser.py"\n')
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
        f'[catalogs]\nfile = "echo run >> {tmp_path}/cat.calls; echo src/parser.py; echo src/lexer.py"\n')
    ctx = api.resolve(str(store_dir))
    n = api.add(ctx, {"kind": "note", "target": {"type": "project", "name": None}}, author="t")
    assert not (tmp_path / "cat.calls").exists(), "a project target runs no catalog"

    new = api.supersede(ctx, n.id, author="t",
                        refs=[{"type": "file", "name": "parser"}, {"type": "file", "name": "lexer"}])
    assert [r.name for r in new.refs] == ["src/parser.py", "src/lexer.py"]
    assert (tmp_path / "cat.calls").read_text().count("run") == 1
