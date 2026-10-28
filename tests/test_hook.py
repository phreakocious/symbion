import json
import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

from symbion import store
from symbion.config import Config
from symbion.catalog import posix_sh

HOOK = Path(__file__).resolve().parents[1] / "src/symbion/data/skill/session_start.sh"

# Resolved once, via the test runner's own PATH, and invoked by absolute path.
# subprocess.run(["sh", ...], env={**SRC_ENV, "PATH": ...}) resolves argv[0] itself
# through the CHILD's PATH (os.get_exec_path(env)) -- so a PATH that is empty
# or wrong (as test_hook_exits_0_when_symbion_is_not_installed needs, to prove
# symbion is genuinely unreachable) makes the shell itself unresolvable and raises
# FileNotFoundError in the parent before the hook ever runs. An absolute path
# has a dirname, so Python skips PATH search for the interpreter while the
# hook's own `command -v symbion` still runs inside the broken env below.
# A strict POSIX shell where there is one: macOS's /bin/sh is bash and
# accepts bashisms, and a system without bash ran no hook at all (2026-10-05).
# On Windows, the sh Git for Windows installs, which Claude Code runs hooks under.
SH = shutil.which("dash") or ("/bin/sh" if os.name != "nt" else posix_sh())
if SH is None:
    pytest.skip("no sh: Git for Windows installs one", allow_module_level=True)

# The dir holding the `symbion` console script this suite itself runs under
# -- NOT from shutil.which("symbion"), which returns None whenever the calling
# shell hasn't activated the venv (true of this very test run). sysconfig's
# scripts dir is where pip put it for this interpreter: a venv's bin (Scripts
# on Windows), or outside a venv the install's own, which on Windows is
# Scripts beside python.exe, not python.exe's dir (CI's setup-python layout).
SYMBION_BIN = str(Path(sysconfig.get_path("scripts")))
# That script imports symbion through the venv's editable install; this
# tree's src/ goes first (conftest.py says why).
SRC_ENV = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
# Windows will not start a process with no SYSTEMROOT in its environment.
# Nor will Path.home() answer there without USERPROFILE.
SRC_ENV.update({k: os.environ[k] for k in ("SYSTEMROOT", "USERPROFILE") if k in os.environ})
# The hook's PATH: this venv's `symbion` first, then the git the suite runs
# under. A bare `/usr/bin:/bin` held no git where packages install to
# /usr/local/bin (OpenBSD, 2026-10-05), and `symbion` died on it.
TOOLS = os.pathsep.join([SYMBION_BIN, str(Path(shutil.which("git")).parent), "/usr/bin", "/bin"])



def _context(stdout):
    """What the agent reads: a JSON reply's additionalContext, else stdout
    as it is (an error, or the hook's own not-on-PATH line)."""
    try:
        return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]
    except ValueError:
        return stdout

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo -- config.project_root() shells out to
    `git worktree list --porcelain` and needs one, or symbion itself exits
    nonzero with 'not a git repository' before ever reaching the store."""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "f.txt").write_text("x")
    _git(r, "add", "-A")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return r


def test_hook_prints_the_zero_line_for_an_empty_but_existing_store(git_repo, tmp_path):
    """symbion IS resolvable, cwd IS a git repo, and the store DOES exist
    (just empty) -- proves this actually reached `symbion summary` and ran
    its real code path, rather than exiting early at `command -v symbion`
    like the sibling test below. A vacuous version of this test would pass
    on an early exit too, since both print nothing to stderr and return 0."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS,
                            "SYMBION_DIR": str(store_dir)})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "bug 0" in _context(r.stdout)


def test_hook_shows_the_person_one_line_and_the_agent_the_summary(git_repo, tmp_path):
    """A SessionStart hook's plain stdout reaches only the agent; the person
    saw nothing. systemMessage is the line Claude Code prints to them."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    store.add(store_dir, kind="bug", target={"type": "project", "name": None},
              body="the open head", status="open")
    env = {**SRC_ENV, "PATH": TOOLS, "SYMBION_DIR": str(store_dir)}
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stderr == ""
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "the open head" in text
    assert out["systemMessage"] == text.splitlines()[0] and "bug 1" in out["systemMessage"]
    # Codex shows a systemMessage as a warning: the agent's text only.
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                       env={**env, "SYMBION_AUTHOR": "codex"})
    assert list(json.loads(r.stdout)) == ["hookSpecificOutput"]


def test_hook_carries_the_last_wrap(git_repo, tmp_path):
    """The handoff is the wrap commit's message, so the hook hands it to the
    agent where a handoff file would have been read."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    store.add(store_dir, kind="note", target={"type": "project", "name": None}, body="x")
    store.commit(store_dir, "the gist\n\nread this first", Config(project_root=git_repo),
                 wrap="ada")
    env = {**SRC_ENV, "PATH": TOOLS, "SYMBION_DIR": str(store_dir)}
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stderr == ""
    text = _context(r.stdout)
    assert "last wrap by ada, just now" in text and "    read this first" in text, text


def test_hook_names_the_absent_store(git_repo, tmp_path):
    """Amended 2026-09-20: `init` creates the store and installs
    the hook together, so the hook running against an absent store is a
    pointer to nowhere, a renamed repo or a wrong SYMBION_DIR -- never a
    first session -- and silence there read as "no store yet" twice in the
    field while the next `add` forked a second store. Exit 0 and empty
    stderr still hold: the hook never fails a session. The sibling test
    below keeps the genuinely quiet direction (not installed, no store)."""
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS,
                            "SYMBION_DIR": str(tmp_path / "nope")})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "no store at" in _context(r.stdout) and str(tmp_path / "nope") in _context(r.stdout)
    assert "bug 0" not in _context(r.stdout), "an absent store must not print the zero-counts line"
    assert "no store at" in json.loads(r.stdout)["systemMessage"], "the person must see it too"


def test_hook_is_silent_in_a_project_that_never_adopted_symbion(git_repo):
    """User-level since 2026-09-24, the hook runs in EVERY project. No store,
    no `.symbion` pointer and no SYMBION_DIR is a project that never adopted
    symbion, and it must hear nothing -- the tests around this one keep the
    loud directions (a named store that is absent, a store with rows)."""
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_hook_names_a_pointer_to_a_store_that_is_not_there(git_repo):
    (git_repo / ".symbion").write_text("../gone-notes\n")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS})
    assert r.returncode == 0 and "no store at" in _context(r.stdout) and "gone-notes" in _context(r.stdout)


def test_hook_names_a_pointer_that_links_to_a_directory(git_repo, symlink):
    """`read` on a directory printed bash's `read error: Is a directory` and
    the hook went silent, as in a project that never adopted symbion."""
    (git_repo.parent / "elsewhere").mkdir()
    symlink(git_repo / ".symbion", "../elsewhere", directory=True)
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS})
    assert r.returncode == 0 and "read error" not in _context(r.stdout) + r.stderr
    assert "is a link to ../elsewhere" in _context(r.stdout)


def test_hook_exits_0_and_prints_nothing_when_symbion_is_not_installed_and_no_store(tmp_path):
    """The quiet direction of the not-installed case: no sibling store, so
    this is a project that never adopted symbion, and the hook must say
    nothing -- the loud test below covers the other direction."""
    r = subprocess.run([SH, str(HOOK)], cwd=tmp_path,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": "/nonexistent"})
    assert r.returncode == 0
    assert r.stderr == ""
    assert r.stdout == ""


def test_hook_names_the_missing_binary_when_a_sibling_store_exists(git_repo, tmp_path):
    """The loud direction. A project whose sibling `<name>-notes` store exists
    but whose `symbion` is off PATH is not a project that never adopted
    symbion -- silence there hides exactly the failure the hook exists to
    report (measured 2026-09-04: this repo's own read loop was dead for a
    session because the venv script was never put on PATH). With PATH set to
    nowhere the hook can use only bash builtins to find the store."""
    store_dir = tmp_path / "proj-notes"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": "/nonexistent"})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "not on PATH" in _context(r.stdout)


def test_hook_summarizes_the_project_dir_not_the_cwd(tmp_path):
    """`root` used to be assigned INSIDE the not-installed branch, so the
    installed path never saw it: the hook checked CLAUDE_PROJECT_DIR's store
    for existence and then summarized whatever project the cwd happened to be.
    Two stores here, and only the project dir's has the bug -- reading the
    cwd's gives 0 and passes every other assertion in this file."""
    other, proj = tmp_path / "other", tmp_path / "proj"
    for r in (other, proj):
        r.mkdir()
        _git(r, "init", "-q")
        (r / "f.txt").write_text("x")
        _git(r, "add", "-A")
        _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        store.ensure_store(tmp_path / f"{r.name}-notes")
    store.add(tmp_path / "proj-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")

    r = subprocess.run([SH, str(HOOK)], cwd=other, capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS,
                            "CLAUDE_PROJECT_DIR": str(proj)})
    assert r.returncode == 0 and r.stderr == ""
    assert "bug 1" in _context(r.stdout), f"summarized the cwd, not the project: {r.stdout!r}"


def test_hook_still_uses_the_cwd_when_no_project_dir_is_set(tmp_path):
    """The other direction of the same `root`: with CLAUDE_PROJECT_DIR unset it
    must stay the cwd, or the fix above would break every ordinary invocation."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / "f.txt").write_text("x")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    store.ensure_store(tmp_path / "proj-notes")
    store.add(tmp_path / "proj-notes", kind="bug",
              target={"type": "project", "name": None}, status="open")

    r = subprocess.run([SH, str(HOOK)], cwd=proj, capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS})
    assert r.returncode == 0 and r.stderr == ""
    assert "bug 1" in _context(r.stdout)


def test_hook_prints_open_heads_and_not_the_schema(git_repo, tmp_path):
    """The kinds table changes only when symbion.toml does, and SKILL.md
    already embeds it, so an agent with the skill loaded paid for it twice
    every session. The open heads are what a session cannot re-derive
    without knowing to ask. Both directions: the row is there, the table
    is not."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text('[kinds]\nanomaly = { status = true }\n')
    store.add(store_dir, kind="anomaly", target={"type": "project", "name": None},
              body="latency drifts 2 ms", status="open")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS,
                            "SYMBION_DIR": str(store_dir)})
    assert r.returncode == 0 and r.stderr == ""
    lines = _context(r.stdout).splitlines()
    # SYMBION_DIR names a store the repo's tree does not, so the header
    # says which one: see test_summary_names_a_store_set_by_the_environment.
    assert lines[0].startswith("symbion --dir ../store: open outside arcs: anomaly 1")
    assert any("latency drifts 2 ms" in l for l in lines[1:]), "the open head itself"
    assert not any(l.startswith("kinds  (symbion.toml [kinds])") for l in lines)


def test_hook_finds_a_pointed_store_with_no_symbion_on_path(git_repo, tmp_path):
    """The bash twin of config.store_dir's pointer. The store is NOT the
    sibling the repo name implies, so without reading `.symbion` the hook
    goes silent -- which reads as "never adopted" and is the exact failure
    this branch exists to report."""
    store_dir = tmp_path / "elsewhere-store"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    (git_repo / ".symbion").write_text("../elsewhere-store\n")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert "not on PATH" in _context(r.stdout)
    assert "elsewhere-store" in _context(r.stdout)


@pytest.mark.parametrize("via", ["pointer", "SYMBION_DIR"])
def test_hook_names_a_selected_store_that_is_missing_with_no_symbion_on_path(git_repo, tmp_path, via):
    """A pointer or SYMBION_DIR marks a project that adopted symbion, so with
    symbion off PATH a store they name that is gone must not read as "never
    adopted" either."""
    env = {**SRC_ENV, "PATH": "/nonexistent", "HOME": str(tmp_path)}
    if via == "pointer":
        (git_repo / ".symbion").write_text("../gone-store\n")
    else:
        env["SYMBION_DIR"] = str(tmp_path / "gone-store")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stderr == ""
    assert "not on PATH" in _context(r.stdout) and "gone-store" in _context(r.stdout)


def test_hook_is_silent_on_the_same_tree_without_the_pointer(git_repo, tmp_path):
    """The other direction: identical tree and identical store, pointer
    removed. Without this the test above passes on a hook that prints the
    message unconditionally."""
    store_dir = tmp_path / "elsewhere-store"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    assert not (git_repo / ".symbion").exists()
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout == ""


def test_hook_reads_a_pointer_saved_with_crlf(git_repo, tmp_path):
    """Notepad, or text mode on Windows, ends the pointer's line in CRLF, and
    `read -r` keeps the CR: the hook named a store whose name ended in it,
    which is not there."""
    store_dir = tmp_path / "elsewhere-store"
    store_dir.mkdir()
    (store_dir / "notes.jsonl").write_text("")
    (git_repo / ".symbion").write_bytes(b"../elsewhere-store\r\n")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert "elsewhere-store because 'symbion' is not on PATH" in _context(r.stdout), r.stdout


def test_hook_ignores_a_blank_pointer(git_repo, tmp_path):
    """A blank pointer must not resolve to the repo root. If it did, a repo
    carrying its own notes.jsonl would satisfy the -e test and the hook would
    claim a store that is really the project tree."""
    (git_repo / ".symbion").write_text("\n  \n")
    (git_repo / "notes.jsonl").write_text("")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": "/nonexistent", "HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout == ""


def test_hook_names_a_malformed_kinds_table_instead_of_reading_as_no_store(git_repo, tmp_path):
    """`symbion summary 2>/dev/null` swallowed every error, so a store whose
    symbion.toml carries a broken [kinds] table started the session exactly
    like a project that never adopted symbion. The error goes to STDOUT: a
    SessionStart hook's stdout is what reaches the agent, and exit 0 with
    empty stderr still holds -- the hook never fails a session."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    (store_dir / "symbion.toml").write_text('[kinds]\nnote = { status = "yes" }\n')
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo,
                       capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS,
                            "SYMBION_DIR": str(store_dir)})
    assert r.returncode == 0
    assert r.stderr == ""
    assert "[kinds]" in _context(r.stdout) and "'note'" in _context(r.stdout), f"silent: {r.stdout!r}"


@pytest.mark.parametrize("layout", ["sibling", "pointer", "worktree pointer", "none"])
def test_hook_in_a_linked_worktree_reads_the_main_checkouts_store(git_repo, tmp_path, layout):
    """config.project_root() is the MAIN worktree, so every linked worktree
    shares its store. The hook derived `<linked-name>-notes` and exited 0
    before the CLI ran: a session started in a linked worktree heard nothing.
    The worktree sits under another parent and the pointer is untracked, so
    neither the worktree's name nor its own `.symbion` can reach the store.
    `worktree pointer`: a project adopting symbion on a branch has the
    pointer in the linked worktree first, and its path is the project's,
    so it resolves against the main checkout, not the worktree.
    `none` is the other direction: a project that never adopted stays silent."""
    wt = tmp_path / "wt" / "feature"
    _git(git_repo, "worktree", "add", "-q", "--detach", str(wt))
    s = {"sibling": tmp_path / "proj-notes", "pointer": tmp_path / "elsewhere-store",
         "worktree pointer": tmp_path / "elsewhere-store"}.get(layout)
    if s:
        store.ensure_store(s)
        store.add(s, kind="bug", target={"type": "project", "name": None}, status="open")
    if layout in ("pointer", "worktree pointer"):
        ((git_repo if layout == "pointer" else wt) / ".symbion").write_text("../elsewhere-store\n")
    r = subprocess.run([SH, str(HOOK)], cwd=wt, capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS,
                            "CLAUDE_PROJECT_DIR": str(wt)})
    assert r.returncode == 0 and r.stderr == ""
    if s:
        assert "bug 1" in _context(r.stdout), f"skipped the main checkout's store: {r.stdout!r}"
    else:
        assert r.stdout == ""


# ---- the gotcha hook ----
GOTCHAS = HOOK.with_name("gotchas.sh")


def _touch(env, payload):
    r = subprocess.run([SH, str(GOTCHAS)], input=payload if isinstance(payload, str)
                       else json.dumps(payload), capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stderr == "", r
    if not r.stdout:
        return ""
    out = json.loads(r.stdout)["hookSpecificOutput"]
    sent = payload.get("hook_event_name", "PostToolUse") if isinstance(payload, dict) else None
    assert out["hookEventName"] == sent, "Claude Code reads it as that event's answer"
    return out["additionalContext"]


def test_a_files_gotcha_reaches_a_session_the_first_time_it_touches_the_file(git_repo,
                                                                            tmp_path):
    """A trap recorded on a file reached a session only if it ran `context
    --target` on that file first, and four sessions did not, each at a cost
    (2026-10-02 to 10-10). Session start lists the session's #gotcha rows on
    catalog names, and the tool hook prints a row the first time a tool call
    names its file: once per session, until the next session start (a
    compaction drops what the agent read)."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    serve, other = ({"type": "file", "name": n} for n in ("src/serve.py", "src/other.py"))
    hit = store.add(s, kind="note", target=serve, author="ann", tags=["gotcha"],
                    body="**Start a scratch serve with XDG_CACHE_HOME.** Or a sidebar lists it.")
    via_ref = store.add(s, kind="note", target={"type": "project", "name": None}, author="ann",
                        tags=["gotcha"], refs=[other], body="ref trap")
    for target, tags, body in ((serve, [], "not a trap"), (serve, ["gotcha", "retired"], "old trap"),
                               ({"type": "item", "name": "windows"}, ["gotcha"], "item trap")):
        store.add(s, kind="note", target=target, author="ann", tags=tags, body=body)
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}

    def start(sid):
        r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                           env=env, input=json.dumps({"session_id": sid,
                                                      "hook_event_name": "SessionStart"}))
        assert r.returncode == 0 and "bug" in _context(r.stdout), r

    start("s1")
    read = {"session_id": "s1", "tool_name": "Read",
            "tool_input": {"file_path": f"{git_repo}/src/serve.py"},
            "tool_response": {"file": {"content": "import other  # src/other.py, windows"}}}
    said = _touch(env, read)
    assert hit.id[-10:] in said and "Start a scratch serve with XDG_CACHE_HOME." in said, said
    assert not any(t in said for t in ("ref trap", "not a trap", "old trap", "item trap")), said
    assert _touch(env, read) == "", "once per session"
    windows = {"session_id": "s1", "tool_name": "Edit",
               "tool_input": {"file_path": "C:\\proj\\src\\other.py", "old_string": "a"}}
    assert via_ref.id[-10:] in _touch(env, windows), "a ref names the file too"
    bash = {"session_id": "s1", "tool_name": "Bash",
            "tool_input": {"command": "sed -n 1,20p src/serve.py  # windows"}}
    assert _touch(env, bash) == ""
    start("s1")                                   # compacted: the agent read it before
    assert hit.id[-10:] in _touch(env, bash)
    assert _touch(env, {**bash, "session_id": "s2"}) == "", "no session start, no list"
    assert _touch(env, "") == "" and _touch(env, "not json") == ""


def test_a_subagent_gets_a_gotcha_the_main_agent_already_had(git_repo, tmp_path):
    """A subagent's calls shared the session's marks, so a row the main agent
    got never reached a subagent that went on to edit the file (a review,
    2026-10-16). Claude Code puts the subagent's agent_id in its calls'
    input; one among a tool's own arguments is no subagent."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    store.add(s, kind="note", target={"type": "file", "name": "README.md"}, author="ann",
              tags=["gotcha"], body="readme trap")
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env,
                       input=json.dumps({"session_id": "s1", "hook_event_name": "SessionStart"}))
    assert r.returncode == 0, r

    def call(args=None, **extra):
        return _touch(env, {"session_id": "s1", **extra, "tool_name": "Read",
                            "tool_input": {"file_path": f"{git_repo}/README.md", **(args or {})}})
    assert "readme trap" in call()
    assert call() == "", "the main agent: once"
    assert "readme trap" in call(agent_id="a1", agent_type="general-purpose")
    assert call(agent_id="a1") == "", "a subagent: once too"
    assert "readme trap" in call(agent_id="a2")
    assert call({"agent_id": "a9"}) == "", "a tool's argument named agent_id is not the call's"


def test_a_gotcha_fires_on_its_own_file_and_again_at_its_first_edit(git_repo, tmp_path):
    """A row on README.md fired on a Read of src/README.md and was then spent
    for the session (a clean-room review, 2026-10-13), and a Bash call that
    only named a file spent its edit-time trap the same way (dogfood,
    2026-10-14). A name matches at a path boundary, from the repo's root; an
    edit matches on the file it edits, not the text it writes, and gets the
    trap once more."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    store.add(s, kind="note", target={"type": "file", "name": "README.md"}, author="ann",
              tags=["gotcha"], body="readme trap")
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}

    def call(sid, tool, **tool_input):
        return _touch(env, {"session_id": sid, "tool_name": tool, "tool_input": tool_input})

    for sid in ("s1", "s2"):
        r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                           env=env, input=json.dumps({"session_id": sid,
                                                      "hook_event_name": "SessionStart"}))
        assert r.returncode == 0, r
    for tool, args in (("Read", {"file_path": f"{git_repo}/src/README.md"}),
                       ("Bash", {"command": "cat docs/README.md OLD_README.md README.md.bak"}),
                       ("Edit", {"file_path": f"{git_repo}/a.md", "new_string": "see README.md"})):
        assert call("s1", tool, **args) == "", (tool, args)
    assert "readme trap" in call("s1", "Bash", command="wc -l README.md")
    assert call("s1", "Read", file_path=f"{git_repo}/README.md") == "", "once per session"
    assert "readme trap" in call("s1", "Edit", file_path=f"{git_repo}/README.md",
                                 old_string="a", new_string="b"), "again at the first edit"
    assert call("s1", "Write", file_path=f"{git_repo}/README.md", content="b") == ""
    assert call("s1", "Read", file_path=f"{git_repo}/README.md") == ""
    assert "readme trap" in call("s2", "Bash", command="cat ./README.md")


def test_a_failed_call_gets_its_files_gotcha_too(git_repo, tmp_path):
    """A Bash call that exited non-zero, when a trap has most likely just
    bitten, heard nothing: Claude Code sends a failed call to
    PostToolUseFailure, not PostToolUse (dogfood, 2026-10-13). Its `error`,
    as a response does, names files the call did not."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    for name, body in (("src/serve.py", "serve trap"), ("src/other.py", "other trap")):
        store.add(s, kind="note", target={"type": "file", "name": name}, author="ann",
                  tags=["gotcha"], body=body)
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env,
                       input=json.dumps({"session_id": "s1", "hook_event_name": "SessionStart"}))
    assert r.returncode == 0, r
    said = _touch(env, {"session_id": "s1", "hook_event_name": "PostToolUseFailure",
                        "tool_name": "Bash", "tool_input": {"command": "pytest src/serve.py"},
                        "tool_use_id": "toolu_1", "error": "Exit code 1\nin src/other.py",
                        "is_interrupt": False})
    assert "serve trap" in said and "other trap" not in said, said


def test_a_gotcha_line_says_its_age_its_files_churn_and_how_to_retire_it(git_repo, tmp_path):
    """A gotcha has no status, so a stale one read as true at every touch
    (the user, 2026-10-14). A commit count alone cannot say stale: every
    file gotcha in one store had changed since, by one commit to hundreds. So the
    line gives the age and the churn, and how to retire the row: the agent,
    in the file at that moment, is the one placed to judge it."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')

    def commit(m):
        _git(git_repo, "add", "-A")
        _git(git_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", m)

    for f in ("serve.py", "other.py"):
        (git_repo / f).write_text("v1\n")
    commit("files")
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    tails = {}
    for f, more in (("serve.py", ""), ("other.py", " detail" * 40)):
        r = subprocess.run([str(Path(SYMBION_BIN) / "symbion"), "add", "note", "--target",
                            f"file:{f}", "--tag", "gotcha", "--body", f"{f} trap{more}"],
                           cwd=git_repo, capture_output=True, text=True, env=env)
        assert r.returncode == 0, r
        tails[f] = r.stdout.strip()[-10:]
    (git_repo / "serve.py").write_text("v2\n")
    commit("serve v2")
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env,
                       input=json.dumps({"session_id": "s1", "hook_event_name": "SessionStart"}))
    assert r.returncode == 0, r

    def line(f):
        return _touch(env, {"session_id": "s1", "tool_name": "Read",
                            "tool_input": {"file_path": f"{git_repo}/{f}"}})

    said = line("serve.py")
    assert "is on file:serve.py (written just now; the file has changed in 1 commit since): " \
           "serve.py trap" in said, said
    assert said.endswith(f" (if it no longer holds: `symbion supersede {tails['serve.py']} "
                         "--add-tag retired`)"), "a gotcha that fits has no more to show"
    said = line("other.py")
    assert "is on file:other.py (written just now): other.py trap" in said, said
    assert f"(`symbion show {tails['other.py']}` prints all of it; if it no longer holds: " \
           f"`symbion supersede {tails['other.py']} --add-tag retired`)" in said, said
