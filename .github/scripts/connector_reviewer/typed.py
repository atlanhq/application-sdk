"""Typed stand-ins for reading JSON whose shape the type checker cannot know.

GitHub events, API replies and model output arrive as untyped JSON, and the
code reads them defensively: `(comment.get("user") or {}).get("login")`. A bare
`{}` there is a `dict[Unknown, Unknown]`, so pyright reports every read made
through it (`reportUnknownMemberType`), and `.github/scripts` holds a new file
to zero such findings (docs/standards/ci.md). `EMPTY` and `objects` say the
same thing with a type. Neither changes what the code does at run time.

lens writes these reads with `{}` and `isinstance(x, dict)`; this module is one
of the fork's differences (`.github/connector-reviewer/FORK.md`).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

# An empty JSON object to fall back to: `(value.get("user") or EMPTY).get("login")`.
# Read-only, so one shared instance is safe.
EMPTY: Mapping[str, Any] = MappingProxyType({})


def objects(value: Any) -> list[dict[str, Any]]:
    """The JSON objects in `value`, a list from untrusted JSON (or None).
    An entry that is not an object is dropped."""
    return [cast("dict[str, Any]", x) for x in value or [] if isinstance(x, dict)]
