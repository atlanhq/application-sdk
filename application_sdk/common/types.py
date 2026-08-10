"""Shared enums, and the convention for deprecating an individual enum member.

An enum *member* cannot carry ``@deprecated``: the decorator applies to a
function or class, and a member is an assignment inside a class body. Marking
one in a comment leaves it invisible to ``gen-deprecations``, which reads
machine-readable markers only — so the symbol ends up hand-coded into a
conformance checker instead, and the generated manifest stops being the single
source of truth it is gated on being.

The convention is a ``__deprecated_members__`` mapping in the enum's class body,
from member name to the deprecation notice::

    class Codec(Enum):
        __deprecated_members__ = {
            "legacy": "Codec.legacy is deprecated; use Codec.modern — "
            "will be removed in v5.0.0.",
        }

        modern = "modern"
        legacy = "legacy"

The name is a dunder, so ``EnumMeta`` treats it as class metadata rather than a
member, and the shared B-series extractor reads it out of the class body the
same way it reads a ``@deprecated`` message. The notice text is subject to the
same authoring rules as every other one (B002: name a migration target and a
removal version; B003: the removal must not already be overdue).

Runtime signal: it belongs at the point the SDK *acts* on the value, not on
member access. A warning on attribute access would fire on the SDK's own
dispatch (``if self.dataframe_type == DataframeType.daft``) on every read and
write, burying the caller's signal in noise from code the caller does not own.
Every entry point that accepts a ``DataframeType`` already emits a
``DeprecationWarning`` when it is handed the deprecated member — see
``storage/formats/{__init__,json,parquet}.py``.
"""

from enum import Enum


class DataframeType(Enum):
    """Enumeration of dataframe types."""

    __deprecated_members__ = {
        "daft": (
            "DataframeType.daft is deprecated; use DataframeType.pandas instead — "
            "will be removed in v4.0.0. It routes to the pandas/pyarrow path."
        ),
    }

    pandas = "pandas"
    daft = "daft"
