"""Validate a scorecard document against the vendored JSON Schema.

The :class:`~conformance.scorecard.schema.Scorecard` pydantic model is the
source of truth; ``scorecard-schema-1.0.json`` is generated from it and vendored
so external consumers (connector-pulse, ad-hoc tooling) can validate the wire
artifact without importing pydantic.  A freshness test asserts the vendored file
still matches the model — the same generated-artifact discipline the conformance
suite uses for its rule docs and ledgers.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).parent / "scorecard-schema-1.0.json"


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def scorecard_json_schema() -> dict[str, Any]:
    """Return the JSON Schema derived from the :class:`Scorecard` model.

    This is the authoritative definition; the vendored file is a snapshot of it.
    """
    from conformance.scorecard.schema import Scorecard

    return Scorecard.model_json_schema(by_alias=True)


def validate_scorecard(doc_or_model: Any) -> None:
    """Validate *doc_or_model* against the vendored scorecard JSON Schema.

    Parameters
    ----------
    doc_or_model:
        A :class:`Scorecard` instance or a plain ``dict``
        (``model_dump(by_alias=True, exclude_none=True)``).

    Raises
    ------
    ImportError
        If ``jsonschema`` is not installed.
    jsonschema.ValidationError
        If the document does not conform to the schema.
    """
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - jsonschema is a core dep
        raise ImportError(
            "jsonschema is required to validate scorecard documents."
        ) from exc

    if hasattr(doc_or_model, "model_dump"):
        doc = doc_or_model.model_dump(by_alias=True, exclude_none=True)
    else:
        doc = doc_or_model

    jsonschema.validate(instance=doc, schema=_load_schema())
