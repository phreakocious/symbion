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
        deadline = time.monotonic() + 30
        while status is None and time.monotonic() < deadline and p.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                    status, page = r.status, r.read().decode()
            except OSError:
                time.sleep(0.3)
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
