"""Enforce that DataframeType.daft is not introduced into new SDK code.

The daft enum value is a deprecated shim (removal: v4.0). These are the only
SDK source files that are allowed to reference it:
- common/types.py          — the definition itself
- storage/formats/__init__.py — the write / write_batches compat branch
- storage/formats/json.py     — JsonFileWriter constructor shim
- storage/formats/parquet.py  — ParquetFileReader / ParquetFileWriter constructor shims

Any new reference outside this allowlist is a regression.
"""

from __future__ import annotations

import re
from pathlib import Path

_SDK_ROOT = Path(__file__).parents[3] / "application_sdk"

_ALLOWED_RELATIVE = frozenset(
    [
        "common/types.py",
        "storage/formats/__init__.py",
        "storage/formats/json.py",
        "storage/formats/parquet.py",
    ]
)

_PATTERN = re.compile(r"\bDataframeType\.daft\b")


def test_dataframe_type_daft_not_used_outside_shim_allowlist() -> None:
    """No SDK source file outside the allowlist may reference DataframeType.daft."""
    violations: list[str] = []
    for py_file in sorted(_SDK_ROOT.rglob("*.py")):
        rel = py_file.relative_to(_SDK_ROOT).as_posix()
        if rel in _ALLOWED_RELATIVE:
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PATTERN.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, (
        "DataframeType.daft found outside the shim allowlist — "
        "do not add new uses (removal: v4.0):\n" + "\n".join(violations)
    )
