"""Validate a SARIF document against the vendored schema.

``jsonschema`` is a ``conformance`` dependency-group dep — it is not available
in production runtime.  This module is therefore only usable in conformance
test contexts (``uv sync --group conformance``).

Example::

    from conformance.suite.schema import ReportBuilder, validate_sarif
    report = ReportBuilder("my-tool", "1.0.0").build()
    validate_sarif(report)   # raises jsonschema.ValidationError on mismatch
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# jsonschema is imported lazily so that importing suite.schema in a context
# without the conformance dep-group installed does not hard-fail at import
# time.  A clear error is raised when validate_sarif() is called.
_SCHEMA_PATH = Path(__file__).parent / "sarif-schema-2.1.0.json"


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_sarif(report_or_dict: Any) -> None:  # type: ignore[misc]
    """Validate *report_or_dict* against the vendored SARIF 2.1.0 JSON Schema.

    Parameters
    ----------
    report_or_dict:
        Either a :class:`~suite.schema.sarif.SarifReport` instance or a
        plain ``dict`` (e.g. the result of
        ``report.model_dump(by_alias=True, exclude_none=True)``).

    Raises
    ------
    ImportError
        If ``jsonschema`` is not installed (i.e. the ``conformance``
        dependency group is not synced).
    jsonschema.ValidationError
        If the document does not conform to SARIF 2.1.0.
    """
    try:
        import jsonschema
    except ImportError as exc:
        raise ImportError(
            "jsonschema is required to validate SARIF documents.  "
            "Install it with: uv sync --group conformance"
        ) from exc

    if hasattr(report_or_dict, "model_dump"):
        doc = report_or_dict.model_dump(by_alias=True, exclude_none=True)
    else:
        doc = report_or_dict

    jsonschema.validate(instance=doc, schema=_load_schema())
