"""pyflakes over src and tests: a name imported or assigned and never used, or
used and never defined. The rules are pyproject.toml's [tool.ruff.lint], so
`ruff check` at a terminal agrees with this test."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_pyflakes_finds_nothing():
    pytest.importorskip("ruff", reason="[test] leaves ruff out where it has no wheel (OpenBSD)")
    r = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "src", "tests"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
