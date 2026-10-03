"""`symbion serve` as a process: what the `user` fixture never runs, since it
builds the pages in-process and skips serve.main() and uvicorn entirely."""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nicegui")

from symbion import store     # noqa: E402

# The console script beside this venv's python (test_hook.py says why not
# shutil.which): its `__main__` guard is half of what broke --reload.
SYMBION = str(Path(sys.executable).parent / "symbion")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_page(p, port):
    """(status, page) of the serve's `/` once it answers, or (None, None) if
    it exits or 30 s pass first."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and p.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                return r.status, r.read().decode()
        except OSError:
            time.sleep(0.3)
    return None, None


def test_reload_serves_a_page_from_the_console_script(tmp_path):
    """--reload died at startup with "You must call ui.run()" from its first
    commit: uvicorn's spawned worker re-runs only the parent's __main__, and
    the script's guard skips main() there. Then the re-exec that fixed it ran
    in the worker too, which started a second server on the same port."""
    store.ensure_store(tmp_path)
    port = _free_port()
    # NiceGUI reads PYTEST_CURRENT_TEST as "under test" and turns reload off.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")      # where serve records itself
    records = tmp_path / "cache" / "symbion" / "serve"
    p = subprocess.Popen(
        [SYMBION, "--dir", str(tmp_path), "serve", "--reload", "--no-browser",
         "--port", str(port), "--author", "t"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        start_new_session=True)
    status = page = None
    try:
        status, page = _get_page(p, port)
        # one record, the parent's: the reload worker serves, but never records
        running = [json.loads(f.read_text()) for f in records.glob("*.json")]
    finally:
        os.killpg(p.pid, signal.SIGTERM)     # the reloader and its worker
        try:
            out = p.communicate(timeout=10)[0].decode()
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out = p.communicate()[0].decode()
    assert status == 200, out
    assert out.count("symbion →") == 1, out   # one server announced, not two
    assert [(r["pid"], r["url"], r["store"]) for r in running] == \
        [(p.pid, f"http://127.0.0.1:{port}", str(tmp_path.resolve()))], running
    assert not list(records.glob("*.json")), "a serve that stopped leaves its record"
    # ui.run's favicon, which only a served page carries: NiceGUI's own icon
    # is a static .ico, ours an inline SVG.
    assert 'href="data:image/svg+xml' in page and "favicon.ico" not in page, page[:2000]
    # What tells Dark Reader the page is already dark: ui.run(dark=True) writes
    # the color-scheme meta its detector reads first; shell() adds its lock.
    assert re.search(r'name="color-scheme"\s+content="dark"', page), page[:2000]
    assert '<meta name="darkreader-lock">' in page, page[:2000]
    # ...and takes off the fallback style Dark Reader adds before it reads the lock
    assert 'querySelectorAll(".darkreader--fallback").forEach(e => e.remove())' in page, page[:2000]


def test_ctrl_c_stops_a_serve_quietly(tmp_path):
    """Ctrl-C is how a person stops a serve. uvicorn shuts down cleanly, then
    re-raises the SIGINT it caught, and the KeyboardInterrupt came out of
    ui.run() as a 48-line traceback and exit 1. --reload's supervisor already
    exits 0 on it."""
    store.ensure_store(tmp_path)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    p = subprocess.Popen(
        [SYMBION, "--dir", str(tmp_path), "serve", "--no-browser",
         "--port", str(port), "--author", "t"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        start_new_session=True)
    try:
        status, _ = _get_page(p, port)
        p.send_signal(signal.SIGINT)
        out = p.communicate(timeout=15)[0].decode()
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
    assert status == 200, out
    assert "Traceback" not in out and p.returncode == 0, (p.returncode, out)
    assert not list((tmp_path / "cache" / "symbion" / "serve").glob("*.json"))


def test_a_second_serve_on_a_store_takes_its_links_when_the_first_stops(tmp_path, monkeypatch,
                                                                         capsys):
    """Two serves on one store left two records, and the other stores'
    sidebars linked either. The first stays the one they link (the owner,
    2026-10-01). The second says so and records too: when the first stops,
    even killed outright, the links go to the second (the owner, 2026-10-01:
    the second recorded nothing, so it was never linked)."""
    from symbion import api
    from symbion.gui import serve, servers
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(serve, "build_page", lambda ctx, author: None)
    store.ensure_store(tmp_path / "s")
    ctx = api.resolve(str(tmp_path / "s"))
    urls = lambda: [r["url"] for r in servers.running()]          # noqa: E731
    seen = []
    monkeypatch.setattr(serve.ui, "run", lambda **kw: seen.append(urls()))

    serve.main(ctx, author="t", port=1111, show=False)            # the first
    assert seen.pop() == ["http://127.0.0.1:1111"]

    first = tmp_path / "cache" / "symbion" / "serve" / "first.json"
    dead = subprocess.Popen(["true"])
    dead.wait()

    def _first(pid, started=1.0):
        first.write_text(json.dumps({"pid": pid, "url": "http://127.0.0.1:1111",
                                     "store": str((tmp_path / "s").resolve()),
                                     "started": started}))
    _first(os.getpid())                                           # alive, and earlier

    def run(**kw):
        seen.append(urls())
        _first(os.getpid(), started=9e9)  # the start decides, not directory order
        seen.append(urls())
        _first(dead.pid)                                          # the first is killed
        seen.append(urls())
    monkeypatch.setattr(serve.ui, "run", run)
    serve.main(ctx, author="t", port=2222, show=False)            # a second, the first alive
    assert seen == [["http://127.0.0.1:1111"], ["http://127.0.0.1:2222"],
                    ["http://127.0.0.1:2222"]]
    assert "another symbion serve runs on this store: http://127.0.0.1:1111" in \
        capsys.readouterr().err
    assert not list(first.parent.glob("*.json")), "the second leaves its record"


def test_a_stores_port_is_its_names_and_the_same_on_every_machine(tmp_path):
    """The owner, 2026-10-02: a row link must not go stale when its serve
    restarts. Each store takes a port derived from its name, so a restart,
    another session, or a clone on another machine gets the same one. The
    values are pinned: a change to the formula moves every link."""
    from symbion.gui import servers
    assert servers.name(tmp_path / "widget-notes") == "widget"
    assert servers.name(tmp_path / "widget") == "widget"
    assert servers.port(tmp_path / "widget-notes") == 26080
    assert servers.port(tmp_path / "a" / "widget-notes") == 26080   # where it sits does not count
    assert servers.port(tmp_path / "gadget-notes") == 21642
    assert all(20000 <= servers.port(tmp_path / f"s{i}") < 30000 for i in range(200))


def _listener(port: int = 0) -> socket.socket:
    """A socket bound as uvicorn binds one: 127.0.0.1, SO_REUSEADDR."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen()
    return s


def test_a_restarted_serve_gets_its_port_back_through_time_wait():
    """The test bind ran without SO_REUSEADDR, and the connections a serve
    had closed, still in TIME_WAIT, refused it: a serve restarted after it
    answered a request moved to a random port (measured 2026-10-02, on
    macOS and Linux)."""
    from symbion.gui.serve import pick_free_port
    lst = _listener()
    port = lst.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = lst.accept()
    conn.close()                    # the server closes first: TIME_WAIT is on its side
    assert cli.recv(1) == b""
    cli.close()
    lst.close()
    assert pick_free_port(port) == port


def test_a_port_a_serve_holds_reads_as_taken():
    """With SO_REUSEADDR, an any-address bind succeeds on macOS beside a
    listener on 127.0.0.1, so it would read a running serve's port as free.
    The test bind uses the address uvicorn binds."""
    from symbion.gui.serve import pick_free_port
    with _listener() as held:
        port = held.getsockname()[1]
        assert pick_free_port(port) != port


def _main_port(tmp_path, monkeypatch, capsys, store_port, **kw):
    """serve.main up to ui.run, on a store whose own port is `store_port`:
    the port it would serve on, and what it printed on stderr."""
    from symbion import api
    from symbion.gui import serve, servers
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(serve, "build_page", lambda ctx, author: None)
    monkeypatch.setattr(servers, "port", lambda store: store_port)
    seen = {}
    monkeypatch.setattr(serve.ui, "run", lambda **k: seen.update(k))
    store.ensure_store(tmp_path / "s-notes")
    serve.main(api.resolve(str(tmp_path / "s-notes")), author="t", show=False, **kw)
    return seen["port"], capsys.readouterr().err


def test_a_serve_takes_its_stores_port(tmp_path, monkeypatch, capsys):
    want = _free_port()
    port, err = _main_port(tmp_path, monkeypatch, capsys, want)
    assert port == want
    assert "this store's port" not in err


def test_an_explicit_port_wins_without_a_warning(tmp_path, monkeypatch, capsys):
    with _listener() as held:
        port, err = _main_port(tmp_path, monkeypatch, capsys, held.getsockname()[1],
                               port=1234)
    assert port == 1234
    assert "this store's port" not in err


def test_a_taken_port_names_its_holder(tmp_path, monkeypatch, capsys):
    """The owner, 2026-10-02: collisions can be detected. Two names can give
    one port; the later serve takes a free port and says whose serve holds
    its own, or that no symbion serve does. Another serve on this store
    already has its own warning, and gets no second one."""
    with _listener() as held:
        want = held.getsockname()[1]
        port, err = _main_port(tmp_path, monkeypatch, capsys, want)
        assert port != want
        assert (f"warning: this store's port, {want}, is held by a process that is "
                f"not a symbion serve. This serve takes {port}, which changes at each "
                "restart; --port picks a fixed one.") in err

        records = tmp_path / "cache" / "symbion" / "serve"
        records.mkdir(parents=True, exist_ok=True)
        other = (tmp_path / "other-notes").resolve()
        (records / "other.json").write_text(json.dumps(
            {"pid": os.getpid(), "url": f"http://127.0.0.1:{want}",
             "store": str(other), "started": 1.0}))
        port, err = _main_port(tmp_path, monkeypatch, capsys, want)
        assert (f"is held by the serve on {other}, a store whose name gives the "
                "same port") in err

        mine = (tmp_path / "s-notes").resolve()
        (records / "other.json").write_text(json.dumps(
            {"pid": os.getpid(), "url": f"http://127.0.0.1:{want}",
             "store": str(mine), "started": 1.0}))
        port, err = _main_port(tmp_path, monkeypatch, capsys, want)
        assert "another symbion serve runs on this store" in err
        assert "this store's port" not in err


def test_a_port_held_on_every_address_reads_as_taken():
    """On macOS, a 127.0.0.1 bind with SO_REUSEADDR succeeds beside a
    listener on every address (0.0.0.0), and uvicorn's bind would then take
    that service's loopback traffic, with no warning (found in review,
    2026-10-02). The test bind also tries every address."""
    from symbion.gui.serve import pick_free_port
    with socket.socket() as held:
        held.bind(("", 0))
        held.listen()
        port = held.getsockname()[1]
        assert pick_free_port(port) != port
