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

import ipaddress
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from nicegui import app, ui
from nicegui.helpers import format_url

from . import servers
from .pages import build_page
from .theme import FAVICON_SVG


class AllowOnly:
    """ASGI: only this machine and `nets` may connect. Both scopes: the page
    writes over its websocket, so an http-only check guards nothing."""

    def __init__(self, app, nets) -> None:
        self.app, self.nets = app, nets

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and not self._allowed(scope.get("client")):
            return await _refuse(scope, send, b"symbion serve: not in its --allow\n")
        await self.app(scope, receive, send)

    def _allowed(self, client) -> bool:
        try:
            ip = ipaddress.ip_address(client[0])
        except (TypeError, ValueError):
            return False
        # A dual-stack bind (`::`) sees an IPv4 client as ::ffff:a.b.c.d.
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return ip.is_loopback or any(ip in n for n in self.nets)


class HostIsAddress:
    """ASGI: a loopback serve answers only to a Host of `localhost` or an IP
    address. A web page reaches it by DNS rebinding: the page's own name,
    re-pointed at 127.0.0.1, passes every peer check, and the serve's port
    follows from the store's name. No page is named by an address."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            try:
                name = urlsplit("//" + host).hostname or ""
            except ValueError:                      # `[::1` with no `]`
                name = ""
            if name != "localhost" and not _ip(name):
                return await _refuse(scope, send, b"symbion serve: open it at localhost or "
                                                  b"an IP address; a web page reaches it "
                                                  b"under any other name\n")
        await self.app(scope, receive, send)


async def _refuse(scope, send, body: bytes) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
    else:
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})


def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None                         # a name


def loopback(host: str) -> bool:
    ip = _ip(host)
    return ip.is_loopback if ip else host == "localhost"


def local_url(host: str, port: int) -> str:
    """Where this machine reaches a serve on `host`: on every address, by
    loopback; on one address, only there."""
    ip = _ip(host)
    if ip and ip.is_unspecified:
        host = "127.0.0.1" if ip.version == 4 else "::1"
    return format_url("http", host, port)


def _binds(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                           socket.SOCK_STREAM) as s:
            # On Windows SO_REUSEADDR binds over a live listener, and every
            # held port read as free; there a bind without it is the probe.
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def pick_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """`preferred` if it is free, else any free port. Two test binds, both
    with SO_REUSEADDR, as uvicorn binds: without it, the last serve's
    connections in TIME_WAIT refuse the port, and the serve moves at each
    restart. On macOS each bind alone misses a holder: one on 127.0.0.1
    passes the any-address bind, and one on every address passes the
    127.0.0.1 bind, where uvicorn would then take its loopback traffic
    (measured 2026-10-02). A third on `host`, the address uvicorn binds:
    macOS passes both others beside a holder there. Raises OSError when
    `host` is no address of this machine."""
    if all(_binds(h, preferred) for h in {"127.0.0.1", "", host}):
        return preferred
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                       socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main(ctx, *, author: str, port=None, show: bool = True, reload: bool = False,
         argv=(), host: str = "127.0.0.1", allow=()) -> int:
    """`argv` is the CLI's own, `--dir` included: --reload re-runs it.
    `allow` is ip_network objects: who besides this machine may connect."""
    if not loopback(host) and not allow:
        print(f"error: --host {host} lets other machines connect, and the GUI writes "
              "with no login: name the ones that may with --allow (an address or a "
              "network, e.g. 192.168.1.0/24)", file=sys.stderr)
        return 1
    if hasattr(signal, "SIGBREAK"):
        # Windows' Ctrl+Break: uvicorn stops on it, then re-raises it under
        # the default handler, which ended the process before the finally
        # below took the record off. It stops as Ctrl-C does instead.
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    # SIGTERM (`kill`, launchd, systemd, a shutdown) the same way: under the
    # default handler the record outlived the serve (2026-10-09).
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    # With --reload, uvicorn spawns a worker that re-runs main(); its port is
    # unused, and it must neither announce the URL nor re-exec. The worker's
    # __main__ is still spawn's stub while this runs, so only the process
    # name tells the two apart.
    top = multiprocessing.current_process().name == "MainProcess"
    if reload and top and getattr(sys.modules["__main__"].__spec__, "name", None) != __name__:
        cmd = [sys.executable, "-m", __name__, *argv]
        if os.name != "nt":
            os.execv(sys.executable, cmd)
        # Windows has no exec: os.execv starts a new process and ends this
        # one, so the console script returned at once and the serve ran on,
        # detached from its terminal. A Ctrl-C reaches the child too, and the
        # child stops on it; this waits for that.
        with subprocess.Popen(cmd) as child:
            while True:
                try:
                    sys.exit(child.wait())
                except KeyboardInterrupt:
                    pass
    build_page(ctx, author=author)
    if not loopback(host):
        # And the bound address: this machine reaches a serve on one address
        # from that address, not from loopback.
        own = _ip(host)
        app.add_middleware(AllowOnly, nets=[*allow, ipaddress.ip_network(own)] if own
                           else [*allow])
    else:
        # ponytail: a wide serve checks the peer, not the Host: a name for
        # this machine is fair there and unknowable here. A page in an
        # allowed client's browser can still rebind; a Host list flag
        # closes that if a wide serve ever faces untrusted browsing.
        app.add_middleware(HostIsAddress)
    # The store's own port, so a link to it outlives a restart (the owner,
    # 2026-10-02). `--port` overrides it and is never warned about.
    want = servers.port(ctx.store_dir) if port is None else None
    try:
        port = port if port is not None else pick_free_port(want, host)
    except OSError as e:
        print(f"error: cannot listen on {host}: {e.strerror or e}", file=sys.stderr)
        return 1
    url = local_url(host, port)
    mine = None
    if top:
        print(f"symbion → {url}  (writing as {author})", flush=True)
        if not loopback(host):
            print(f"listening on {host}, port {port}, for this machine and "
                  f"{', '.join(map(str, allow))}. There is no login: each of them "
                  f"writes as {author}.", flush=True)
        # For the other serves' sidebars. The first serve on a store stays
        # the one they link while it runs; a second records too, and its
        # links take over when the first stops.
        first = servers.serving(ctx.store_dir)
        if first:
            print(f"warning: another symbion serve runs on this store: {first['url']} "
                  f"(pid {first['pid']}). The other stores' sidebars link that one "
                  "until it stops, then this one.", file=sys.stderr, flush=True)
        elif want is not None and port != want:
            # Two names can give one port: say whose serve holds it.
            held = next((r for r in servers.running()
                         if urlsplit(r["url"]).port == want), None)
            who = (f"the serve on {held['store']}, a store whose name gives the same port"
                   if held else "a process that is not a symbion serve")
            print(f"warning: this store's port, {want}, is held by {who}. This serve "
                  f"takes {port}, which changes at each restart; --port picks a fixed one.",
                  file=sys.stderr, flush=True)
        mine = servers.record(url, ctx.store_dir)
    restart = []
    if top and hasattr(signal, "SIGUSR1"):
        # `serve --restart`: stop as Ctrl-C does, then exec again below. Not
        # SIGHUP: a closed terminal sends that to its foreground job.
        def _restart(*_):
            restart.append(True)
            signal.raise_signal(signal.SIGINT)
        signal.signal(signal.SIGUSR1, _restart)
    try:
        # Never ui.run()'s default, 0.0.0.0 outside native mode: the GUI
        # writes rows with no authentication.
        ui.run(
            host=host, port=port, show=show, reload=reload,
            uvicorn_reload_dirs=str(Path(__file__).resolve().parent),
            uvicorn_reload_includes="*.py",
            title="symbion", dark=True, show_welcome_message=False, favicon=FAVICON_SVG,
            reconnect_timeout=30.0,
        )
    except KeyboardInterrupt:
        # Ctrl-C: uvicorn has already shut down, then re-raised the SIGINT
        # it caught. A stop, not a crash; --reload's supervisor exits 0 too.
        pass
    finally:
        if mine:
            mine.unlink(missing_ok=True)
    if restart:
        # The same process, so the same terminal and Ctrl-C; the browser
        # tab it opened at the first start reconnects on its own.
        print("symbion serve: restarting", flush=True)
        # multiprocessing's resource tracker, a child, exits when the exec
        # closes its pipe, and the new image never reaps it: a zombie per
        # restart. _stop() closes the pipe and reaps it.
        # ponytail: private API, there from Python 3.8 through 3.14; if it
        # goes, the getattr skips it and the zombies come back.
        from multiprocessing import resource_tracker
        getattr(resource_tracker._resource_tracker, "_stop", lambda: None)()
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:],
                                  *(["--no-browser"] if show else [])])
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    from symbion.cli import main as _cli_main
    _rc = _cli_main()
    if __name__ == "__main__":       # the worker returns: uvicorn runs the app
        sys.exit(_rc)
