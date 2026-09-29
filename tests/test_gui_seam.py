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

import io
import re
import tokenize
from pathlib import Path

import pytest

GUI_DIR = Path(__file__).resolve().parent.parent / "src" / "symbion" / "gui"

ALLOWED_STORE_READS = {
    "load", "load_malformed", "load_arcs", "heads", "heads_for", "query",
    "tag_counts", "arc_items", "arc_progress", "read_status", "due_state", "since_cutoff",
    "exists",
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


def _code(text: str) -> str:
    """`text` without its comments: a comment that says `not store.supersede`
    is not a call. tokenize, not a `#` regex, so a `#` inside a string does
    not cut off the code after it."""
    toks = tokenize.generate_tokens(io.StringIO(text).readline)
    return tokenize.untokenize(t for t in toks if t.type != tokenize.COMMENT)


def _violations(text: str, allowed=ALLOWED_STORE_READS) -> set:
    """Store attributes touched here that are not on the allowlist.

    A symbol imported out of store directly is reported under its own name: it
    is used bare, so no attribute access exists for the allowlist to judge."""
    text = _code(text)
    bad = set()
    for m in _SYMBOL_IMPORT.finditer(text):
        bad |= {p.strip().split()[0] for p in m.group(1).split(",") if p.strip()}
    for alias in _store_aliases(text) or {"store"}:
        attrs = re.findall(rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)", text)
        bad |= set(attrs) - allowed
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


def test_the_audit_skips_comments_but_not_code_after_a_hash_in_a_string():
    assert _violations("# api.supersede, not store.supersede\nstore.load(d)\n") == set()
    assert _violations('x = "#"; store.add(d)\n') == {"add"}


# cli.py reads more than gui/ does, and makes two writes no api function
# covers: `init` creates the store (no row), and `arc reconcile --apply` moves
# rows onto names taken from the catalog, canonical already. Every other write
# goes through api. `rename` did not, and stored a short sha as typed.
CLI_FILE = GUI_DIR.parent / "cli.py"
CLI_STORE_CALLS = ALLOWED_STORE_READS | {
    "note_from_dict", "read_dict", "reconcile_arc",      # reads
    "ensure_store", "apply_reconciliation",               # the two writes
}


def test_cli_writes_through_api_but_for_two_named_calls():
    assert CLI_FILE.is_file(), f"{CLI_FILE} missing"
    bad = _violations(CLI_FILE.read_text(encoding="utf-8"), CLI_STORE_CALLS)
    assert not bad, (
        f"cli.py calls store directly for {sorted(bad)}. Write through "
        f"symbion.api, which resolves names and refs the way add does.")


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

from symbion import store, cli  # noqa: E402


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
    store.ensure_store(tmp_path)      # an absent store stops serve first
    monkeypatch.setattr(builtins, "__import__", no_nicegui)
    rc = cli.main(["--dir", str(tmp_path), "serve"])

    assert rc == 1
    assert "symbion[gui]" in capsys.readouterr().err


def test_a_broken_gui_import_is_not_reported_as_a_missing_extra(
        repo, tmp_path, monkeypatch, capsys):
    """The other direction, and the reason the handler inspects the message.
    Answering every ImportError with 'pip install symbion[gui]' sends the
    reader to reinstall an extra they already have."""
    pytest.importorskip("nicegui")   # without it, serve stops at the missing extra
    real_import = builtins.__import__

    def broken_pages(name, *a, **kw):
        if name.endswith("pages") or name == "pages":
            raise ImportError("No module named 'symbion.gui.pages'")
        return real_import(name, *a, **kw)

    for mod in [m for m in sys.modules if m.startswith("symbion.gui")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    store.ensure_store(tmp_path)      # an absent store stops serve first
    monkeypatch.setattr(builtins, "__import__", broken_pages)
    with pytest.raises(ImportError, match="pages"):
        cli.main(["--dir", str(tmp_path), "serve"])


def test_serve_listens_on_loopback_only(monkeypatch):
    """The GUI writes rows with no authentication, so it must not be reachable
    from the network. NiceGUI's ui.run() defaults to host 0.0.0.0 outside
    native mode; serve has to pass the loopback address itself."""
    pytest.importorskip("nicegui")
    from symbion.gui import serve
    seen = {}
    monkeypatch.setattr(serve, "build_page", lambda ctx, author: None)
    monkeypatch.setattr(serve.ui, "run", lambda **kw: seen.update(kw))
    serve.main(None, author="t", port=1, show=False)
    assert seen["host"] == "127.0.0.1"
