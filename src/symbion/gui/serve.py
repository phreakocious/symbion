"""Process entry for `symbion serve`.

--reload is special. uvicorn's reload worker is a spawned process, and spawn
re-runs the parent's __main__ in it and nothing else. From the console script
that is its `if __name__ == "__main__"` guard, which skips main(); from
`python -m symbion` it is a package __main__, which spawn skips outright
(multiprocessing.spawn._fixup_main_from_name). Either way the worker never
calls ui.run() and startup dies with "You must call ui.run()". So main()
re-execs as `python -m symbion.gui.serve`: this module is then __main__, the
worker re-runs it as __mp_main__, and the block at the bottom sends both into
the same `serve` branch of the CLI.

`nicegui` is imported at MODULE scope, not inside main(): cli.py catches the
ImportError at `from .gui import serve` and turns it into an install
instruction. An import deferred into main() raises past that handler, which is
the bare traceback the handler exists to prevent.
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import sys
from pathlib import Path

from nicegui import ui

from . import servers
from .pages import build_page
from .theme import FAVICON_SVG

_DEFAULT_PORT = 43210


def pick_free_port(preferred: int = _DEFAULT_PORT) -> int:
    """Try `preferred`; if busy, ask the kernel for any free port (bind 0)."""
    for candidate in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", candidate))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("no free TCP port available")


def main(ctx, *, author: str, port=None, show: bool = True,
         reload: bool = False, argv=()) -> None:
    """`argv` is the CLI's own, `--dir` included: --reload re-runs it."""
    # With --reload, uvicorn spawns a worker that re-runs main(); its port is
    # unused, and it must neither announce the URL nor re-exec. The worker's
    # __main__ is still spawn's stub while this runs, so only the process
    # name tells the two apart.
    top = multiprocessing.current_process().name == "MainProcess"
    if reload and top and getattr(sys.modules["__main__"].__spec__, "name", None) != __name__:
        os.execv(sys.executable, [sys.executable, "-m", __name__, *argv])
    build_page(ctx, author=author)
    port = port if port is not None else pick_free_port()
    url = f"http://127.0.0.1:{port}"
    mine = None
    if top:
        print(f"symbion → {url}  (writing as {author})", flush=True)
        # For the other serves' sidebars. The first serve on a store stays
        # the one they link while it runs; a second records too, and its
        # links take over when the first stops.
        first = servers.serving(ctx.store_dir)
        if first:
            print(f"warning: another symbion serve runs on this store: {first['url']} "
                  f"(pid {first['pid']}). The other stores' sidebars link that one "
                  "until it stops, then this one.", file=sys.stderr, flush=True)
        mine = servers.record(url, ctx.store_dir)
    try:
        # Loopback only: the GUI writes rows with no authentication, and
        # ui.run() defaults to 0.0.0.0 outside native mode.
        ui.run(
            host="127.0.0.1", port=port, show=show, reload=reload,
            uvicorn_reload_dirs=str(Path(__file__).resolve().parent),
            uvicorn_reload_includes="*.py",
            title="symbion", dark=True, show_welcome_message=False, favicon=FAVICON_SVG,
            reconnect_timeout=30.0,
        )
    finally:
        if mine:
            mine.unlink(missing_ok=True)


if __name__ in {"__main__", "__mp_main__"}:
    from symbion.cli import main as _cli_main
    _rc = _cli_main()
    if __name__ == "__main__":       # the worker returns: uvicorn runs the app
        sys.exit(_rc)
