"""One store over several repos that change together: [repos] (the repos
design, 2026-10-13). Three sibling repos and one store whose name matches
none of them; each repo's `.symbion` names it."""
import json
import subprocess
from datetime import timedelta

import pytest
from symbion import api, catalog, cli, config, gitref, store

TOML = '[repos]\nansible = "../ansible"\nnet = "../tf-net"\napp = "../tf-app"\n'
FILES = {"ansible": ("site.yml", "roles/web/tasks/main.yml"),
         "tf-net": ("main.tf", "modules/vpc/main.tf", "environments/x/main.tf"),
         "tf-app": ("main.tf", "outputs.tf")}


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(r, msg="c"):
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(r, "rev-parse", "HEAD")


def _repo(path, *files):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    for p in files:
        (path / p).parent.mkdir(parents=True, exist_ok=True)
        (path / p).write_text(f"{path.name} {p}\n")
    return path


@pytest.fixture
def ws(tmp_path):
    """tmp_path holding ansible/, tf-net/, tf-app/ and work-notes/."""
    for name, paths in FILES.items():
        r = _repo(tmp_path / name, *paths)
        (r / ".symbion").write_text("../work-notes\n")
        _commit(r, f"{name} c0")
    s = tmp_path / "work-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text(TOML)
    return tmp_path


@pytest.fixture
def sym(ws, monkeypatch, capsys):
    """Run symbion in ws/`at`, or with --dir from ws itself, which is in no
    repo, when `at` is None: (exit code, stdout, stderr)."""
    def run(*args, at="tf-net"):
        monkeypatch.chdir(ws / at if at else ws)
        rc = cli.main(list(args) if at else ["--dir", str(ws / "work-notes"), *args])
        out, err = capsys.readouterr()
        return rc, out, err
    return run


def _heads(ws):
    return store.newest_first(store.heads(store.load(ws / "work-notes")))


def test_each_repo_is_a_type_and_the_roots_stand_where_the_cwd_is(ws, monkeypatch):
    monkeypatch.chdir(ws / "tf-app")
    ctx = api.resolve()
    assert {"ansible", "net", "app"} <= ctx.target_types
    assert "file" not in ctx.target_types and "file" in ctx.input_types
    assert ctx.cfg.here == "app"
    assert ctx.cfg.work_root == (ws / "tf-app").resolve()
    assert ctx.cfg.repo("net").work_root == (ws / "tf-net").resolve()
    assert ctx.cfg.repo("net").repos == {}
    assert ctx.cfg.catalogs["net"] == config.FILE_CATALOG
    monkeypatch.chdir(ws)
    out = api.resolve(str(ws / "work-notes")).cfg
    assert out.here is None
    assert out.project_root is None and out.work_root is None   # outside every repo, none


def test_a_linked_worktree_stands_in_its_repo(ws, monkeypatch):
    wt = ws / "net-wt"
    _git(ws / "tf-net", "worktree", "add", "-q", "-b", "feature", str(wt))
    monkeypatch.chdir(wt)
    cfg = api.resolve().cfg
    assert cfg.here == "net"
    assert cfg.repo("net").work_root == wt.resolve()
    assert cfg.repo("app").work_root == (ws / "tf-app").resolve()


@pytest.mark.parametrize("toml, says", [
    ('[repos]\nitem = "../ansible"\n', "'item' is taken"),
    ('[repos]\nfile = "../ansible"\n', "'file' is taken"),
    ('[repos]\nnet = 3\n', "a path is a quoted string"),
    ('[repos]\nnet = "../tf-net"\nnet2 = "../tf-net/"\n', "one repo is one type"),
    ('repos = "../tf-net"\n', "a table of NAME"),
    ('[repos]\nnet = "../tf-net"\n[catalogs]\nfile = "git ls-files"\n',
     "Move the command under the repo"),
    ('[repos]\nnet = "../tf-net"\n[renames]\nfile = "git"\n',
     "write it under each repo whose renames it follows: net = 'git'"),
])
def test_a_repos_table_that_cannot_work_does_not_load(ws, toml, says):
    s = ws / "work-notes"
    (s / "symbion.toml").write_text(toml)
    with pytest.raises(ValueError, match=says):
        config.load(s, project_root=None)


def test_a_store_its_repos_list_is_no_orphan(ws):
    assert config.orphan_stores(ws / "ansible") == []


def test_a_store_with_repos_follows_no_one_owner(ws, monkeypatch):
    """With one pointer left, store_owner names that repo; --dir from
    outside every repo must still stand in none."""
    for r in ("tf-net", "tf-app"):
        (ws / r / ".symbion").unlink()
    monkeypatch.chdir(ws)
    ctx = api.resolve(str(ws / "work-notes"))
    assert ctx.cfg.here is None and not ctx.followed_owner


def test_init_from_a_repo_the_store_does_not_list_points_that_repo(ws, sym):
    """Outside every listed repo the roots stand in none, and the pointer
    still goes into the cwd's repo."""
    r = _repo(ws / "tf-dns", "main.tf")
    _commit(r)
    rc, out, err = sym("--dir", "../work-notes", "init", "--yes", at="tf-dns")
    assert rc == 0, err
    assert (r / ".symbion").read_text().strip() == "../work-notes"


def test_from_outside_every_repo_a_catalog_that_is_no_repos_is_refused(ws, sym):
    """What is about no row's object runs in the repo you stand in, and
    outside every listed repo it is refused, naming the repos, even when
    its command reads no repo (the repos design, "Where you stand")."""
    (ws / "work-notes" / "symbion.toml").write_text(TOML + r"""[catalogs]
host = 'printf "h1\nh2\n"'
""")
    rc, out, err = sym("add", "note", "--target", "item:deploy", "--body", "x", at=None)
    assert rc == 0, err
    dns = _repo(ws / "tf-dns", "main.tf")                # names the store, is not listed
    (dns / ".symbion").write_text("../work-notes\n")
    _commit(dns)
    for at in (None, "tf-dns"):
        rc, out, err = sym("add", "note", "--target", "host:h1", "--body", "x", at=at)
        assert rc == 1 and "none of this store's repos (ansible, net, app)" in err, (at, err)
    assert sym("add", "note", "--target", "host:h1", "--body", "x", at="ansible")[0] == 0


def test_main_tf_in_two_repos_is_two_targets(ws, sym):
    assert sym("add", "note", "--target", "net:main.tf", "--body", "a")[0] == 0
    assert sym("add", "note", "--target", "app:main.tf", "--body", "b", at="ansible")[0] == 0
    assert {(n.target.type, n.target.name, n.body) for n in _heads(ws)} == {
        ("net", "main.tf", "a"), ("app", "main.tf", "b")}
    rc, out, err = sym("context", "--target", "net:main.tf", "--json", at="tf-app")
    assert [r["body"] for r in json.loads(out)["notes"]] == ["a"]


def test_a_repos_catalog_runs_in_that_repo_from_any_cwd(ws, sym):
    for at in ("tf-net", "ansible", None):
        rc, out, err = sym("add", "note", "--target", "app:outputs", "--body", str(at), at=at)
        assert rc == 0, (at, err)
    assert {n.target.name for n in _heads(ws)} == {"outputs.tf"}
    rc, out, err = sym("arc", "seed", "--scope", "net", "--dry-run", at="ansible")
    assert "modules/vpc/main.tf" in out and "site.yml" not in out


def test_a_catalogs_entry_replaces_a_repos_command_and_runs_in_its_tree(ws, sym):
    s = ws / "work-notes"
    (s / "tools").mkdir()
    # A marker, not `pwd`: Git for Windows' sh prints %TEMP% as /tmp.
    (s / "tools" / "tf.sh").write_text(': > ran-here\ngit ls-files "*.tf"\n')
    (s / "symbion.toml").write_text(TOML + '[catalogs]\nnet = "sh \\"$SYMBION_DIR\\"/tools/tf.sh"\n')
    rc, out, err = sym("add", "note", "--target", "net:vpc", "--body", "x", at="ansible")
    assert rc == 0, err
    assert [n.target.name for n in _heads(ws)] == ["modules/vpc/main.tf"]
    assert (ws / "tf-net" / "ran-here").is_file()


def test_renames_for_a_repo_type_reads_that_repos_history(ws, monkeypatch):
    _git(ws / "tf-net", "mv", "main.tf", "network.tf")
    _commit(ws / "tf-net", "rename")
    (ws / "work-notes" / "symbion.toml").write_text(TOML + '[renames]\nnet = "git"\n')
    monkeypatch.chdir(ws / "ansible")
    assert catalog.rename_map(api.resolve().cfg, "net") == {"main.tf": "network.tf"}


def test_reconcile_reads_each_repos_renames_alone(ws, sym):
    """Reconcile merged every `[renames]` type into one map, so tf-net's
    rename of main.tf moved an app task on main.tf once tf-app held a
    network.tf of its own (2026-10-15). Each type reads its own evidence."""
    assert sym("arc", "create", "--name", "x", "--scope", "net")[0] == 0
    aid = store.load_arcs(ws / "work-notes")[0].id
    for t in ("net", "app"):
        assert sym("add", "task", "--target", f"{t}:main.tf", "--arc-id", aid)[0] == 0
    _git(ws / "tf-net", "mv", "main.tf", "network.tf")
    _commit(ws / "tf-net", "rename")
    _git(ws / "tf-app", "rm", "-q", "main.tf")
    _commit(ws / "tf-app", "drop")
    (ws / "tf-app" / "network.tf").write_text("a different file\n")
    _commit(ws / "tf-app", "add")
    (ws / "work-notes" / "symbion.toml").write_text(TOML + '[renames]\nnet = "git"\napp = "git"\n')
    rc, out, err = sym("arc", "reconcile", aid, "--json")
    assert rc == 0, err
    assert {r["target_type"]: r["status"] for r in json.loads(out)} == \
        {"net": "renamed", "app": "stale"}


def test_a_listed_repo_this_machine_lacks_refuses_writes_and_reads(ws, sym):
    """A write on its type is refused, naming the path; its rows still read."""
    (ws / "work-notes" / "symbion.toml").write_text(TOML + 'dns = "../tf-dns"\n')
    rc, out, err = sym("add", "note", "--target", "dns:main.tf", "--body", "x")
    assert rc == 1 and "'dns' is listed under [repos] at" in err and "tf-dns" in err
    store.add_many(ws / "work-notes", [{"kind": "note", "body": "x",
                                         "target": {"type": "dns", "name": "main.tf"}}])
    rc, out, err = sym("list", "--type", "dns", "--json")
    assert rc == 0 and json.loads(out)[0]["commits_since"] is None


def test_a_repo_rows_file_is_read_in_its_repo(ws, sym):
    assert sym("add", "note", "--target", "net:main.tf", "--body", "x", at="ansible")[0] == 0
    assert _heads(ws)[0].target_blob == _git(ws / "tf-net", "hash-object", "main.tf")
    (ws / "tf-net" / "main.tf").write_text("changed\n")
    _commit(ws / "tf-net", "edit")
    for at in ("ansible", "tf-net", None):
        rc, out, err = sym("list", "--json", at=at)
        assert json.loads(out)[0]["commits_since"] == 1, at


def test_a_file_only_a_linked_worktree_holds_resolves_there(ws, sym):
    wt = ws / "net-wt"
    _git(ws / "tf-net", "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "dns.tf").write_text("x\n")
    rc, out, err = sym("add", "note", "--target", "net:dns.tf", "--body", "x", at="net-wt")
    assert rc == 0 and "matches nothing" not in err, err


X = "tf-net/environments/x"


def _objs(ws):
    return {(o.type, o.name) for n in _heads(ws) for o in (n.target, *n.refs)}


def _write(sym, ws, how, name, at):
    """One `file:` name through one write input."""
    if how == "target":
        return sym("add", "note", "--target", f"file:{name}", "--body", "x", at=at)
    if how == "ref":
        return sym("add", "note", "--target", "item:x", "--ref", f"file:{name}",
                   "--body", "x", at=at)
    if how == "json":
        p = ws / "entries.jsonl"
        p.write_text(json.dumps({"kind": "note", "target": f"file:{name}"}) + "\n")
        return sym("add", "--from-json", str(p), at=at)
    nid = sym("add", "note", "--target", "item:y", "--body", "y", at=at)[1].strip()
    flag = "--add-ref" if how == "add-ref" else "--ref"
    return sym("supersede", nid, flag, f"file:{name}", at=at)


@pytest.mark.parametrize("how", ["target", "ref", "json", "add-ref", "set-ref"])
def test_file_meets_the_rule_through_every_write_input(ws, sym, how):
    rc, out, err = _write(sym, ws, how, "main.tf", X)
    assert rc == 0, err
    assert "note: file:main.tf is net:environments/x/main.tf" in err
    assert ("net", "environments/x/main.tf") in _objs(ws)
    assert "file" not in {t for t, _ in _objs(ws)}
    rc, out, err = _write(sym, ws, how, "main.tf", "ansible")
    assert rc == 1, err
    assert ("file:main.tf matches net:main.tf and app:main.tf; write one of them, "
            "or its path") in err


def test_each_step_of_the_file_rule_from_a_subdirectory(ws, sym):
    for name, want in (
            ("main.tf", ("net", "environments/x/main.tf")),             # the cwd first
            ("modules/vpc/main.tf", ("net", "modules/vpc/main.tf")),     # then the root
            ("site.yml", ("ansible", "site.yml")),                       # one other repo
            ("new.tf", ("net", "environments/x/new.tf")),                # a new file
            ("../../../tf-app/outputs.tf", ("app", "outputs.tf")),       # a path
            (str(ws / "ansible" / "site.yml"), ("ansible", "site.yml")),
            ("./main.tf", ("net", "environments/x/main.tf"))):
        rc, out, err = sym("add", "note", "--target", f"file:{name}", "--body", name, at=X)
        assert rc == 0, (name, err)
        n = next(n for n in _heads(ws) if n.body == name)
        assert (n.target.type, n.target.name) == want, name
    rc, out, err = sym("add", "note", "--target", "file:../../../work-notes/x",
                       "--body", "x", at=X)
    assert rc == 1 and "in none of this store's repos" in err, err


def test_a_doc_resolves_from_outside_every_repo(ws, monkeypatch, capsys):
    """A `doc:` name is a path in the store's own docs/, so it needs no repo
    to stand in. From the store's dir, a [repos] store refused it as in none
    of its repos (the user, 2026-10-15)."""
    s = ws / "work-notes"
    (s / "docs").mkdir(exist_ok=True)
    (s / "docs" / "plan.md").write_text("# plan\n")
    for cwd in (ws, s):                   # in no git repo; in the store's own
        monkeypatch.chdir(cwd)
        rc = cli.main(["--dir", str(s), "add", "note", "--target", "item:x",
                       "--ref", "doc:plan", "--body", str(cwd)])
        assert rc == 0, capsys.readouterr().err
    assert [n.refs[0].name for n in _heads(ws)] == ["plan.md"] * 2


def test_a_path_into_a_linked_worktree_names_its_repo(ws, sym):
    wt = ws / "app-wt"
    _git(ws / "tf-app", "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "dns.tf").write_text("x\n")
    rc, out, err = sym("add", "note", "--target", "file:../app-wt/dns.tf", "--body", "x")
    assert rc == 0, err
    assert ("app", "dns.tf") in _objs(ws)


def test_from_outside_every_repo_only_a_path_or_one_holder_answers(ws, sym):
    assert sym("add", "note", "--target", "file:site.yml", "--body", "a", at=None)[0] == 0
    assert sym("add", "note", "--target", "file:./tf-app/outputs.tf", "--body", "b",
               at=None)[0] == 0
    assert {("ansible", "site.yml"), ("app", "outputs.tf")} <= _objs(ws)
    rc, out, err = sym("add", "note", "--target", "file:main.tf", "--body", "c", at=None)
    assert rc == 1 and "matches net:main.tf and app:main.tf" in err, err
    rc, out, err = sym("add", "note", "--target", "file:new.tf", "--body", "d", at=None)
    assert rc == 1 and "give its path, or write REPO:new.tf" in err, err


def test_one_batch_lands_file_rows_in_each_repo_that_holds_them(ws, sym):
    """Each row of one batch lands in its own repo, and the pending names stay per repo."""
    p = ws / "entries.jsonl"
    p.write_text("".join(json.dumps({"kind": "note", "target": f"file:{n}"}) + "\n"
                         for n in ("site.yml", "outputs.tf", "vpc/main.tf")))
    rc, out, err = sym("add", "--from-json", str(p))
    assert rc == 0, err
    assert _objs(ws) == {("ansible", "site.yml"), ("app", "outputs.tf"),
                         ("net", "modules/vpc/main.tf")}


def test_the_store_refuses_a_file_ref_the_rule_missed(ws, sym, monkeypatch):
    monkeypatch.setattr(gitref, "file_target",
                        lambda cfg, q, extra=None, say=True, cache=None: ("file", q))
    rc, out, err = sym("add", "note", "--target", "item:x", "--ref", "file:site.yml",
                       "--body", "x")
    assert rc == 1 and "unknown target type: 'file'" in err, err
    assert _heads(ws) == []


def test_a_row_written_before_repos_keeps_its_file_ref_through_an_edit(ws, sym):
    s = ws / "work-notes"
    (s / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    rc, nid, err = sym("add", "note", "--target", "item:x", "--ref", "file:main.tf",
                       "--body", "x")
    assert rc == 0, err
    (s / "symbion.toml").write_text(TOML)
    rc, out, err = sym("supersede", nid.strip(), "--body", "edited")
    assert rc == 0, err


def test_file_skips_a_listed_repo_this_machine_lacks(ws, sym):
    """`file:` skips a missing repo, and says so."""
    (ws / "work-notes" / "symbion.toml").write_text(TOML + 'dns = "../tf-dns"\n')
    rc, out, err = sym("add", "note", "--target", "file:site.yml", "--body", "x")
    assert rc == 0, err
    assert "note: file:site.yml skips dns: not on this machine" in err


def test_a_scope_and_a_to_type_take_a_repo_never_file(ws, sym):
    rc, out, err = sym("arc", "create", "--name", "x", "--scope", "file")
    assert rc == 2 and "choose from" in err and "net" in err
    rc, out, err = sym("rename", "a", "b", "--type", "item", "--to-type", "file")
    assert rc == 2 and "choose from" in err


def test_a_path_resolves_from_the_cwd_in_a_store_without_repos(tmp_path, monkeypatch,
                                                               capsys):
    """A bare name keeps the rule from the root: in a store without repos,
    `file:x.py` is also the form `list` prints."""
    r = _repo(tmp_path / "p", "src/x.py", "x.py")
    _commit(r)
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text(f'[catalogs]\nfile = "{config.FILE_CATALOG}"\n')
    monkeypatch.chdir(r / "src")

    def add(name):
        rc = cli.main(["add", "note", "--target", f"file:{name}", "--body", name])
        return rc, capsys.readouterr().err
    # `lib/../x.py` was stored as typed: a `..` inside makes a path too
    for name, want in (("./x.py", "src/x.py"), (str(r / "src" / "x.py"), "src/x.py"),
                       ("x.py", "x.py"), ("lib/../x.py", "src/x.py")):
        rc, err = add(name)
        assert rc == 0, (name, err)
        n = next(n for n in store.heads(store.load(s)) if n.body == name)
        assert (n.target.type, n.target.name) == ("file", want), name
    rc, err = add("../../elsewhere.py")
    assert rc == 1 and "outside this project" in err, err


def test_each_read_form_takes_file_as_a_write_does(ws, sym):
    """One assertion per form: a read that skips the rule prints a silent 0."""
    for t in ("ansible:site.yml", "net:main.tf", "app:main.tf"):
        assert sym("add", "note", "--target", t, "--body", t)[0] == 0
    for args in (("context", "--target", "file:site.yml", "--json"),
                 ("list", "--type", "file", "--name", "site.yml", "--json"),
                 ("list", "--name", "file:site.yml", "--json"),
                 ("list", "--target", "file:site", "--json"),
                 ("list", "--target", "file:../ansible/site.yml", "--json")):
        rc, out, err = sym(*args, at="tf-app")
        assert rc == 0, (args, err)
        rows = json.loads(out)
        rows = rows["notes"] if isinstance(rows, dict) else rows
        assert [r["body"] for r in rows] == ["ansible:site.yml"], args
    rc, out, err = sym("list", "--type", "file", "--json")
    assert sorted(r["body"] for r in json.loads(out)) == [
        "ansible:site.yml", "app:main.tf", "net:main.tf"]


def test_a_departed_files_rows_read_by_file(ws, sym):
    assert sym("add", "note", "--target", "app:outputs.tf", "--body", "x")[0] == 0
    _git(ws / "tf-app", "rm", "-q", "outputs.tf")
    _commit(ws / "tf-app", "rm")
    rc, out, err = sym("list", "--name", "file:outputs.tf", "--json")
    assert rc == 0 and len(json.loads(out)) == 1, err


def test_list_type_file_counts_what_its_status_filter_hides(ws, sym):
    """The hidden-count footer re-queries with the same tuple of repo types."""
    assert sym("add", "task", "--target", "net:main.tf", "--body", "a")[0] == 0
    nid = sym("add", "task", "--target", "app:outputs.tf", "--body", "b")[1].strip()
    assert sym("resolve", nid)[0] == 0
    rc, out, err = sym("list", "--type", "file", "--status", "open")
    assert rc == 0 and "+1 resolved (--status resolved)" in out, out


def test_a_read_takes_a_path_from_the_cwd_in_a_store_without_repos(tmp_path, monkeypatch,
                                                                   capsys):
    r = _repo(tmp_path / "p", "src/x.py")
    _commit(r)
    s = tmp_path / "p-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text(f'[catalogs]\nfile = "{config.FILE_CATALOG}"\n')
    monkeypatch.chdir(r)
    assert cli.main(["add", "note", "--target", "file:src/x.py", "--body", "x"]) == 0
    monkeypatch.chdir(r / "src")
    capsys.readouterr()
    assert cli.main(["list", "--target", "file:./x.py", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_a_commit_peels_in_the_repo_that_holds_it(ws, sym):
    net = _git(ws / "tf-net", "rev-parse", "HEAD")
    app = _git(ws / "tf-app", "rev-parse", "HEAD")
    assert sym("add", "note", "--target", "commit:HEAD", "--body", "here")[0] == 0
    rc, out, err = sym("add", "note", "--target", f"commit:{app[:10]}", "--body", "other")
    assert rc == 0 and "does not know" not in err, err
    assert {n.body: n.target.name for n in _heads(ws)} == {"here": net, "other": app}
    rc, out, err = sym("context", "--commit", app[:10], "--json")
    assert rc == 0 and "does not know" not in err
    assert [r["body"] for r in json.loads(out)["notes"]] == ["other"]
    rc, out, err = sym("list", "--type", "commit")
    assert "tf-app c0" in out
    rc, out, err = sym("add", "note", "--target", "commit:main", "--body", "x", at=None)
    assert rc == 1 and "names a different commit in each of ansible, net, app" in err, err


def test_recent_takes_the_newest_commits_of_every_repo_by_time(ws, monkeypatch):
    """The commit picker offers the newest commits first (the user,
    2026-10-16). In a store over several repos, a commit may be any repo's."""
    for day, name in enumerate(("ansible", "tf-net", "tf-app"), start=1):
        monkeypatch.setenv("GIT_COMMITTER_DATE", f"2030-01-0{day}T00:00:00Z")
        _commit(ws / name, f"{name} late")
    monkeypatch.chdir(ws / "tf-net")
    got = gitref.recent(api.resolve().cfg, 2)
    assert list(got.values()) == ["tf-app late", "tf-net late"]


def test_unmerged_asks_each_repo_for_its_own_shas(ws, monkeypatch):
    app = ws / "tf-app"
    _git(app, "checkout", "-q", "-b", "fix")
    fix = _commit(app, "fix")
    _git(app, "checkout", "-q", "main")
    monkeypatch.chdir(ws / "tf-net")
    assert gitref.unmerged(api.resolve().cfg, [fix]) == {fix: "fix"}


def _check(sym, target, at="tf-net"):
    rc, out, err = sym("add", "check", "--target", target, "--checked", "plan",
                       "--result", "clean", at=at)
    assert rc == 0, err
    return out.strip()


def _state(sym, nid, at):
    rc, out, err = sym("show", nid, "--json", at=at)
    assert rc == 0, err
    row = json.loads(out)[0]
    return row["state"], row["distance"]


def test_a_check_on_a_repos_file_reads_that_repos_head_from_anywhere(ws, sym):
    nid = _check(sym, "net:main.tf", at="ansible")
    assert _heads(ws)[0].provenance["repo"] == "net"
    for at in ("ansible", "tf-net", "tf-app", None):
        assert _state(sym, nid, at) == ("current", 0), at
    _commit(ws / "tf-app", "app moves")
    assert _state(sym, nid, "tf-app") == ("current", 0)
    _commit(ws / "tf-net", "net moves")
    for at in ("ansible", "tf-net", "tf-app", None):
        assert _state(sym, nid, at) == ("behind", 1), at


def test_a_check_on_no_repos_object_stamps_where_you_stand(ws, sym):
    _check(sym, "item:dns", at="tf-app")
    assert _heads(ws)[0].provenance["repo"] == "app"
    rc, out, err = sym("add", "check", "--target", "item:dns", "--checked", "c",
                       "--result", "r", at=None)
    assert rc == 1 and "none of this store's repos" in err, err
    nid = _check(sym, "net:main.tf", at=None)
    assert next(n for n in _heads(ws) if n.id == nid).provenance["repo"] == "net"
    nid = _check(sym, f"commit:{_git(ws / 'tf-app', 'rev-parse', 'HEAD')}", at="ansible")
    assert next(n for n in _heads(ws) if n.id == nid).provenance["repo"] == "app"
    nid = _check(sym, "file:outputs.tf", at="ansible")
    assert next(n for n in _heads(ws) if n.id == nid).provenance["repo"] == "app"


def test_a_stamp_with_no_listed_repo_reads_where_its_sha_is(ws, sym):
    """No `repo` (written before [repos]), and a `repo`
    since removed from [repos]."""
    app = _git(ws / "tf-app", "rev-parse", "HEAD")
    rows = [("none", {"sha": app, "dirty": False}),
            ("gone", {"sha": app, "dirty": False, "repo": "terraform-app"}),
            ("lost", {"sha": "f" * 40, "dirty": False})]
    store.add_many(ws / "work-notes", [
        {"kind": "check", "target": {"type": "item", "name": name}, "checked": "c",
         "result": "r", "provenance": prov} for name, prov in rows])
    rc, out, err = sym("list", "--kind", "check", "--json", at="ansible")
    assert {r["target"]["name"]: r["state"] for r in json.loads(out)} == {
        "none": "current", "gone": "current", "lost": "unverifiable"}


def test_resolving_a_prediction_stamps_its_targets_repo(ws, sym):
    rc, nid, err = sym("add", "prediction", "--target", "net:main.tf", "--checked", "plan",
                       "--body", "clean", at="tf-app")
    assert rc == 0, err
    head = _commit(ws / "tf-net", "net moves")
    assert sym("resolve", nid.strip(), "--result", "clean", at="tf-app")[0] == 0
    prov = _heads(ws)[0].provenance
    assert (prov["repo"], prov["sha"]) == ("net", head)


def test_a_check_on_a_missing_repo_names_its_path(ws, sym):
    """A check on a missing repo is refused, naming its path."""
    (ws / "work-notes" / "symbion.toml").write_text(TOML + 'dns = "../tf-dns"\n')
    rc, out, err = sym("add", "check", "--target", "dns:main.tf", "--checked", "c",
                       "--result", "r")
    assert rc == 1 and "'dns' is listed under [repos] at" in err, err


def test_the_summary_names_the_store_its_repos_and_where_you_stand(ws, sym):
    rc, out, err = sym("summary")
    assert rc == 0, err
    assert ("repos in work-notes: ansible ../ansible, net (here), app ../tf-app; "
            "file:PATH is read from where you stand, REPO:PATH from that repo's root") in out
    d = json.loads(sym("summary", "--json", at=X)[1])
    assert [(r["name"], r["from_cwd"], r["here"]) for r in d["repos"]] == [
        ("ansible", "../../../ansible", False), ("net", "../..", True),
        ("app", "../../../tf-app", False)]
    assert d["unlisted_repo"] is None


def test_the_summary_gives_an_unlisted_repo_the_line_to_add(ws, sym):
    r = _repo(ws / "tf-dns", "main.tf")
    (r / ".symbion").write_text("../work-notes\n")
    _commit(r)
    rc, out, err = sym("summary", at="tf-dns")
    assert ('this repo, tf-dns, names the store and is not listed: add '
            '`tf-dns = "../tf-dns"` under [repos]') in out, out


def test_the_last_wrap_counts_each_repos_commits(ws, sym, monkeypatch):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-01T00:00:00Z")
    assert sym("add", "note", "--target", "item:x", "--body", "x")[0] == 0
    assert sym("commit", "--wrap", "-m", "day")[0] == 0
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2031-01-01T00:00:00Z")
    for r in ("tf-net", "tf-net", "tf-app"):
        _commit(ws / r)
    rc, out, err = sym("summary")
    assert "3 commits since (net 2, app 1)" in out, out


def test_schema_lists_each_repo_with_its_path_and_command(ws, sym):
    t = {x["type"]: x for x in json.loads(sym("schema", "--json")[1])["targets"]}
    assert t["net"]["repo"] == str((ws / "tf-net").resolve())
    assert t["net"]["catalog"] == config.FILE_CATALOG
    assert "repo: " + str((ws / "tf-net").resolve()) in sym("schema")[1]


def test_converting_a_one_repo_store_moves_its_file_rows_as_stored(tmp_path, monkeypatch, capsys):
    """The spec's conversion, one `rename` with no names: targets and refs
    move as stored, a departed path that is a unique substring of a live one
    included; `--type file` is taken while rows of it remain; the old stamps
    read current. A per-name rename resolves its new name, and moved
    `a/b.txt` onto `legacy/a/b.txt`."""
    r = _repo(tmp_path / "ansible", "site.yml", "hosts.yml", "a/b.txt")
    _commit(r)
    s = tmp_path / "ansible-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text(f'[catalogs]\nfile = "{config.FILE_CATALOG}"\n')
    monkeypatch.chdir(r)

    def sym(*a):
        rc = cli.main(list(a))
        out, err = capsys.readouterr()
        assert rc == 0, (a, err)
        return out
    sym("add", "note", "--target", "file:site.yml", "--ref", "file:hosts.yml", "--body", "x")
    sym("add", "note", "--target", "file:a/b.txt", "--body", "departs")
    (r / "legacy").mkdir()
    _git(r, "mv", "a", "legacy/a")
    _commit(r, "move")
    chk = sym("add", "check", "--target", "file:site.yml", "--checked", "c",
              "--result", "r").strip()
    (s / "symbion.toml").write_text('[repos]\nansible = "../ansible"\n')
    sym("rename", "--type", "file", "--to-type", "ansible")
    assert {(o.type, o.name) for n in store.heads(store.load(s))
            for o in (n.target, *n.refs)} == {
        ("ansible", "site.yml"), ("ansible", "hosts.yml"), ("ansible", "a/b.txt")}
    assert json.loads(sym("show", chk, "--json"))[0]["state"] == "current"


def test_rename_takes_both_names_or_neither_and_then_another_type(ws, sym):
    rc, out, err = sym("rename", "--type", "net")
    assert rc == 2 and "--to-type" in err, err
    rc, out, err = sym("rename", "main.tf", "--type", "net")
    assert rc == 2 and "OLD and NEW" in err, err


def test_rename_moves_a_short_commit_onto_the_full_id_of_its_repo(ws, sym):
    app = _git(ws / "tf-app", "rev-parse", "HEAD")
    store.add_many(ws / "work-notes", [{"kind": "note", "body": "x",
                                         "target": {"type": "commit", "name": app[:7]}}])
    rc, out, err = sym("rename", app[:7], app[:7], "--type", "commit")
    assert rc == 0, err
    assert [n.target.name for n in _heads(ws)] == [app]


def test_rename_type_refuses_a_type_neither_declared_nor_held(ws, sym):
    rc, out, err = sym("rename", "a", "b", "--type", "nosuch")
    assert rc == 2 and "invalid type 'nosuch'" in err and "choose from" in err


def test_the_viewer_reads_a_repo_rows_file_in_its_repo(ws, monkeypatch):
    pytest.importorskip("nicegui")
    from symbion.gui.notes import view_path
    monkeypatch.chdir(ws / "tf-app")
    ctx = api.resolve()
    assert view_path(ctx, "site.yml", "ansible") == ((ws / "ansible" / "site.yml").resolve(), None)
    assert view_path(ctx, "site.yml") is None


def test_the_viewer_reads_a_file_only_its_repos_default_branch_holds(ws, monkeypatch):
    """A ref to a .tf file had no view button in a store over several repos:
    the file was on main, and that repo had another branch checked out (the
    user, 2026-10-15). The repo's default branch holds the copy shown."""
    pytest.importorskip("nicegui")
    from symbion.gui.notes import view_path
    net = ws / "tf-net"
    base = _git(net, "rev-parse", "HEAD")
    (net / "modules" / "dns").mkdir(parents=True)
    (net / "modules" / "dns" / "main.tf").write_text('resource "dns_zone" "z" {}\n')
    _commit(net, "dns")
    _git(net, "checkout", "-q", "-b", "wip", base)
    monkeypatch.chdir(ws / "tf-app")
    ctx = api.resolve()
    assert view_path(ctx, "modules/dns/main.tf", "net") == (None, "main")
    assert "dns_zone" in gitref.blob_text(ctx.cfg.for_type("net"), "main", "modules/dns/main.tf")
    assert view_path(ctx, "modules/dns/main.tf", "app") is None, "another repo's main lacks it"
    assert view_path(ctx, "modules/nowhere.tf", "net") is None


def test_a_check_tip_lists_the_commits_of_its_repo(ws, sym, monkeypatch):
    pytest.importorskip("nicegui")
    from symbion.gui.notes import check_tip
    _check(sym, "net:main.tf", at="ansible")
    _commit(ws / "tf-net", "net moves")
    monkeypatch.chdir(ws / "ansible")
    ctx, n = api.resolve(), _heads(ws)[0]
    state, distance = gitref.check_state(ctx.cfg, n.provenance)
    assert (state, distance) == ("behind", 1)
    assert "net moves" in check_tip(ctx.cfg, n.provenance, state, distance)


def test_a_rewritten_stamp_is_looked_up_in_its_own_repo(ws, sym, monkeypatch):
    """Read from another repo, the stamp's successor is still searched for
    where the stamp was taken, by the state and by the printers that name
    the successor."""
    nid = _check(sym, "net:main.tf", at="ansible")
    net = ws / "tf-net"
    (net / "x.tf").write_text("x\n")
    _git(net, "add", "-A")
    _git(net, "commit", "-q", "--amend", "--no-edit")
    monkeypatch.chdir(ws / "ansible")
    ctx, n = api.resolve(), _heads(ws)[0]
    assert gitref.check_state(ctx.cfg, n.provenance) == ("rewritten", 0)
    rc, out, err = sym("show", nid, "--json", at="ansible")
    row = json.loads(out)[0]
    assert (row["state"], row.get("successor_state"), row.get("same_tree")) == (
        "rewritten", "current", False), err


def test_a_scope_reads_each_repos_head_once(ws, sym, monkeypatch):
    """A page of checks stamped in a repo the serve does not stand in read
    that repo's HEAD once a row: a home page of 200 took 2.07 s against
    0.16 s (2026-10-14). Inside `heads_once` each repo's HEAD is read
    once; outside one every read is fresh, so a commit between reads shows."""
    for _ in range(3):
        _check(sym, "app:main.tf", at="tf-net")
    monkeypatch.chdir(ws / "tf-net")
    ctx, rows = api.resolve(), _heads(ws)
    head, real, reads = gitref.head_sha(ctx.cfg), gitref.head_sha, []
    monkeypatch.setattr(gitref, "head_sha", lambda cfg: reads.append(1) or real(cfg))
    with gitref.heads_once():
        assert {api.verdict_state(ctx.cfg, n, head)[0] for n in rows} == {"current"}
    assert len(reads) == 1, "app's HEAD, once for three rows"
    _commit(ws / "tf-app", "app moves")
    assert {api.verdict_state(ctx.cfg, n, head)[0] for n in rows} == {"behind"}
    with gitref.heads_once():
        assert {api.verdict_state(ctx.cfg, n, head)[0] for n in rows} == {"behind"}


def test_the_recent_board_holds_a_check_current_in_another_repo(ws, sym, monkeypatch):
    pytest.importorskip("nicegui")
    from symbion.gui import pages
    _check(sym, "app:main.tf", at="tf-net")
    monkeypatch.chdir(ws / "tf-net")
    ctx, n = api.resolve(), _heads(ws)[0]
    later = store.written_at(n) + timedelta(days=1)
    assert pages._recent(ctx, n, later, gitref.head_sha(ctx.cfg))


def test_a_checks_stamp_follows_the_repo_its_target_resolves_to_in_a_batch(ws, sym):
    """The stamp is taken under the lock, after the target resolves beside
    the batch's other names: row 2's `file:new.tf` is app's new file from
    row 1, so its check stamps app, not the repo the cwd stands in."""
    p = ws / "entries.jsonl"
    p.write_text(json.dumps({"kind": "note", "target": "file:../../../tf-app/new.tf"}) + "\n"
                 + json.dumps({"kind": "check", "target": "file:new.tf", "checked": "c",
                               "result": "r"}) + "\n")
    rc, out, err = sym("add", "--from-json", str(p), at=X)
    assert rc == 0, err
    chk = next(n for n in _heads(ws) if n.kind == "check")
    assert (chk.target.type, chk.target.name) == ("app", "new.tf")
    assert chk.provenance["repo"] == "app"


def test_a_larger_batch_of_file_rows_runs_no_more_catalogs(ws, sym, monkeypatch):
    """A batch of `file:` rows asks each repo's catalog once, as a batch of
    `REPO:` rows does: the pool is the write's, not each name's. Measured
    before: 20 rows ran 61 catalogs."""
    ran = []
    real = catalog.run_configured

    def spy(cfg, cmd, input=None):
        ran.append(cfg.work_root)
        return real(cfg, cmd, input)
    monkeypatch.setattr(catalog, "run_configured", spy)
    counts = []
    for n in (2, 5):
        ran.clear()
        p = ws / "entries.jsonl"
        p.write_text("".join(json.dumps({"kind": "note", "target": "file:site.yml",
                                         "body": f"{n}.{i}"}) + "\n" for i in range(n)))
        rc, out, err = sym("add", "--from-json", str(p))
        assert rc == 0, err
        counts.append(len(ran))
    assert counts[0] == counts[1], counts


def test_the_recent_board_holds_a_current_check_when_the_serve_stands_in_no_repo(
        ws, sym, monkeypatch):
    pytest.importorskip("nicegui")
    from symbion.gui import pages
    _check(sym, "net:main.tf")
    monkeypatch.chdir(ws)
    ctx = api.resolve(str(ws / "work-notes"))
    n = _heads(ws)[0]
    assert api.verdict_state(ctx.cfg, n)[0] == "current"
    later = store.written_at(n) + timedelta(days=1)
    assert pages._recent(ctx, n, later, gitref.head_sha(ctx.cfg))


def test_list_type_file_also_lists_rows_still_on_file(ws, sym):
    """A store converted to [repos] may still hold rows on `file`, which
    `list --type file` must show: they are what is left to convert."""
    store.add_many(ws / "work-notes", [{"kind": "note", "body": "old",
                                         "target": {"type": "file", "name": "main.tf"}}])
    assert sym("add", "note", "--target", "net:main.tf", "--body", "new")[0] == 0
    rc, out, err = sym("list", "--type", "file", "--json")
    assert rc == 0, err
    assert sorted(r["body"] for r in json.loads(out)) == ["new", "old"]


# The repos branch's final review, six edges (each read at b552b14).

def test_rename_within_a_type_the_store_dropped_names_to_type(ws, sym):
    """It said `unknown target type: 'file'`, and nothing of --to-type."""
    store.add_many(ws / "work-notes", [{"kind": "note", "body": "x",
                                         "target": {"type": "file", "name": "main.tf"}}])
    rc, out, err = sym("rename", "main.tf", "x.tf", "--type", "file")
    assert rc == 2 and "--to-type REPO (ansible, net, app)" in err, err
    rc, out, err = sym("rename", "main.tf", "main.tf", "--type", "file", "--to-type", "net")
    assert rc == 0 and out.startswith("re-targeted 1 entry, re-pointed 0 refs"), err


def test_a_dot_segment_at_a_repos_root_notes_where_it_landed(ws, sym):
    rc, out, err = sym("add", "note", "--target", "file:environments/../main.tf", "--body", "x")
    assert rc == 0 and "is net:main.tf" in err and "a new file" not in err, err


def test_a_read_of_a_missing_file_is_worded_as_a_read(ws, sym):
    for args in (("list", "--target", "file:nosuch.tf"),
                 ("context", "--target", "file:nosuch.tf")):
        rc, out, err = sym(*args)
        assert "read as net:nosuch.tf" in err and "a new file" not in err, (args, err)


def test_a_closed_repo_takes_no_new_file(ws, sym):
    """`[resolvers] net = "closed"`: a name no repo holds was taken as a new
    file in the repo you stand in, past the closed catalog. A read of it is
    still a read."""
    (ws / "work-notes" / "symbion.toml").write_text(TOML + '[resolvers]\nnet = "closed"\n')
    rc, out, err = sym("add", "note", "--target", "file:nosuch.tf", "--body", "x")
    assert rc == 1 and "file:nosuch.tf matches nothing in any repo, and the net catalog is " \
        "closed" in err, err
    rc, out, err = sym("list", "--target", "file:nosuch.tf")
    assert rc == 0 and "read as net:nosuch.tf" in err, err
    rc, out, err = sym("add", "note", "--target", "file:nosuch.tf", "--body", "x", at="tf-app")
    assert rc == 0 and "taken as app:nosuch.tf, a new file" in err, "app is open"


def test_branch_outside_every_repo_names_the_repos(ws, sym):
    """It printed git's `not a git repository`."""
    rc, out, err = sym("context", "--branch", "main", at=None)
    assert rc == 1 and "none of this store's repos (ansible, net, app)" in err, err


def test_a_directory_typed_as_a_path_is_noted_as_one(ws, sym):
    rc, out, err = sym("add", "note", "--target", "file:./modules", "--body", "x")
    assert rc == 0 and "is net:modules, a directory" in err, err


def test_a_batch_footer_leaves_out_a_name_a_path_placed(ws, sym):
    """Its own note said where it went; the footer said `stored as typed`.
    Both directions: a catalog miss still reaches the footer."""
    rows = ws / "rows.jsonl"
    rows.write_text("".join(json.dumps({"kind": "note", "body": b, "target": t}) + "\n"
                            for b, t in (("a", {"type": "file", "name": "./new.tf"}),
                                         ("b", {"type": "net", "name": "typo.tf"}))))
    rc, out, err = sym("add", "--from-json", str(rows))
    assert rc == 0 and "1 of 1 names not in their catalog, stored as typed: net:typo.tf\n" \
        in err, err
