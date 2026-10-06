# tests/_resolvers.py
"""Fixtures for the resolver tests: a numeric resolver with a 1.0 window and a
store-derived catalog, both as scripts under tmp_path, so the tests exercise
the real protocol (stdin, exit codes) and not a Python stand-in."""
import sys
from pathlib import Path

from symbion.store import ensure_store

READING_RESOLVER = r'''
import sys
lines = sys.stdin.read().splitlines()
q, cands = float(lines[0]), lines[1:]
hits = sorted({c for c in cands if abs(float(c) - q) <= 1.0})
if len(hits) > 1:
    print("\n".join(hits)); sys.exit(2)
print(hits[0] if hits else f"{q:.2f}")
'''

STORE_CATALOG = r'''
import json, sys
p = sys.argv[1]
try:
    rows = [json.loads(l) for l in open(p) if l.strip()]
except FileNotFoundError:
    rows = []
for name in sorted({r["target"]["name"] for r in rows
                    if r["target"]["type"] == "reading" and r["target"]["name"]}):
    print(name)
'''


def reading_store(tmp_path: Path, *, counter: bool = False) -> Path:
    """A store dir with `reading` catalogued from its own notes.jsonl and
    resolved numerically. `counter=True` makes the resolver also append one
    line to <store>/resolver.calls per invocation."""
    store = tmp_path / "store"
    ensure_store(store)
    (tmp_path / "resolve_reading.py").write_text(READING_RESOLVER)
    (tmp_path / "catalog_reading.py").write_text(STORE_CATALOG)
    # Forward slashes: a C:\ path is an escape in TOML and in sh.
    py = Path(sys.executable).as_posix()
    resolver = f"{py} {(tmp_path / 'resolve_reading.py').as_posix()}"
    if counter:
        resolver = f"echo hit >> {(store / 'resolver.calls').as_posix()}; " + resolver
    (store / "symbion.toml").write_text(
        f'[catalogs]\nreading = "{py} {(tmp_path / "catalog_reading.py").as_posix()} {(store / "notes.jsonl").as_posix()}"\n'
        f'[resolvers]\nreading = "{resolver}"\n')
    return store


def calls(store: Path) -> int:
    p = store / "resolver.calls"
    return len(p.read_text().splitlines()) if p.exists() else 0
