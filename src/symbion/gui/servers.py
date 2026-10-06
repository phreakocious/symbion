"""Which `symbion serve` processes run here, and on which store. The sidebar
links the other stores' (the owner, 2026-10-01: switch stores in the GUI).
One serve per store keeps each store's author and repo; a store in every
URL of one process would not.

Each serve writes one record while it runs, under the user's cache
directory rather than the store, so a store anywhere is found. Two serves
on one store both record; the links go to the earlier while it runs, then
to the later, with nothing to poll. No nicegui here: a reader outside the
GUI may use it."""
from __future__ import annotations

import json
import os
import tempfile
import time
import zlib
from pathlib import Path


def _dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "symbion" / "serve"


def name(store) -> str:
    """A store's name: its directory, less a `-notes` suffix. The sidebar
    shows it, and the store's port derives from it."""
    return Path(store).resolve().name.removesuffix("-notes")


def port(store) -> int:
    """The store's own port: the same at every restart, from every session,
    and on every machine where its directory has the same name (the owner,
    2026-10-02: a row link must not go stale). Below the ports either OS
    gives outgoing connections (macOS from 49152, Linux from 32768)."""
    return 20000 + zlib.crc32(name(store).encode()) % 10000


def _temp_roots() -> list[Path]:
    # Both: on macOS TMPDIR is under /private/var/folders, while a Claude
    # Code scratchpad, where a session's scratch store sits, is under /tmp.
    return [Path(tempfile.gettempdir()), Path("/tmp")]


def _scratch(path) -> bool:
    p = Path(path).resolve()
    return any(p.is_relative_to(r.resolve()) for r in _temp_roots())


def record(url: str, store) -> Path | None:
    """This process's record; the caller removes the path when it stops.
    None for a scratch store when the cache is not scratch too: a scratch
    serve started without XDG_CACHE_HOME was listed in the owner's sidebar
    (2026-10-05). A test's cache is under a temp root, so it still records."""
    d = _dir()
    if _scratch(store) and not _scratch(d):
        return None
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{os.getpid()}.json"
    path.write_text(json.dumps({"pid": os.getpid(), "url": url,
                                "store": str(Path(store).resolve()),
                                "started": time.time()}), encoding="utf-8")
    return path


def running() -> list[dict]:
    """One live record per store, the earliest started: a store's links
    stay on its first serve until it stops. A dead one's record is removed:
    a serve killed outright never removes its own."""
    live = []
    for path in _dir().glob("*.json"):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
            pid, _ = int(r["pid"]), (r["url"], r["store"])
            started = float(r.get("started", 0))
        except (OSError, ValueError, KeyError, TypeError):
            continue                       # torn mid-write, or not ours
        if _alive(pid):
            live.append((started, r))
        else:
            path.unlink(missing_ok=True)
    first = {}
    for _, r in sorted(live, key=lambda sr: sr[0]):
        first.setdefault(r["store"], r)
    return list(first.values())


def serving(store) -> dict | None:
    """The record a store's links go to, or None while no serve runs on it."""
    here = str(Path(store).resolve())
    return next((r for r in running() if r["store"] == here), None)


def _alive(pid: int) -> bool:
    # ponytail: a pid reused after a crash reads as alive; a port check would
    # catch it, if a stale link ever shows up.
    if os.name == "nt":
        # On Windows signal 0 is CTRL_C_EVENT, and os.kill(pid, 0) raised
        # nothing for a pid that had exited: every record read as alive.
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)    # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5    # ERROR_ACCESS_DENIED: someone else's
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259      # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                        # alive, someone else's
    return True
