"""Shared helpers for the error-seam prescription rules (P043/P045, CONNECT-970).

The SDK's public error contract is ``application_sdk.errors.__all__``.  Every
other module that defines error classes is internal: it can be reorganised, and
the class a caller observes at a boundary can change, without a deprecation
cycle.  An app that builds control flow on such a class is depending on
something that was never a contract.

That is not hypothetical.  A connector app caught ``FormatReadError`` to tolerate
a legitimately-empty artifact prefix.  SDK 3.27.0 added an already-typed
pass-through above the wrapping clause in ``storage/formats/json.py``, so the app
began receiving a bare ``ObjectStoreReadError`` instead.  The two classes are
siblings, meeting only at ``AppError``, so the handler stopped matching, the
guard became dead code, and the failure escaped the activity on every retry.

Scope
-----
``COVERED_MODULE_PREFIX`` deliberately covers only ``application_sdk.storage.formats``
for now.  47 other SDK modules define error classes and will be brought in once
the app-repo blast radius is measured and the classes apps legitimately need are
promoted.  Widening is a change to that one constant, not to the checkers.

Resolution is limited to **directly bound names** — the ``from X import Y`` form.
A bare ``import application_sdk.storage.formats.format_errors`` followed by
attribute access is not resolved.  Every real occurrence found across the app
fleet uses the direct form.
"""

from __future__ import annotations

import ast
import importlib.resources as _ir
import json
from functools import lru_cache
from pathlib import Path

from conformance.suite.checks._ast_common import safe_read_text

# Only this module tree is covered today; see the "Scope" note above.
COVERED_MODULE_PREFIX = "application_sdk.storage.formats."

# The one supported import path for SDK error classes.
PUBLIC_ERROR_MODULE = "application_sdk.errors"

# Committed JSON, relative to the ``conformance`` package root (ships in the
# wheel under ``conformance/data/`` — same mechanism as the deprecation manifest
# and the toolkit baseline).
_ALLOWLIST_RELPATH: tuple[str, ...] = ("data", "public_errors.json")

# The SDK's public error surface, relative to the SDK repo root.  Only read by
# the generator (SDK-dev time); never present in a consumer app repo.
SDK_ERRORS_INIT_RELPATH: tuple[str, ...] = ("application_sdk", "errors", "__init__.py")


def _allowlist_path() -> Path:
    return Path(str(_ir.files("conformance"))).joinpath(*_ALLOWLIST_RELPATH)


ALLOWLIST_PATH = _allowlist_path()


def build_allowlist(sdk_root: Path) -> tuple[str, ...]:
    """Parse the ``__all__`` of ``application_sdk/errors/__init__.py``.

    Returns the sorted subset whose names end in ``Error`` — the public error
    classes.  The rest of ``__all__`` (``Audience``, ``FailureDetails``, the
    legacy ``AAF-*`` constants) is not an error class and cannot appear in an
    ``except`` clause.
    """
    init_py = sdk_root.joinpath(*SDK_ERRORS_INIT_RELPATH)
    text = safe_read_text(init_py)
    if text is None:
        raise ValueError(
            f"{init_py} is unreadable or not valid UTF-8 — cannot build the "
            f"public error allowlist."
        )

    tree = ast.parse(text, filename=str(init_py))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        names = {
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
        return tuple(sorted(n for n in names if n.endswith("Error")))

    raise ValueError(f"{init_py} declares no literal __all__ — cannot build allowlist.")


def serialize(names: tuple[str, ...]) -> str:
    """Deterministic JSON so ``--check`` is a stable staleness gate."""
    return (
        json.dumps({"public_error_names": list(names)}, indent=2, sort_keys=True) + "\n"
    )


@lru_cache(maxsize=1)
def load_allowlist() -> frozenset[str]:
    """Load the committed allowlist, or an empty set when absent/unparseable.

    Returning empty (rather than raising) keeps the suite from crashing a
    consumer's CI if the baked data ever goes missing.  The allowlist only
    selects which remediation a finding suggests, never whether it fires, so an
    empty set degrades the message and nothing else.
    """
    try:
        data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    names = data.get("public_error_names")
    if not isinstance(names, list):
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str))


def covered_error_name(origin: str | None) -> str | None:
    """Return the class name when *origin* is an error class in a covered module.

    *origin* is a fully-qualified ``module.name`` string as produced by
    ``collect_import_origins``.  The ``Error`` suffix is what distinguishes an
    error class from the helper functions that share these modules — every one
    of the SDK's 184 error classes ends in ``Error``, and no other SDK class
    does, so ``convert_datetime_to_epoch`` and friends are correctly ignored.

    A name the public surface already exports returns ``None``: P045 alone owns
    the import-path migration for a promoted class, and P043's "not exported"
    claim would be false for it.  The drift guard keeps the allowlist current,
    so a class later removed from ``__all__`` resumes firing here.
    """
    if not origin or not origin.startswith(COVERED_MODULE_PREFIX):
        return None
    name = origin.rsplit(".", 1)[-1]
    if not name.endswith("Error"):
        return None
    # Promoted classes: P045 owns the import-path migration; P043's
    # "not exported" claim would be false for these.
    if name in load_allowlist():
        return None
    return name


def remediation(name: str) -> str:
    """The fix for *name*, which differs by whether a public equivalent exists."""
    if name in load_allowlist():
        return f"Import it from '{PUBLIC_ERROR_MODULE}' instead."
    return (
        f"Catch 'AppError' from '{PUBLIC_ERROR_MODULE}' and branch on '.code' "
        f"instead — error codes are wire contracts and are more stable than a "
        f"class's module location."
    )
