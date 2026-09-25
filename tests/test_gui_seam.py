"""gui/ may read from store, never write through it.

An ALLOWLIST, not a denylist. store.py has eleven writers -- ensure_store, add,
add_many, supersede, rename_target, commit, create_arc, rename_arc,
archive_arc, seed_arc, apply_reconciliation -- so any list of
forbidden names is a snapshot that silently permits the twelfth. An earlier
draft of the spec enumerated five and failed open on exactly the three the GUI
advertises. Naming the READS instead means a new store writer is denied by
default.

The audit resolves the local name store is bound to rather than matching the
literal text `store.`. `from .. import store as S` is the import style the rest
of this package uses, and a regex anchored on `store.` sees nothing in such a
file -- it would pass by matching zero attributes, which is the same
false-clean this module's third test exists to rule out at the directory level.
A direct symbol import (`from ..store import add`) has no attribute access at
all to inspect, so it is refused outright rather than parsed.

Reads source text rather than importing, so it runs without nicegui installed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

GUI_DIR = Path(__file__).resolve().parent.parent / "src" / "symbion" / "gui"

ALLOWED_STORE_READS = {
    "load", "load_malformed", "load_arcs", "heads", "heads_for", "query",
    "tag_counts", "arc_items", "arc_progress", "read_status", "due_state", "exists",
    "notes_path", "arcs_path", "Note", "Arc", "Target",
    "STATUSES", "BUILTIN_TARGET_TYPES",
}

# `from <anything> import ..., store [as X], ...` -- the module-object import.
_MODULE_IMPORT = re.compile(r"^[ \t]*from[ \t]+[.\w]*[ \t]+import[ \t]+([^\n#]+)", re.M)
# `from <anything>.store import name, ...` -- symbols lifted straight out.
_SYMBOL_IMPORT = re.compile(r"^[ \t]*from[ \t]+[.\w]*\bstore[ \t]+import[ \t]+([^\n#]+)", re.M)


def _store_aliases(text: str) -> set:
    """Every local name bound to the store MODULE in this file."""
    names = set()
    for m in _MODULE_IMPORT.finditer(text):
        for part in m.group(1).split(","):
            bits = part.strip().split()
            if bits[:1] == ["store"]:
                names.add(bits[2] if bits[1:2] == ["as"] else "store")
    return names


def _violations(text: str) -> set:
    """Store attributes touched here that are not reads on the allowlist.

    A symbol imported out of store directly is reported under its own name: it
    is used bare, so no attribute access exists for the allowlist to judge."""
    bad = set()
    for m in _SYMBOL_IMPORT.finditer(text):
        bad |= {p.strip().split()[0] for p in m.group(1).split(",") if p.strip()}
    for alias in _store_aliases(text) or {"store"}:
        attrs = re.findall(rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)", text)
        bad |= set(attrs) - ALLOWED_STORE_READS
    return bad


def test_gui_never_writes_through_store():
    bad = {}
    for p in sorted(GUI_DIR.rglob("*.py")):
        v = _violations(p.read_text(encoding="utf-8"))
        if v:
            bad[p.name] = sorted(v)
    assert not bad, (
        f"gui/ may only read from store; these are not on the allowlist: {bad}. "
        f"Writes go through symbion.api, which stamps author, canonicalizes "
        f"names and refs, and applies provenance.")


def test_the_audit_fires_on_a_planted_violation():
    """The other direction. Without this the audit could pass by matching
    nothing -- a regex typo, a moved directory, an empty glob."""
    assert _violations("x = store.supersede(d, i)") == {"supersede"}
    assert _violations("store.add_many(d, rows)") == {"add_many"}
    assert _violations("rows = store.query(notes, tag='x')") == set()


def test_the_audit_follows_an_aliased_import():
    """`from .. import store as S` is this package's own import style. Anchored
    on the literal `store.`, the audit would read every such file as clean."""
    aliased = "from .. import store as S\nS.supersede(d, i)\n"
    assert _violations(aliased) == {"supersede"}
    assert _violations("from .. import store as S\nS.read_status(n)\n") == set()
    # ...and the alias must not be mistaken for a second, still-live `store`.
    assert _store_aliases(aliased) == {"S"}
    assert _store_aliases("from .. import api, store as S\n") == {"S"}
    assert _store_aliases("from .. import store\n") == {"store"}


def test_a_direct_symbol_import_is_refused():
    """`from ..store import add` leaves a bare `add(...)` with no attribute
    access to inspect, so the allowlist can never see it. Refuse the import."""
    assert _violations("from ..store import add\nadd(d, kind='note')\n") == {"add"}
    assert _violations("from symbion.store import add, heads\n") == {"add", "heads"}
    # A module import on the same shape must NOT trip the symbol rule.
    assert _violations("from .. import store\nstore.load(d)\n") == set()


def test_the_gui_directory_is_actually_being_scanned():
    """A clean result must mean 'scanned and clean', never 'scanned nothing'."""
    assert GUI_DIR.is_dir(), f"{GUI_DIR} missing"
    assert list(GUI_DIR.rglob("*.py")), "no gui modules found to audit"


# ---- the missing-extra path ----
# Imported down here so the audit above stays importable without nicegui: these
# names are only needed by the CLI test, which never touches gui/ source text.
import builtins  # noqa: E402
import sys  # noqa: E402

from symbion import cli  # noqa: E402


def test_serve_without_the_extra_explains_itself(repo, tmp_path, monkeypatch, capsys):
    real_import = builtins.__import__

    def no_nicegui(name, *a, **kw):
        if name == "nicegui" or name.startswith("nicegui."):
            raise ImportError("No module named 'nicegui'")
        return real_import(name, *a, **kw)

    # A previously imported serve module would satisfy the import from cache and
    # never reach the patched hook -- the test would then pass while proving
    # nothing about the handler.
    for mod in [m for m in sys.modules if m.startswith("symbion.gui")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", no_nicegui)
    rc = cli.main(["--dir", str(tmp_path), "serve"])

    assert rc == 1
    assert "symbion[gui]" in capsys.readouterr().err


def test_a_broken_gui_import_is_not_reported_as_a_missing_extra(
        repo, tmp_path, monkeypatch, capsys):
    """The other direction, and the reason the handler inspects the message.
    Answering every ImportError with 'pip install symbion[gui]' sends the
    reader to reinstall an extra they already have."""
    real_import = builtins.__import__

    def broken_pages(name, *a, **kw):
        if name.endswith("pages") or name == "pages":
            raise ImportError("No module named 'symbion.gui.pages'")
        return real_import(name, *a, **kw)

    for mod in [m for m in sys.modules if m.startswith("symbion.gui")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", broken_pages)
    with pytest.raises(ImportError, match="pages"):
        cli.main(["--dir", str(tmp_path), "serve"])
