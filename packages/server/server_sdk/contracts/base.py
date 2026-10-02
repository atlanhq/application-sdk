"""Serializable enum base.

A str-backed enum that serializes to its value in JSON and Pydantic output.
"""

from __future__ import annotations

from enum import Enum


class SerializableEnum(str, Enum):
    """String enum whose value is used for (de)serialization."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)
