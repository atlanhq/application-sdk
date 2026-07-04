"""Shared "read a rendered param back off an on-disk managed file" extractors.

Leaf module: no dependency on ``conformance.suite`` or ``conformance.bootstrap.command``.
Both the C002 drift checker (``conformance.suite.checks.bootstrap_drift``) and the
``bootstrap`` command's re-run autodetection (``conformance.bootstrap.command``)
import from here at module level, so a template format change can't leave one
caller silently out of sync with the other — and neither layer has to reach
into the other's module to share this logic (which previously produced a
``bootstrap.command -> suite.checks.bootstrap_drift -> bootstrap.render``
import cycle, dodged only by making the ``command.py`` side's imports
function-local).
"""

from __future__ import annotations

import json
import re

# conformance.yaml's exit-zero mode is rendered as a GitHub Actions expression
# (`exit-zero: ${{ ... || << exit_zero >> }}`), not a plain `key: "value"` pair
# — the boolean is the last token before the closing `}}`.
EXIT_ZERO_RE = re.compile(r"exit-zero:.*\|\|\s*(true|false)\s*\}\}")


def extract_field(text: str, field: str) -> str:
    """Return the value of ``field: <value>`` in *text*, or ``""`` if absent.

    *value* may be bare or quoted (``field: value`` or ``field: "value"``);
    quotes are stripped. Matches the first ``field:`` line, at any
    indentation level. Single source of truth for "read a rendered param
    back off an on-disk managed file" — both the C002 drift-comparison
    extractors and ``bootstrap``'s re-run autodetection call this, so a
    template format change can't leave one caller silently out of sync with
    the other.
    """
    for line in text.splitlines():
        m = re.match(rf"^\s*{re.escape(field)}:\s*(\S+)", line)
        if m:
            return m.group(1).strip("\"'")
    return ""


def extract_renovate_automerge(text: str) -> str:
    """Return renovate.json's automerge mode (``"true"``/``"false"``) from *text*.

    renovate.json's soft-mode block (rendered only when ``automerge ==
    "false"``) is a Jinja ``<% if %>`` block, not a substitutable value —
    detected structurally via the ``lockFileMaintenance`` key that block's
    canonical content always adds, not by matching the human-readable
    ``description`` prose inside it, so wording edits to that prose can't
    silently break mode detection.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "true"
    return (
        "false" if isinstance(data, dict) and "lockFileMaintenance" in data else "true"
    )
