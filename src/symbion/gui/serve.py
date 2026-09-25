"""Process entry for `symbion serve`.

--reload is special: multiprocessing's spawn reloader deliberately skips
re-running a package __main__ (its module spec name ends in '.__main__'; see
multiprocessing.spawn._fixup_main_from_name), so NiceGUI's ui.run() would never
re-execute in the reload worker and startup dies with "You must call ui.run()".
This module is not a __main__, so the worker re-runs it.

`nicegui` is imported at MODULE scope, not inside main(): cli.py catches the
ImportError at `from .gui import serve` and turns it into an install
instruction. An import deferred into main() raises past that handler, which is
the bare traceback the handler exists to prevent.
"""
from __future__ import annotations

import multiprocessing
import socket
from pathlib import Path

from nicegui import ui

from .pages import build_page

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
         reload: bool = False) -> None:
    build_page(ctx, author=author)
    port = port if port is not None else pick_free_port()
    # Only the top-level process announces the URL. With --reload, uvicorn
    # spawns workers that re-run main(); their port is unused.
    if multiprocessing.current_process().name == "MainProcess":
        print(f"symbion → http://localhost:{port}  (writing as {author})", flush=True)
    ui.run(
        port=port, show=show, reload=reload,
        uvicorn_reload_dirs=str(Path(__file__).resolve().parent),
        uvicorn_reload_includes="*.py",
        title="symbion", dark=True, show_welcome_message=False,
        reconnect_timeout=30.0,
    )
