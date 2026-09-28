"""The SDK's old import paths resolve to the api package's objects, not copies.

The error taxonomy and the handler surface live in ``atlan-application-sdk-api``
(``application_sdk_api``). Their ``application_sdk`` paths are shims, and the
guarantee that makes the worker and the consolidated API host behave the same is
object identity: ``application_sdk.errors.AuthError is
application_sdk_api.errors.AuthError``. A re-declared class would pass any test
that compares behaviour and still break ``isinstance`` and ``except`` across the
two paths, so identity is asserted name by name.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings

import pytest

#: (old path, new path, deprecated on the old path)
_PAIRS: tuple[tuple[str, str, bool], ...] = (
    ("application_sdk.errors", "application_sdk_api.errors", False),
    ("application_sdk.errors.base", "application_sdk_api.errors.base", False),
    (
        "application_sdk.errors.categories",
        "application_sdk_api.errors.categories",
        False,
    ),
    ("application_sdk.errors.leaves", "application_sdk_api.errors.leaves", False),
    ("application_sdk.errors.wire", "application_sdk_api.errors.wire", False),
    (
        "application_sdk.credentials.errors",
        "application_sdk_api.credentials.errors",
        False,
    ),
    ("application_sdk.credentials.spec", "application_sdk_api.credentials.spec", False),
    (
        "application_sdk.credentials.ingress",
        "application_sdk_api.credentials.ingress",
        False,
    ),
    ("application_sdk.handler", "application_sdk_api.handler", True),
    ("application_sdk.handler.base", "application_sdk_api.handler.base", True),
    (
        "application_sdk.handler.contracts",
        "application_sdk_api.handler.contracts",
        True,
    ),
    ("application_sdk.handler.context", "application_sdk_api.handler.context", True),
    ("application_sdk.handler.manifest", "application_sdk_api.handler.manifest", True),
    (
        "application_sdk.handler.service_errors",
        "application_sdk_api.handler.service_errors",
        True,
    ),
)

#: Worker-surface names that live in handler.contracts but are not handler code.
_WORKER_NAMES = frozenset(
    {
        "PreflightGateMode",
        "EventTriggerConfig",
        "EventFilterRule",
        "SubscriptionConfig",
        "CloudEventEnvelope",
        "FileUploadResponse",
        "create_app_handler_service",
        "run_app_handler_service",
        "bind_invocation_context",
    }
)


def _public_names(module: object) -> list[str]:
    names = getattr(module, "__all__", None)
    if names is None:
        names = [n for n in dir(module) if not n.startswith("_")]
    return sorted(names)


@pytest.mark.parametrize(
    ("old", "new", "deprecated"), _PAIRS, ids=[p[0] for p in _PAIRS]
)
def test_every_public_name_is_the_same_object(
    old: str, new: str, deprecated: bool
) -> None:
    old_mod = importlib.import_module(old)
    new_mod = importlib.import_module(new)
    mismatched = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for name in _public_names(new_mod):
            if getattr(old_mod, name) is not getattr(new_mod, name):
                mismatched.append(name)
    assert not mismatched, f"{old} re-declares instead of re-exporting: {mismatched}"


def test_isinstance_and_except_cross_both_paths() -> None:
    from application_sdk_api.errors import AuthError as NewAuth

    from application_sdk.errors import AppError as OldBase

    class _AppSpecific(NewAuth):
        pass

    err = _AppSpecific(message="denied")
    assert isinstance(err, OldBase)
    with pytest.raises(OldBase):
        raise err


@pytest.mark.parametrize(
    ("old", "new", "deprecated"), _PAIRS, ids=[p[0] for p in _PAIRS]
)
def test_only_the_handler_surface_warns(old: str, new: str, deprecated: bool) -> None:
    name = next(
        n for n in _public_names(importlib.import_module(new)) if n not in _WORKER_NAMES
    )
    module = importlib.import_module(old)
    module.__dict__.pop(name, None)  # drop a cached resolution so __getattr__ runs
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        getattr(module, name)
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert bool(deprecation) is deprecated, (
        old,
        name,
        [str(w.message) for w in caught],
    )


@pytest.mark.parametrize("name", sorted(_WORKER_NAMES - {"bind_invocation_context"}))
def test_worker_surface_names_do_not_warn(name: str) -> None:
    import application_sdk.handler as handler_pkg

    handler_pkg.__dict__.pop(name, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        getattr(handler_pkg, name)
    assert not [
        w
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "application_sdk.handler." in str(w.message)
    ]


def test_importing_errors_pulls_no_serving_or_worker_stack() -> None:
    """The workflow sandbox imports errors; it must not drag FastAPI or the server in."""
    code = (
        "import sys, application_sdk.errors, application_sdk_api.errors;"
        "print(sorted(m for m in ('fastapi', 'application_sdk_api.server', 'temporalio')"
        " if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]", out


def test_the_sdk_and_the_api_package_are_released_in_lockstep() -> None:
    """One version knob: the SDK pins the api package at its own version.

    The release job publishes both from the same commit (api first), and
    release.py moves all three together, so an SDK can never be installed next to
    a stale api. A hand-edit that desyncs them fails here, before any release.
    """
    import re
    import tomllib
    from pathlib import Path

    import application_sdk_api

    root = Path(__file__).resolve().parents[2]
    sdk = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    api = tomllib.loads((root / "packages/api/pyproject.toml").read_text())["project"]
    pins = [d for d in sdk["dependencies"] if d.startswith("atlan-application-sdk-api")]
    assert pins == [f"atlan-application-sdk-api=={sdk['version']}"], pins
    assert api["version"] == sdk["version"]
    assert application_sdk_api.__version__ == sdk["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", sdk["version"])
