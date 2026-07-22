"""Shared mustache-substitution walker for the testing harnesses.

Both the e2e / full-DAG manifest harness (``application_sdk.testing.e2e.base``)
and the SDR harness (``application_sdk.testing.sdr.base``) substitute exact-match
``{{...}}`` placeholders inside a manifest's nested ``extract`` args. The walker
lives here so the two harnesses share one implementation (no drift) and the SDR
harness need not depend on the AE-publish base class.
"""

from __future__ import annotations

import re
from typing import Any

#: A whole-value mustache placeholder (no internal braces) — matches the
#: exact-match notion :func:`apply_mustache_subs` substitutes on, so a value like
#: ``"{{a}}-{{b}}"`` is NOT mistaken for a single placeholder.
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")


def apply_mustache_subs(obj: Any, subs: dict[str, Any]) -> Any:
    """Recursively replace exact-match ``{{...}}`` strings with ``subs`` values.

    Only a string that is *exactly* a single ``{{key}}`` (and present in
    ``subs``) is replaced; partial/compound values like ``"{{a}}-{{b}}"`` are
    left untouched.
    """
    if isinstance(obj, dict):
        return {k: apply_mustache_subs(v, subs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [apply_mustache_subs(x, subs) for x in obj]
    if isinstance(obj, str) and obj in subs:
        return subs[obj]
    return obj
