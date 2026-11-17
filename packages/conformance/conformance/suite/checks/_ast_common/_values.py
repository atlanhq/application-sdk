"""Walks over parsed JSON/TOML values."""

from __future__ import annotations

from collections.abc import Iterator

__all__ = ["iter_strings"]


def iter_strings(value: object) -> Iterator[str]:
    """Recursively yield every string leaf under a parsed JSON/TOML *value*.

    Descends dict values and list items; every other leaf type is skipped.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():  # pyright: ignore[reportUnknownVariableType]
            yield from iter_strings(v)  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(value, list):
        for v in value:  # pyright: ignore[reportUnknownVariableType]
            yield from iter_strings(v)  # pyright: ignore[reportUnknownArgumentType]
