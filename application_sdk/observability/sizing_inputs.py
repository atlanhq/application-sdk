"""How much data an activity was given — the driver variable for tier sizing.

Peak memory alone says a tier is wrong; it cannot say what to key the tier *on*.
That needs the input size, so a rule can be fitted and evaluated before the
activity runs.

Two sources, in order:

1. **``FileReference`` fields on the Input.** Zero config, and the bytes are
   already on local disk: the SDK materialises durable refs at the top of the
   activity, inside the window the sizing interceptor measures, so this is a
   local ``stat`` rather than an object-store call.
2. **``sizing_input_bytes()`` on the Input.** The escape hatch for apps that pass
   raw object-store paths instead of ``FileReference`` — AE's ``merge`` takes
   ``input_prefixes: list[str]`` and would otherwise report nothing.

``None`` means unknown, never 0 — a zero would fit a rule to inputs nobody sized.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)

#: Optional method an Input may define to report its own size in bytes.
SIZING_HOOK = "sizing_input_bytes"

# A stat per file, so a pathological ref would walk a whole tree at activity end.
# Bounded here rather than trusted: the number is telemetry, and a partial count
# labelled ``truncated`` is more useful than an activity slowed down measuring
# itself.
_MAX_FILES_WALKED = 10_000


@dataclass(frozen=True)
class InputSize:
    """Bytes an activity was handed, and where the number came from.

    ``basis`` segments the data: ``file_reference`` is measured, ``hook`` is
    whatever the app chose to report, and mixing them silently would fit one rule
    to two different definitions of "input".
    """

    bytes: int
    file_count: int
    basis: str
    truncated: bool = False


def describe_inputs(input_data: Any) -> InputSize | None:
    """Size an activity's input, or ``None`` if it cannot be determined.

    Never raises: this runs beside a real activity and must not affect it.
    """
    try:
        from_refs = _size_from_file_refs(input_data)
        if from_refs is not None:
            return from_refs
        return _size_from_hook(input_data)
    # conformance: ignore[E004] telemetry; an unsizeable input is "unknown", not a failure
    except Exception:
        _logger.debug("input sizing failed", exc_info=True)
        return None


def _size_from_file_refs(input_data: Any) -> InputSize | None:
    """Sum the on-disk size of every materialised ``FileReference``."""
    from application_sdk.storage.file_ref_sync import (  # noqa: PLC0415 — circular: storage imports contracts which import observability
        _find_file_refs,
    )

    refs = _find_file_refs(input_data)
    if not refs:
        return None

    total = 0
    files = 0
    truncated = False
    for ref in refs:
        if not ref.local_path:
            # Durable but not materialised (``auto_materialize=False``). Sizing it
            # would need an object-store call per activity, so it is left to the
            # hook — the app that opted out already knows its own sizes.
            continue
        size, count, hit_cap = _walk(ref.local_path)
        total += size
        files += count
        truncated = truncated or hit_cap

    if files == 0:
        return None
    return InputSize(
        bytes=total, file_count=files, basis="file_reference", truncated=truncated
    )


def _walk(path: str) -> tuple[int, int, bool]:
    """``(bytes, file_count, hit_cap)`` for a file or directory tree."""
    if os.path.isfile(path):
        return os.path.getsize(path), 1, False

    total = 0
    files = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            if files >= _MAX_FILES_WALKED:
                return total, files, True
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            # conformance: ignore[E014] a file removed mid-walk is expected; skip it
            except OSError:
                continue
    return total, files, False


def _size_from_hook(input_data: Any) -> InputSize | None:
    """Call the Input's own ``sizing_input_bytes()``, if it defines one.

    The contract is deliberately minimal — return bytes, or ``None`` if unknown —
    so an app can reuse a number it already computed rather than being pushed into
    the SDK's file model.
    """
    hook = getattr(input_data, SIZING_HOOK, None)
    if not callable(hook):
        return None
    value = hook()
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _logger.debug("%s returned %r; expected a non-negative int", SIZING_HOOK, value)
        return None
    file_count = getattr(input_data, "sizing_input_file_count", None)
    return InputSize(
        bytes=value,
        file_count=file_count if isinstance(file_count, int) else 0,
        basis="hook",
    )
