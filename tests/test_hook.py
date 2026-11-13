import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import time
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


def test_the_plugin_shim_runs_the_real_cli_package_hook(git_repo, tmp_path):
    """The plugin's shim against the installed CLI, not a fake. Python on
    Windows writes `skill --path` to the pipe with CRLF. Git's sh drops the CR
    in `$(…)`; dash, which runs this test under Git Bash, keeps it, and
    without the shim's strip read a working CLI as missing (2026-11-10)."""
    store_dir = tmp_path / "store"
    store.ensure_store(store_dir)
    shim = HOOK.parents[4] / "plugin" / "hooks" / "session_start.sh"
    r = subprocess.run([SH, str(shim)], cwd=git_repo, input="", capture_output=True, text=True,
                       env={**SRC_ENV, "PATH": TOOLS, "SYMBION_DIR": str(store_dir),
                            "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")})
    assert (r.returncode, r.stderr) == (0, "")
    assert "bug 0" in _context(r.stdout)
    cached = (tmp_path / "c" / "symbion" / "skill_dir").read_text(encoding="utf-8")
    assert Path(cached.rstrip("\n")) == HOOK.parent


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
                       else json.dumps(payload), capture_output=True, text=True,
                       encoding="utf-8", env=env)   # Windows' default is cp1252
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


def _shown(*rows, sym="symbion"):
    """gotchas.sh's line at an edit for rows the reader already got in full."""
    tails = " ".join(r.id[-10:] for r in rows)
    w, it = ("gotcha {} was", "it") if len(rows) == 1 else ("gotchas {} were", "them")
    return (f"symbion: this file's {w.format(tails)} shown in full earlier in this session "
            f"(`{sym} show {tails}` prints {it} again).")


def test_an_edit_reprints_in_full_only_what_this_reader_never_got(git_repo, tmp_path):
    """At a file's first edit, each row this reader got before comes back as
    one line of ids, and a row it never got comes in full (the user, 2026-11-09).
    The line's command reaches a named store too."""
    s = tmp_path / "named-notes"
    store.ensure_store(s)
    x, y = ({"type": "file", "name": n} for n in ("src/x.py", "src/y.py"))
    a = store.add(s, kind="note", target=x, author="ann", tags=["gotcha"], body="trap a")
    b = store.add(s, kind="note", target=x, author="ann", tags=["gotcha"], body="trap b")
    c = store.add(s, kind="note", target=y, refs=[x], author="ann", tags=["gotcha"], body="trap c")
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c"),
           "SYMBION_DIR": str(s)}
    for sid in ("s1", "s2"):
        r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env,
                           input=json.dumps({"session_id": sid, "hook_event_name": "SessionStart"}))
        assert r.returncode == 0, r

    def call(sid, tool, path):
        return _touch(env, {"session_id": sid, "tool_name": tool,
                            "tool_input": {"file_path": f"{git_repo}/{path}"}})
    assert "trap c" in call("s1", "Read", "src/y.py")
    head, first, second, third = call("s1", "Edit", "src/x.py").split("\n")
    sym = re.search(r"`(symbion --dir \S+) show", head)[1]
    assert "trap a" in first and "trap b" in second and third == _shown(c, sym=sym)
    assert call("s1", "Edit", "src/x.py") == ""
    said = call("s2", "Read", "src/x.py")
    assert all(t in said for t in ("trap a", "trap b", "trap c")), said
    assert call("s2", "Edit", "src/x.py") == _shown(a, b, c, sym=sym)
    exe = shutil.which("symbion", path=SYMBION_BIN)            # Windows: symbion.exe
    shown = subprocess.run([exe, *shlex.split(sym)[1:], "show", a.id[-10:], b.id[-10:]], cwd=git_repo,
                           capture_output=True, text=True, env={**env, "SYMBION_DIR": ""})
    assert "trap a" in shown.stdout and "trap b" in shown.stdout, shown


def test_a_gotcha_fires_on_its_own_file_and_again_at_its_first_edit(git_repo, tmp_path):
    """A row on README.md fired on a Read of src/README.md and was then spent
    for the session (a clean-room review, 2026-10-13), and a Bash call that
    only named a file spent its edit-time trap the same way (dogfood,
    2026-10-14). A name matches at a path boundary, from the repo's root; an
    edit matches on the file it edits, not the text it writes, and gets the
    trap once more: by its id when this reader got it in full before, since
    the answer lands after the edit (the user, 2026-11-09)."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    row = store.add(s, kind="note", target={"type": "file", "name": "README.md"}, author="ann",
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
    assert call("s1", "Edit", file_path=f"{git_repo}/README.md",
                old_string="a", new_string="b") == _shown(row), "again at the first edit, by id"
    assert call("s1", "Write", file_path=f"{git_repo}/README.md", content="b") == ""
    assert call("s1", "Read", file_path=f"{git_repo}/README.md") == ""
    # A subagent got nothing from the main agent's read: in full.
    assert "readme trap" in _touch(env, {"session_id": "s1", "agent_id": "a1", "tool_name": "Edit",
                                         "tool_input": {"file_path": f"{git_repo}/README.md"}})
    assert "readme trap" in call("s2", "Bash", command="cat ./README.md")


def test_a_gotcha_fires_on_its_file_in_a_linked_worktree(git_repo, tmp_path):
    """Read and Edit name a file by absolute path, and in a linked worktree the
    dir above the row's name is the worktree's, not the root's: every such call
    got nothing (dogfood, 2026-11-04). The worktree's `.git` file names the
    root's git dir. A worktree made after session start counts, as does one
    under a dir with a blank; another repo's worktree and a plain dir do not."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    row = store.add(s, kind="note", target={"type": "file", "name": "src/serve.py"},
                    author="ann", tags=["gotcha"], body="serve trap")
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    for sid in ("s1", "s2", "s3", "s4"):
        r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                           env=env, input=json.dumps({"session_id": sid,
                                                      "hook_event_name": "SessionStart"}))
        assert r.returncode == 0, r
    inside, spaced = git_repo / ".worktrees" / "feature", tmp_path / "my work" / "feature"
    for wt in (inside, spaced):
        _git(git_repo, "worktree", "add", "-q", "--detach", str(wt))
    other = tmp_path / "other" / "feature"
    other.mkdir(parents=True)
    (other / ".git").write_text(f"gitdir: {tmp_path}/elsewhere/.git/worktrees/feature\n")

    def call(sid, tool, **tool_input):
        return _touch(env, {"session_id": sid, "tool_name": tool, "tool_input": tool_input})

    for d in (other, tmp_path / "plain"):
        assert call("s1", "Read", file_path=f"{d}/src/serve.py") == "", d
    assert "serve trap" in call("s1", "Read", file_path=f"{inside}/src/serve.py")
    assert call("s1", "Edit", file_path=f"{inside}/src/serve.py",
                old_string="a", new_string="b") == _shown(row), "again at the first edit"
    assert "serve trap" in call("s2", "Edit", file_path=f"{spaced}/src/serve.py",
                                old_string="a", new_string="b")
    assert "serve trap" in call("s3", "Read", file_path=f"{spaced}/src/serve.py")
    assert "serve trap" in call("s4", "Bash", command=f"sed -n 1p {inside}/src/serve.py")


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

    head, said = line("serve.py").split("\n")
    # One header a call says the shared words in, then a short line per row:
    # a full line was 2/3 wrapper, repeated in every row of a burst (2026-11-12).
    assert head == ("symbion gotchas on what this call names (id, target, age, commits to "
                    "the file since; `symbion show ID` prints one whole, `symbion supersede ID "
                    "--add-tag retired` retires one that no longer holds):"), head
    # A stamp, not `just now`: the line is cached for the session, and an age froze.
    assert re.fullmatch(rf"- {tails['serve.py']} on file:serve\.py \(\d{{4}}-\d\d-\d\d "
                        r"\d\d:\d\d:\d\dZ, 1 commit since\): serve\.py trap", said), said
    head, said = line("other.py").split("\n")
    assert re.fullmatch(rf"- {tails['other.py']} on file:other\.py \([-\d]+ [:\d]+Z\): "
                        r"other\.py trap( detail)+…", said), "a clipped row ends in …"


def test_a_gotcha_line_fired_by_a_ref_names_the_ref_and_counts_its_file(git_repo, tmp_path):
    """A call that named a row's ref got "is on <target>" and the target's
    churn, so the line read as misfiled (dogfood, 2026-10-30). A ref keeps no
    blob, so its count starts at the row's first write."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    (s / "symbion.toml").write_text('[catalogs]\nfile = "git ls-files"\n')
    for f in ("serve.py", "other.py"):
        (git_repo / f).write_text("v1\n")
    past = {**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    _git(git_repo, "add", "-A")
    subprocess.run(["git", "-C", str(git_repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "files"], check=True, capture_output=True, env=past)
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    r = subprocess.run([str(Path(SYMBION_BIN) / "symbion"), "add", "note", "--target",
                        "file:serve.py", "--ref", "file:other.py", "--tag", "gotcha",
                        "--body", "shared trap"], cwd=git_repo, capture_output=True, text=True,
                       env={**env, "SYMBION_NOW": "2026-01-10T00:00:00+00:00"})
    assert r.returncode == 0, r
    for v in ("v2\n", "v3\n"):
        (git_repo / "other.py").write_text(v)
        _git(git_repo, "add", "-A")
        _git(git_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", v)
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env,
                       input=json.dumps({"session_id": "s1", "hook_event_name": "SessionStart"}))
    assert r.returncode == 0, r

    def line(f, sid):
        return _touch(env, {"session_id": sid, "tool_name": "Read",
                            "tool_input": {"file_path": f"{git_repo}/{f}"}})

    said = line("other.py", "s1")
    assert re.search(r"on file:serve\.py, refers to file:other\.py \([^,)]+, 2 commits to "
                     r"other\.py since\): shared trap", said), said
    r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True, env=env,
                       input=json.dumps({"session_id": "s2", "hook_event_name": "SessionStart"}))
    assert r.returncode == 0, r
    said = line("serve.py", "s2")
    assert re.search(r"on file:serve\.py \([^,)]+\): shared trap", said), \
        "the target's own line: no ref named, and serve.py has not changed"


def test_a_gotcha_fires_on_a_path_never_on_a_name(git_repo, tmp_path):
    """A gotcha reached through a name that is no path (a service, a host) fired on
    any call that named it, most often a call that had nothing to do with its
    trap (2026-11-06). The hook matches paths only: a file, a repo's file, or a
    catalog name with a `/`."""
    toml = ('[catalogs]\nfile = "git ls-files"\nservice = "printf \'lynx\\\\nheron\\\\n\'"\n'
            'server = "printf \'/etc/edge.conf\\\\nedge\\\\n\'"\n')
    bash = _gotcha_session(git_repo, tmp_path, toml, [
        ({"type": "service", "name": "lynx"}, [], "lynx trap"),
        ({"type": "file", "name": "scan.py"}, [{"type": "service", "name": "lynx"}], "scan trap"),
        ({"type": "server", "name": "edge"}, [], "edge trap"),
        ({"type": "server", "name": "/etc/edge.conf"}, [], "conf trap")])
    for cmd in ("deploy lynx -- python3 record.py", "ssh edge uptime", "python3 plot.py --label lynx"):
        assert bash(cmd) == "", cmd
    said = bash("sed -n 1p scan.py")
    assert re.search(r"on file:scan\.py \([^)]+\): scan trap", said), said
    assert "conf trap" in bash("cat /etc/edge.conf")


def _gotcha_session(git_repo, tmp_path, toml, rows):
    """A store holding `rows` (target, refs, body), every one a #gotcha, and a
    Bash call into the gotcha hook; each session id starts its session first."""
    s = tmp_path / "proj-notes"
    store.ensure_store(s)
    if toml:
        (s / "symbion.toml").write_text(toml)
    for target, refs, body in rows:
        store.add(s, kind="note", target=target, author="ann", tags=["gotcha"], refs=refs,
                  body=body)
    env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")}
    started = set()

    def bash(cmd, sid="s1"):
        if sid not in started:
            r = subprocess.run([SH, str(HOOK)], cwd=git_repo, capture_output=True, text=True,
                               env=env, input=json.dumps({"session_id": sid,
                                                          "hook_event_name": "SessionStart"}))
            assert r.returncode == 0, r
            started.add(sid)
        return _touch(env, {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": cmd}})
    return bash


def test_a_symbion_command_that_names_a_file_neither_fires_nor_spends_its_gotcha(git_repo,
                                                                                tmp_path):
    """A write about a file, or a read of its entries, is no use of it, and
    `context` prints the gotcha anyway. A symbion command that only named the
    object spent its line before the first real use (2026-11-05)."""
    bash = _gotcha_session(git_repo, tmp_path, None,
                           [({"type": "file", "name": "src/serve.py"}, [], "serve trap")])
    for cmd in ("symbion add note --target file:src/serve.py --body x",
                ".venv/bin/symbion context --target file:src/serve.py 2>&1 | head -5",
                "cd /work && symbion add task --target project --body-file - <<'EOF' 2>&1\n"
                "see src/serve.py\nEOF",
                'symbion add note --body-file - <<"END"\nsrc/serve.py\nEND',
                'S="symbion --dir /n"; $S add note --body-file - <<\'EOF\'\nsee src/serve.py\nEOF',
                "S=.venv/bin/symbion\n${S} context --target file:src/serve.py"):
        assert bash(cmd) == "", cmd
    assert "serve trap" in bash("sed -n 1,5p src/serve.py; symbion add note --target "
                                "file:src/serve.py --body y"), "unspent, and the sed uses it"
    for i, cmd in enumerate(("cd /x/symbion && sed -n 1p src/serve.py",
                             "symbion add note --body-file - <<'EOF'\nsrc/serve.py\nEOF\n"
                             "cat src/serve.py",
                             "python3 - <<'EOF'\nopen('src/serve.py')\nEOF",
                             f"cat /x/symbion-notes/{git_repo.name}/src/serve.py",
                             "S='symbion --dir /n'; P=python3; $P - <<'EOF'\nopen('src/serve.py')\nEOF",
                             "git log --grep=symbion src/serve.py",
                             "cd /work\nsrc/serve.py --help")):
        assert "serve trap" in bash(cmd, sid=f"s{i + 2}"), cmd


# ---- one case table, both gotcha hooks ----
CODEX_GOTCHAS = HOOK.with_name("codex_gotchas.py")
BOTH = ["gotchas.sh", "codex"]


def _both_hooks(tmp_path, rows, head=None):
    """A call into gotchas.sh or the Codex hook, each against `rows` (id, name,
    text) written as `_list_gotchas` writes the list, LF on every platform:
    Windows text mode wrote CRLF, and the root, the last field, read as a
    `\\r` no dir matches (2026-11-09). `head`, the header file beside it.
    Each call is a fresh session: a fire spends its row."""
    d = tmp_path / "c" / "symbion" / "gotchas"
    d.mkdir(parents=True)
    n = iter(range(1000))

    def run(hook, cmd, *, sh=SH, tool="Bash", args=None, env=()):
        sid = f"s{next(n)}"
        (d / sid).write_text("".join(f"{i}\t{name}\t{text}\t{''.join(root)}\n"
                                     for i, name, text, *root in rows),
                             encoding="utf-8", newline="\n")
        if head is not None:
            (d / f"{sid}.head").write_text(head, encoding="utf-8", newline="\n")
        event = {"session_id": sid, "tool_name": tool, "tool_input": args or {"command": cmd}}
        argv = [sh, str(GOTCHAS)] if hook == "gotchas.sh" else [sys.executable, str(CODEX_GOTCHAS)]
        env = {**SRC_ENV, "PATH": TOOLS, "HOME": str(tmp_path),
               "XDG_CACHE_HOME": str(tmp_path / "c"), **dict(env)}
        r = subprocess.run(argv, input=json.dumps(event), capture_output=True, text=True,
                           encoding="utf-8", env=env)
        assert r.returncode == 0 and r.stderr == "", r
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else ""
    return run


@pytest.mark.parametrize("hook", BOTH)
@pytest.mark.parametrize("cmd, fires", [
    ("cat src/x.py", True),
    ("symbion add note --target file:src/x.py --body x", False),
    ('grep -n "symbion show" src/x.py', True),
    ('symbion add note --body x <<< "text" && cat src/x.py', True),
    # A separator or newline inside quotes is part of the argument.
    ("symbion add note --body 'first; the fix is in src/x.py'", False),
    ('symbion add note --body "a | b & c; see src/x.py"', False),
    ('symbion add note --body "line one\nline two; src/x.py"', False),
    ('symbion add note --body "say \\"a; b\\" src/x.py"', False),
    ("symbion add note --body 'it'\\''s; src/x.py'", False),
    ("symbion add note --body a\\; src/x.py", False),
    ("symbion add note --body x \\\n  --ref file:src/x.py", False),
    ("symbion add note --body x 2>&1 --ref file:src/x.py", False),
    ("symbion add note --body 'a; b'; cat src/x.py", True),
    ("symbion add note --body \"a\nb\" && sed -n 1p src/x.py", True),
    # Inside a quoted string, `symbion` is another shell's command, which the
    # quote ends; a comment's apostrophe opens no quote. Neither may swallow
    # the call after it.
    ('sym=".venv/bin/symbion --dir $B" && cat src/x.py', True),
    ("symbion list --tag x  # what's open\ncat src/x.py", True),
    ('out="$(symbion list)"; cat src/x.py', True),
    ("ssh h 'cd w; symbion add note --body \"a; src/x.py\"; ls'", False),
    ('echo "n: $(symbion list --target file:src/x.py | wc -l)"', False),
    ('echo "n: $(symbion list --grep "$p" --target file:src/x.py)"', False),
    # A heredoc's body holds no shell quotes: its apostrophe opens none.
    ("cat > f <<'EOF'\nthe centre's miss\nEOF\nsymbion add note --target 'item:x' --ref file:src/x.py", False),
    # A `)` ends a command, unless a `$(` in it opened one.
    ("wc -l $(symbion list --target file:a.py) src/x.py", True),
    ("symbion add note --body $(cat f) --ref file:src/x.py", False),
    # longer than the 16 KB gotchas.sh reads whole: the body still goes with its command
    ("symbion add note --body-file - <<'EOF'\nsee src/x.py\n" + "z\n" * 10000 + "EOF", False)])
def test_both_gotcha_hooks_read_a_call_alike(tmp_path, hook, cmd, fires):
    """Each hook cuts symbion commands its own way, and a case one got right
    the other could miss (audit, 2026-11-08). A quoted `symbion ` is an
    argument, and a `<<<` has no delimiter line: the call after it counts. A
    separator inside quotes, or escaped, is part of its argument."""
    run = _both_hooks(tmp_path, [("id1", "src/x.py", "x trap")])
    assert run(hook, cmd) == ("x trap" if fires else "")


@pytest.mark.parametrize("hook", BOTH)
def test_both_gotcha_hooks_print_the_header_once_before_the_rows(tmp_path, hook):
    """The words every row of a burst shared go in one header (2026-11-12),
    which each hook prints before the call's first row, and only then."""
    run = _both_hooks(tmp_path, [("id1", "src/x.py", "- x trap"), ("id2", "src/x.py", "- y trap"),
                                 ("id3", "src/z.py", "- z trap")],
                      head="gotchas (`symbion supersede ID --add-tag retired`):")
    assert run(hook, "cat src/x.py") == \
        "gotchas (`symbion supersede ID --add-tag retired`):\n- x trap\n- y trap"
    assert run(hook, "ls") == ""


@pytest.mark.parametrize("hook", BOTH)
def test_both_gotcha_hooks_fire_in_a_linked_worktree(tmp_path, hook):
    """gotchas.sh fired on a file in a linked worktree named by absolute path
    (dogfood, 2026-11-04), and the Codex hook did not (audit, 2026-11-08). The
    worktree's `.git` file names `<root>/.git/worktrees/`; another repo's
    worktree and a plain dir do not count."""
    run = _both_hooks(tmp_path, [("id1", "src/x.py", "x trap", "proj")])
    for d, repo in (("wt/feature", "proj"), ("my work/feature", "proj"), ("wt/stray", "other")):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / ".git").write_text(f"gitdir: {tmp_path}/{repo}/.git/worktrees/x\n")
    assert run(hook, f"sed -n 1p {tmp_path}/wt/feature/src/x.py") == "x trap"
    assert run(hook, None, tool="Edit", args={
        "file_path": f"{tmp_path}/my work/feature/src/x.py"}) == "x trap"
    # single quotes, and a line's start: gotchas.sh missed both (audit, 2026-11-12)
    assert run(hook, f"cat '{tmp_path}/my work/feature/src/x.py'") == "x trap"
    assert run(hook, f"cd /tmp\n{tmp_path}/wt/feature/src/x.py --help") == "x trap"
    for d in ("wt/stray", "plain"):
        assert run(hook, f"cat {tmp_path}/{d}/src/x.py") == "", d


@pytest.mark.parametrize("hook", BOTH)
def test_a_gotcha_holding_a_line_separator_silences_no_hook(tmp_path, hook):
    """The Codex hook split its list with splitlines(), which splits at U+2028
    too: one such row silenced every gotcha, with no error (audit, 2026-11-08)."""
    run = _both_hooks(tmp_path, [("id1", "src/a.py", "a\u2028trap"), ("id2", "src/x.py", "x trap")])
    assert run(hook, "cat src/a.py src/x.py") == "a\u2028trap\nx trap"


@pytest.mark.parametrize("hook", BOTH)
def test_a_long_run_with_no_blank_stays_inside_the_hook_timeout(tmp_path, hook):
    """The Codex hook tried a path prefix at each position of a call, for each
    row: 300 rows on an 8 KB run of `1,2,3` took 82 s, and the timeout is 5 s
    (audit, 2026-11-08)."""
    run = _both_hooks(tmp_path, [(f"id{i}", f"src/m{i}.py", f"trap {i}") for i in range(300)])
    start = time.monotonic()
    assert run(hook, "python3 -c 'print([" + ",".join(map(str, range(2000))) + "])'") == ""
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("hook", BOTH)
def test_a_long_run_beside_a_rows_name_stays_inside_the_hook_timeout(tmp_path, hook):
    """A call that lacks a row's name skips its patterns. One that holds it
    tried each path pattern at every start of a run with no blank: 16 KB took
    2.4 s in the Codex hook, 8 s under load, and the timeout is 5 s (audit,
    2026-11-12). Each place the name occurs is read from its own word."""
    run = _both_hooks(tmp_path, [("id1", "README.md", "x trap", "proj")])
    start = time.monotonic()
    assert run(hook, "cat docs/README.md; echo " + "a," * 16000) == ""
    assert time.monotonic() - start < 5
    assert run(hook, "cat /w/proj/README.md; echo " + "a," * 16000) == "x trap"


@pytest.mark.parametrize("tool", ["Write", "Bash", "many", "lines"])
def test_a_large_call_stays_inside_the_hook_timeout(tmp_path, tool):
    """In bash, which is macOS's sh, under a UTF-8 locale, each cut and match
    over the whole call is quadratic: a 280 KB Write took 21 s (audit,
    2026-11-08), a 280 KB Bash heredoc 78 s (2026-11-09), and the timeout is
    5 s. An edit is read by its path alone, and a long call by its ends. Each
    symbion command costs one awk, about 4 ms, so a call holding 60 stays well
    inside; 8 KB of lines before one took 49 s in a cut of sh's own (2026-11-09)."""
    run = _both_hooks(tmp_path, [("id1", "src/x.py", "x trap")])
    body = "y = 1\n" * 40000
    args = {"file_path": "/w/src/x.py", "content": body} if tool == "Write" else \
        {"command": f"cat > /w/out.txt <<'EOF'\n{body}EOF\ncat src/x.py"} if tool == "Bash" else \
        {"command": "\n".join(f'symbion add note --body "step {i}; see src/m{i}.py"'
                               for i in range(60)) + "\ncat src/x.py"} if tool == "many" else \
        {"command": "".join(f"line {i}: it is the centre miss\n" for i in range(250))
                    + "symbion add note --body x\ncat src/x.py"}
    start = time.monotonic()
    said = run("gotchas.sh", None, sh="/bin/sh" if Path("/bin/sh").exists() else SH,
               tool="Write" if tool == "Write" else "Bash", args=args, env={"LC_ALL": "en_US.UTF-8"})
    assert said == "x trap" and time.monotonic() - start < 5


@pytest.mark.parametrize("field", ["name", "body"])
def test_a_row_with_a_number_for_a_string_costs_the_hint_not_the_summary(tmp_path, monkeypatch,
                                                                          capsys, field):
    """Another tool's row with a number for its target's name, or for its body,
    raised in the gotcha list, and the session started on a traceback, not
    its summary (audit, 2026-11-08)."""
    import io
    from symbion import cli
    s = tmp_path / "s-notes"
    store.ensure_store(s)
    store.add(s, kind="note", target={"type": "file", "name": "src/x.py"}, author="ann",
              tags=["gotcha"], body="a trap")
    path = store.notes_path(s)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["id"] = "20260101-000000-000000-bad"
    if field == "name":
        row["target"]["name"] = 12345
    else:
        row["body"] = 5
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "s1"}')))
    assert cli.main(["--dir", str(s), "summary", "--hook"]) == 0
    out = capsys.readouterr().out
    assert "hookSpecificOutput" in out and "no gotcha hints" not in out
    # The load refuses the bad row, so the good row's hint still reaches the list.
    listed = (cli._session_cache() / "s1").read_text(encoding="utf-8")
    assert "a trap" in listed and "-bad\t" not in listed
    ((_, _, why),) = store.load_malformed(s)
    assert why == ("a target's name must be a string, not int 12345" if field == "name"
                   else "body must be a string, not int 5")


def test_a_bug_in_the_gotcha_list_costs_the_hints_and_says_so(tmp_path, monkeypatch, capsys):
    """A bare `except Exception: pass` kept the summary and hid the bug: the
    session had no hints and no word of why (audit, 2026-11-08)."""
    import io
    from symbion import cli
    s = tmp_path / "s-notes"
    store.ensure_store(s)
    store.add(s, kind="task", target={"type": "project", "name": None}, body="canary task")

    def boom(*a):
        raise KeyError("root")
    monkeypatch.setattr(cli, "_list_gotchas", boom)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "s1"}')))
    assert cli.main(["--dir", str(s), "summary", "--hook"]) == 0
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "canary task" in text
    assert text.endswith("symbion: no gotcha hints this session, from a bug in symbion: "
                         "KeyError: 'root'")
