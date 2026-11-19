"""Every exception the SDK defines is an ``AppError``, bar a short allowlist.

A failure that is not an ``AppError`` has no category, code or audience, so the
failure envelope, the preflight gate and the HTTP boundaries all route it as an
untyped crash. Before FND-4298, 27 production exception classes sat outside the
hierarchy, and two of them meant the same thing as an ``AppError`` leaf.

The allowlist names *roots*: a class passes when an allowlisted root is in its
MRO. Each root says why it cannot be an ``AppError``. Adding one is a design
decision, not a way to land a new exception: re-parent the class under the
categorical leaf it belongs to instead (ADR-0013).

``application_sdk.testing`` is out of scope. Its exceptions are raised in a
pytest process, never in a worker or a handler, so they never reach a failure
envelope.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import application_sdk
from application_sdk.errors.base import AppError

_ROOT = Path(application_sdk.__file__).parent

#: Exception classes that are deliberately not ``AppError``, with the reason.
#: Their subclasses are covered too.
_ALLOWED_ROOTS: dict[str, str] = {
    "application_sdk.common.error_codes.AtlanError": (
        "the v2 error tree: deprecated, warns on construction, removed in v4.0"
    ),
    "application_sdk.execution.errors.ApplicationError": (
        "a subclass of Temporal's ApplicationError, for code that needs "
        "Temporal's own retry and error-type semantics; it cannot also be a "
        "dataclass AppError"
    ),
    "application_sdk.outputs.skipped.RecordSkipped": (
        "a skip signal from a sync mapper to its @task loop, recorded as a "
        "skipped record and never a failure"
    ),
    "application_sdk.execution._temporal.preflight_gate._TransientWarmupRaise": (
        "an internal signal the warmup wait absorbs; it never escapes"
    ),
    "application_sdk.infrastructure._dapr.http._ReplayableSecretAnswer": (
        "an internal signal that makes the HTTP transport replay a 500; it "
        "never escapes"
    ),
}

_OUT_OF_SCOPE_PACKAGE = "application_sdk.testing"


def _qualified(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _import_all_sdk_modules() -> None:
    # File-based, as in test_identity_classvars, so a broken package __init__
    # cannot hide the classes under it. A missing optional dependency is
    # tolerated (test_imports owns import health).
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        parts = (
            rel.parent.parts
            if path.name == "__init__.py"
            else rel.with_suffix("").parts
        )
        if not parts:
            continue
        try:
            importlib.import_module("application_sdk." + ".".join(parts))
        except ModuleNotFoundError:
            continue


def _sdk_exception_classes() -> list[type[BaseException]]:
    _import_all_sdk_modules()
    seen: set[type[BaseException]] = set()
    stack: list[type[BaseException]] = [BaseException]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return sorted(
        (
            c
            for c in seen
            if c.__module__.startswith("application_sdk.")
            and not c.__module__.startswith(_OUT_OF_SCOPE_PACKAGE)
        ),
        key=_qualified,
    )


_CLASSES = _sdk_exception_classes()


def _allowed(cls: type) -> bool:
    return any(_qualified(base) in _ALLOWED_ROOTS for base in cls.__mro__)


def test_the_scan_sees_the_sdk() -> None:
    """Anchor: an import failure must not shrink the scan to nothing."""
    names = {_qualified(c) for c in _CLASSES}
    assert "application_sdk.credentials.errors.CredentialParseError" in names
    assert "application_sdk.handler.request_contract.RequestContractError" in names
    assert len(_CLASSES) > 250


def test_every_sdk_exception_is_an_app_error() -> None:
    outside = [
        _qualified(c)
        for c in _CLASSES
        if not issubclass(c, AppError) and not _allowed(c)
    ]
    assert not outside, (
        "These exception classes are not AppError subclasses. Re-parent each "
        "under the categorical leaf it belongs to (ADR-0013), or, if it is a "
        "control-flow signal that never escapes, add it to _ALLOWED_ROOTS "
        "with the reason:\n  " + "\n  ".join(outside)
    )


@pytest.mark.parametrize("root", sorted(_ALLOWED_ROOTS))
def test_every_allowlisted_root_still_needs_its_entry(root: str) -> None:
    """A root that was removed or re-parented leaves a stale entry behind."""
    match = [c for c in _CLASSES if _qualified(c) == root]
    assert match, f"{root} no longer exists; drop it from _ALLOWED_ROOTS"
    assert not issubclass(match[0], AppError), (
        f"{root} is an AppError now; drop it from _ALLOWED_ROOTS"
    )
