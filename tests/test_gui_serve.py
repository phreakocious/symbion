"""`symbion serve` as a process: what the `user` fixture never runs, since it
builds the pages in-process and skips serve.main() and uvicorn entirely."""
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nicegui")

from symbion import store     # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_middleware(monkeypatch):
    """serve.main adds middleware to nicegui's global app: a real add reaches
    every later GUI test, and raises once an earlier test has started the app.
    _main_host records what it would add instead."""
    from symbion.gui import serve
    monkeypatch.setattr(serve.app, "add_middleware", lambda *a, **k: None)

# The console script beside this venv's python (test_hook.py says why not
# shutil.which): its `__main__` guard is half of what broke --reload.
SYMBION = str(Path(sysconfig.get_path("scripts")) / "symbion")
# Windows signals no process group: the child leads its own console group,
# and Ctrl+Break, which serve takes as Ctrl-C, is what reaches it there.
GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
         else {"start_new_session": True})
STOP = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM
CTRL_C = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT


def _kill(p):
    """The serve and whatever it started."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        os.killpg(p.pid, signal.SIGKILL)


def _free_port() -> int:
    """A port nothing holds on any address, below both OSes' ephemeral
    ranges, as a store's own port is. One from bind(127.0.0.1, 0) can be held
    by another user's connection on another address: the 127.0.0.1 bind
    passes it, and serve's any-address test bind refuses it (a CI macOS
    runner, 2026-10-03)."""
    for port in random.sample(range(20000, 30000), 500):
        with socket.socket() as s:
            try:
                s.bind(("", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 20000-29999")


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
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **GROUP)
    status = page = None
    try:
        status, page = _get_page(p, port)
        # one record, the parent's: the reload worker serves, but never records
        running = [json.loads(f.read_text()) for f in records.glob("*.json")]
    finally:
        if os.name == "nt":
            p.send_signal(STOP)
        else:
            os.killpg(p.pid, STOP)           # the reloader and its worker
        try:
            out = p.communicate(timeout=10)[0].decode()
        except subprocess.TimeoutExpired:
            _kill(p)
            out = p.communicate()[0].decode()
    assert status == 200, out
    assert out.count("symbion →") == 1, out   # one server announced, not two
    assert [(r["url"], r["store"]) for r in running] == \
        [(f"http://127.0.0.1:{port}", str(tmp_path.resolve()))], running
    # On Windows the console script is a launcher, and the serve runs under it.
    assert os.name == "nt" or running[0]["pid"] == p.pid, running
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


@pytest.mark.parametrize("stop", [CTRL_C, STOP], ids=["ctrl-c", "term"])
def test_ctrl_c_stops_a_serve_quietly(tmp_path, stop):
    """Ctrl-C is how a person stops a serve. uvicorn shuts down cleanly, then
    re-raises the SIGINT it caught, and the KeyboardInterrupt came out of
    ui.run() as a 48-line traceback and exit 1. --reload's supervisor already
    exits 0 on it. A SIGTERM (`kill`, launchd, a shutdown) died under the
    default handler and left the serve's record behind (2026-10-09)."""
    store.ensure_store(tmp_path)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    p = subprocess.Popen(
        [SYMBION, "--dir", str(tmp_path), "serve", "--no-browser",
         "--port", str(port), "--author", "t"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **GROUP)
    try:
        status, _ = _get_page(p, port)
        p.send_signal(stop)
        out = p.communicate(timeout=15)[0].decode()
    finally:
        if p.poll() is None:
            _kill(p)
            p.communicate()
    assert status == 200, out
    assert "Traceback" not in out and p.returncode == 0, (p.returncode, out)
    assert not list((tmp_path / "cache" / "symbion" / "serve").glob("*.json"))


def _command(pid) -> list[str]:
    return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.split()


@pytest.mark.skipif(os.name == "nt", reason="no SIGUSR1 on Windows")
def test_restart_re_execs_every_serve_in_its_own_terminal(tmp_path):
    """A serve runs the symbion it started with, so a code change or an
    upgrade reached none of the serves a person had running until each was
    restarted by hand (the owner, 2026-10-07). `serve --restart` re-execs
    each in place: the same process, so the same terminal, and with
    --no-browser, so no tab opens. It needs no store: run from anywhere."""
    store.ensure_store(tmp_path / "s")
    (tmp_path / "elsewhere").mkdir()
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["BROWSER"] = "true"                  # the first start opens "a browser"
    records = tmp_path / "cache" / "symbion" / "serve"
    p = subprocess.Popen(
        [SYMBION, "--dir", str(tmp_path / "s"), "serve", "--port", str(port), "--author", "t"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **GROUP)
    try:
        assert _get_page(p, port)[0] == 200
        (before,) = [json.loads(f.read_text()) for f in records.glob("*.json")]
        cli = lambda *a: subprocess.run([SYMBION, "serve", *a], cwd=tmp_path / "elsewhere",  # noqa: E731
                                        env=env, capture_output=True, text=True, timeout=60)
        restart = cli("--restart")
        status, _ = _get_page(p, port)
        (after,) = [json.loads(f.read_text()) for f in records.glob("*.json")]
        command = _command(p.pid)
        zombies = [ln for ln in subprocess.run(["ps", "-A", "-o", "ppid=,stat=,command="],
                                               capture_output=True, text=True).stdout.splitlines()
                   if ln.split()[0] == str(p.pid) and "Z" in ln.split()[1]]
        stop = cli("--stop")
        out = p.communicate(timeout=15)[0].decode()
    finally:
        if p.poll() is None:
            _kill(p)
            p.communicate()
    assert restart.returncode == 0, restart
    assert f"restarted {(tmp_path / 's').resolve()}: http://127.0.0.1:{port}" in restart.stdout
    assert status == 200 and after["pid"] == before["pid"] == p.pid, (before, after)
    assert after["started"] > before["started"]
    assert command[-1] == "--no-browser", command
    # multiprocessing's resource tracker exits when the exec closes its pipe,
    # and the new image never reaps it: a zombie per restart, for days
    assert not zombies, zombies
    assert stop.returncode == 0 and f"stopped {(tmp_path / 's').resolve()}" in stop.stdout, stop
    assert out.count("symbion →") == 2 and "Traceback" not in out, out
    assert p.returncode == 0, (p.returncode, out)
    assert not list(records.glob("*.json"))


@pytest.mark.skipif(os.name == "nt", reason="no ps on Windows; --restart refuses there")
def test_a_serve_is_known_by_its_whole_command_line(monkeypatch):
    """procps' and OpenBSD's `ps` cut `command` at $COLUMNS, into a pipe too:
    with it exported, `serve` fell off the line, every serve read as stale,
    and --restart deleted their records and said none was running (Linux,
    2026-10-08). macOS's ps does not cut, so only a Linux or BSD run sees
    this fail."""
    from symbion.gui import servers
    monkeypatch.setenv("COLUMNS", "20")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "symbion", "serve"])
    try:
        assert servers._a_serve(p.pid)
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(os.name == "nt", reason="no SIGUSR1 on Windows")
def test_restart_signals_no_process_that_is_not_a_serve(tmp_path, monkeypatch, capsys):
    """A serve killed outright leaves its record, and after a reboot its pid
    is soon another process's: SIGUSR1 ends most processes. A record whose
    pid runs no `symbion serve` is stale, and goes. One that does, and dies
    on the signal, is a serve started before --restart existed, or one whose
    restart failed: its terminal says which."""
    from symbion import cli
    from symbion.gui import servers
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    nap = "import time; time.sleep(60)"
    other = subprocess.Popen([sys.executable, "-c", nap])
    # A shell parent reaps it when it dies, as a serve's terminal does; a
    # child of this process would sit as a zombie, which reads as alive.
    shell = subprocess.Popen(["sh", "-c", f'"{sys.executable}" -c "{nap}" symbion serve & '
                              'echo $!; wait'], stdout=subprocess.PIPE, text=True)
    old = int(shell.stdout.readline())
    try:
        for name, pid in (("other", other.pid), ("old", old)):
            servers._dir().mkdir(parents=True, exist_ok=True)
            (servers._dir() / f"{pid}.json").write_text(json.dumps(
                {"pid": pid, "url": "http://127.0.0.1:1", "store": f"/x/{name}",
                 "started": 1.0}))
        rc = cli.main(["serve", "--restart"])
        out = capsys.readouterr().out
    finally:
        other.kill()
        other.wait()
        if shell.poll() is None:
            os.kill(old, signal.SIGKILL)
        shell.wait(timeout=10)
    assert rc == 1, out
    assert other.returncode == -signal.SIGKILL, "a process that is no serve got the signal"
    assert "/x/other" not in out and not (servers._dir() / f"{other.pid}.json").exists()
    assert f"/x/old: pid {old} exited instead of restarting" in out, out


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
    dead = subprocess.Popen([sys.executable, "-c", ""])
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


def test_a_scratch_store_records_only_into_a_scratch_cache(tmp_path, monkeypatch):
    """A scratch serve started without XDG_CACHE_HOME was listed in the
    owner's sidebar (2026-10-05). A store under a temp root records only
    when the cache is under one too, as a test's is; the owner's cache is
    not, even when XDG_CACHE_HOME names it."""
    from symbion.gui import servers
    scratch = tmp_path / "tmp"
    monkeypatch.setattr(servers, "_temp_roots", lambda: [scratch])
    for cache, records in ((tmp_path / "home-cache", False), (scratch / "cache", True)):
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
        assert bool(servers.record("http://127.0.0.1:1", scratch / "s")) is records, cache
        assert bool(servers.record("http://127.0.0.1:1", tmp_path / "s")), cache


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
    lst = _listener(_free_port())
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


# ---- --host and --allow: a serve another machine reaches ----

def test_a_wide_serve_lets_in_this_machine_and_allow_only():
    """The GUI writes with no login, so past loopback only --allow may
    connect. Both scopes: the page writes over its websocket, and an
    http-only check would guard nothing. Lifespan passes, or the app never
    starts."""
    import asyncio
    import ipaddress
    from symbion.gui.serve import AllowOnly
    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append((scope["type"], (scope.get("client") or ("",))[0]))

    async def send(m):
        sent.append(m)

    mw = AllowOnly(app, nets=[ipaddress.ip_network("10.1.0.0/16")])
    asyncio.run(mw({"type": "lifespan"}, None, send))
    ips = ["127.0.0.1", "::1", "10.1.2.3", "::ffff:10.1.2.3", "10.2.0.1", "::ffff:127.0.0.2"]
    for typ in ("http", "websocket"):
        for ip in ips:
            asyncio.run(mw({"type": typ, "client": (ip, 5000)}, None, send))
        asyncio.run(mw({"type": typ, "client": None}, None, send))
    assert reached == [("lifespan", "")] + [(t, ip) for t in ("http", "websocket")
                                            for ip in ips if ip != "10.2.0.1"]
    assert [m.get("status", m.get("code")) for m in sent if "body" not in m] \
        == [403, 403, 1008, 1008]


def test_a_loopback_serve_answers_only_to_an_address_or_localhost():
    """DNS rebinding: a web page re-points its own name at 127.0.0.1, and
    every peer check passes. The Host header still carries that name, and no
    page can be named by an IP literal or `localhost`. Both scopes, as above."""
    import asyncio
    from symbion.gui.serve import HostIsAddress
    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append((scope["type"], dict(scope.get("headers", [])).get(b"host")))

    async def send(m):
        sent.append(m)

    mw = HostIsAddress(app)
    asyncio.run(mw({"type": "lifespan"}, None, send))
    good = [b"127.0.0.1:5", b"localhost:5", b"LocalHost", b"[::1]:5", b"192.0.2.7"]
    bad = [b"rebind.example:5", b"127.0.0.1.rebind.example", b"[::1", b"", None]
    for typ in ("http", "websocket"):
        for h in good + bad:
            asyncio.run(mw({"type": typ, "headers": [] if h is None else [(b"host", h)]},
                           None, send))
    assert reached == [("lifespan", None)] + [(t, h) for t in ("http", "websocket")
                                              for h in good]
    assert [m.get("status", m.get("code")) for m in sent if "body" not in m] \
        == [403] * len(bad) + [1008] * len(bad)


def test_a_serve_names_the_address_this_machine_reaches():
    """The recorded and printed URL: the summary's link and the other
    stores' sidebars use it. A serve on every address answers on loopback;
    one on a single address answers only there."""
    from symbion.gui.serve import local_url
    assert local_url("127.0.0.1", 5) == "http://127.0.0.1:5"
    assert local_url("0.0.0.0", 5) == "http://127.0.0.1:5"
    assert local_url("::", 5) == "http://[::1]:5"
    assert local_url("192.0.2.7", 5) == "http://192.0.2.7:5"
    assert local_url("fe80::7", 5) == "http://[fe80::7]:5"


def _main_host(tmp_path, monkeypatch, **kw):
    """serve.main up to ui.run with these flags: its return, what it gave
    ui.run, and the middleware it added (the app is nicegui's global one,
    so a real add would reach every later GUI test)."""
    from symbion import api
    from symbion.gui import serve
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(serve, "build_page", lambda ctx, author: None)
    seen, added = {}, []
    monkeypatch.setattr(serve.ui, "run", lambda **k: seen.update(k))
    monkeypatch.setattr(serve.app, "add_middleware",
                        lambda cls, **k: added.append({"cls": cls.__name__, **k}))
    store.ensure_store(tmp_path / "s-notes")
    kw.setdefault("port", _free_port())
    rc = serve.main(api.resolve(str(tmp_path / "s-notes")), author="t", show=False, **kw)
    return rc, seen, added


def test_a_serve_past_loopback_needs_allow(tmp_path, monkeypatch, capsys):
    import ipaddress
    rc, seen, added = _main_host(tmp_path, monkeypatch, host="0.0.0.0")
    assert rc == 1 and not seen and not added
    assert "--allow" in capsys.readouterr().err

    net = ipaddress.ip_network("10.1.0.0/16")
    rc, seen, added = _main_host(tmp_path, monkeypatch, host="0.0.0.0", allow=[net])
    assert rc == 0 and seen["host"] == "0.0.0.0"
    assert [a["cls"] for a in added] == ["AllowOnly"] and net in added[0]["nets"]
    assert "10.1.0.0/16" in capsys.readouterr().out

    rc, seen, added = _main_host(tmp_path, monkeypatch)
    assert rc == 0 and seen["host"] == "127.0.0.1"
    assert [a["cls"] for a in added] == ["HostIsAddress"]

    # On one address, this machine connects from that address, not loopback.
    rc, seen, added = _main_host(tmp_path, monkeypatch, host="192.0.2.7", allow=[net])
    assert ipaddress.ip_network("192.0.2.7/32") in added[0]["nets"]
    capsys.readouterr()
    # 192.0.2.7 is a documentation address, on no machine: the port probe
    # binds it, and says so.
    rc, seen, added = _main_host(tmp_path, monkeypatch, host="192.0.2.7", allow=[net],
                                 port=None)
    assert rc == 1 and not seen
    assert "error: cannot listen on 192.0.2.7" in capsys.readouterr().err


def test_a_bad_allow_is_a_usage_error(tmp_path, capsys):
    from symbion import cli
    store.ensure_store(tmp_path)
    assert cli.main(["--dir", str(tmp_path), "serve", "--allow", "lan"]) == 2
    assert "--allow: 'lan' does not appear to be" in capsys.readouterr().err
