"""`symbion serve` as a process: what the `user` fixture never runs, since it
builds the pages in-process and skips serve.main() and uvicorn entirely."""
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
    finally:
        os.killpg(p.pid, signal.SIGTERM)     # the reloader and its worker
        try:
            out = p.communicate(timeout=10)[0].decode()
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out = p.communicate()[0].decode()
    assert status == 200, out
    assert out.count("symbion →") == 1, out   # one server announced, not two
    # ui.run's favicon, which only a served page carries: NiceGUI's own icon
    # is a static .ico, ours an inline SVG.
    assert 'href="data:image/svg+xml' in page and "favicon.ico" not in page, page[:2000]
    # What tells Dark Reader the page is already dark: ui.run(dark=True) writes
    # the color-scheme meta its detector reads first; shell() adds its lock.
    assert re.search(r'name="color-scheme"\s+content="dark"', page), page[:2000]
    assert '<meta name="darkreader-lock">' in page, page[:2000]
