"""Defensive file reading for checkers.

``Path.read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` on
undecodable bytes.  That is a ``ValueError``, **not** an ``OSError``, so the
reflexive ``except OSError:`` guard does not catch it and the exception escapes
the checker — and ``runner.py`` wraps neither ``discover()`` nor ``scan_all()``,
so one stray byte in a consumer repo aborts that repo's entire multi-series run.
A crashed check reports nothing, and silence reads as coverage: the exact
failure mode this suite exists to catch.

This class was fixed three times by enumerating the sites a review listed, and
survived each round through a reader nobody had enumerated.  The durable form is
one shared reader, so that "read defensively" is a property of the helper rather
than a rule every future checker author has to remember.

Use :func:`safe_read_text` for text and :func:`safe_read_json` for JSON.  Both
return ``None`` on any unreadable/undecodable input, so call sites express the
skip explicitly::

    text = safe_read_text(path)
    if text is None:
        continue

``read_bytes()`` needs no wrapper: there is no decode step, and ``ast.parse``
surfaces a bad-encoding buffer as ``SyntaxError``, which callers already catch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["safe_read_json", "safe_read_text"]


def safe_read_text(path: Path, *, encoding: str = "utf-8") -> str | None:
    """Read *path* as text, returning ``None`` if it cannot be read or decoded."""
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError):
        return None


def safe_read_json(path: Path) -> Any | None:
    """Read and parse *path* as JSON, returning ``None`` on any failure."""
    text = safe_read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
