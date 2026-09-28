#!/usr/bin/env python3
"""Generate the ``application_sdk`` re-export shims for code that lives in ``packages/api``.

The error taxonomy and the handler surface are defined once, in
``atlan-application-sdk-api``. Their old ``application_sdk`` paths are shims that
return the same objects. Each shim is *generated* from the api module's public
names and written with static literals — explicit imports, a literal
``__all__``, and (for the deprecated handler paths) a literal
``_DEPRECATED_CONSTANTS`` map served by ``__getattr__`` — so the Symbol Removal
Check can read the surface without importing anything.

Two kinds of shim:

* **first-class** (errors, credentials): explicit imports, no warning. Worker code
  keeps using these paths.
* **deprecated** (handler): handler-surface names resolve through
  ``_DEPRECATED_CONSTANTS`` with a ``DeprecationWarning`` (removal in v4.0).
  Worker-surface names that live on the same path (``PreflightGateMode``, the
  event-trigger configs, …) are plain imports and never warn.

Usage (from the repo root, in the SDK's venv)::

    uv run python .github/scripts/gen_api_shims.py          # rewrite the shims
    uv run python .github/scripts/gen_api_shims.py --check  # exit 1 if any is stale

``tests/unit/test_api_package_identity.py`` runs the check, so adding a public
name to the api package without regenerating fails CI.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Worker-surface names that live in handler.contracts but are not handler code.
WORKER_SURFACE = (
    "CloudEventEnvelope",
    "EventFilterRule",
    "EventTriggerConfig",
    "FileUploadResponse",
    "PreflightGateMode",
    "SubscriptionConfig",
)


@dataclass(frozen=True)
class Shim:
    path: str  # repo-relative file
    old: str  # dotted module path of the shim
    new: str  # dotted module path in the api package
    deprecated: bool = False
    #: Names this path always offered that live outside the api module, as
    #: ``name -> module``; imported explicitly and never deprecated.
    extra: dict[str, str] = field(default_factory=dict)


SHIMS: tuple[Shim, ...] = (
    Shim(
        "application_sdk/errors/__init__.py",
        "application_sdk.errors",
        "application_sdk_api.errors",
    ),
    Shim(
        "application_sdk/errors/base.py",
        "application_sdk.errors.base",
        "application_sdk_api.errors.base",
    ),
    Shim(
        "application_sdk/errors/categories.py",
        "application_sdk.errors.categories",
        "application_sdk_api.errors.categories",
    ),
    Shim(
        "application_sdk/errors/leaves.py",
        "application_sdk.errors.leaves",
        "application_sdk_api.errors.leaves",
    ),
    Shim(
        "application_sdk/errors/wire.py",
        "application_sdk.errors.wire",
        "application_sdk_api.errors.wire",
    ),
    Shim(
        "application_sdk/credentials/errors.py",
        "application_sdk.credentials.errors",
        "application_sdk_api.credentials.errors",
    ),
    Shim(
        "application_sdk/credentials/spec.py",
        "application_sdk.credentials.spec",
        "application_sdk_api.credentials.spec",
    ),
    Shim(
        "application_sdk/credentials/ingress.py",
        "application_sdk.credentials.ingress",
        "application_sdk_api.credentials.ingress",
    ),
    Shim(
        "application_sdk/handler/__init__.py",
        "application_sdk.handler",
        "application_sdk_api.handler",
        deprecated=True,
        extra={
            "create_app_handler_service": "application_sdk.handler.service",
            "run_app_handler_service": "application_sdk.handler.service",
        },
    ),
    Shim(
        "application_sdk/handler/base.py",
        "application_sdk.handler.base",
        "application_sdk_api.handler.base",
        deprecated=True,
    ),
    Shim(
        "application_sdk/handler/contracts.py",
        "application_sdk.handler.contracts",
        "application_sdk_api.handler.contracts",
        deprecated=True,
    ),
    Shim(
        "application_sdk/handler/context.py",
        "application_sdk.handler.context",
        "application_sdk_api.handler.context",
        deprecated=True,
        extra={"bind_invocation_context": "application_sdk.handler.invocation"},
    ),
    Shim(
        "application_sdk/handler/manifest.py",
        "application_sdk.handler.manifest",
        "application_sdk_api.handler.manifest",
        deprecated=True,
    ),
    Shim(
        "application_sdk/handler/service_errors.py",
        "application_sdk.handler.service_errors",
        "application_sdk_api.handler.service_errors",
        deprecated=True,
    ),
)


def _isort_key(name: str) -> tuple[int, str]:
    """isort's default ``order_by_type``: CONSTANTS, then Classes, then the rest."""
    if name.isupper() and len(name) > 1:
        return (0, name)
    if name[:1].isupper():
        return (1, name)
    return (2, name)


def public_names(module_name: str) -> list[str]:
    """The public surface of ``module_name``, read the way the removal check reads it.

    Its ``__all__`` when it declares one; otherwise every public name the module
    defines at top level plus every name it re-exports from ``application_sdk_api``
    — never incidental stdlib / typing imports (``TYPE_CHECKING``, ``UTC``).
    """
    module = importlib.import_module(module_name)
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return sorted(set(declared), key=_isort_key)
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").startswith("application_sdk_api")
        ):
            names.update(a.asname or a.name for a in node.names if a.name != "*")
    return sorted((n for n in names if not n.startswith("_")), key=_isort_key)


def _import_block(module: str, names: list[str], note: str) -> str:
    if not names:
        return ""
    body = "".join(f"    {n},\n" for n in names)
    return f"from {module} import (  # noqa: F401 — {note}\n{body})\n"


def render(shim: Shim) -> str:
    names = public_names(shim.new)
    if not shim.deprecated:
        doc = (
            f'"""Re-export of :mod:`{shim.new}`.\n\n'
            "This code lives in the ``atlan-application-sdk-api`` package so the consolidated\n"
            f"API host and the worker share one implementation. ``{shim.old}`` is the\n"
            "SDK's first-class path to the same objects and is not deprecated.\n\n"
            "Generated by ``.github/scripts/gen_api_shims.py`` — do not edit. Make changes in\n"
            "``packages/api`` and regenerate; ``guard_api_shims.py`` fails CI on hand edits.\n"
            '"""\n'
        )
        out = [
            doc,
            "\nfrom __future__ import annotations\n\n",
            "from typing import Any as _Any\n\n",
        ]
        out.append(f"import {shim.new} as _src\n")
        out.append(_import_block(shim.new, names, "re-exported, same objects"))
        for n, mod in sorted(shim.extra.items()):
            out.append(_import_block(mod, [n], "re-exported"))
        all_names = sorted(set(names) | set(shim.extra))
        out.append(
            "\n__all__ = [\n" + "".join(f'    "{n}",\n' for n in all_names) + "]\n"
        )
        out.append(
            "\n\ndef __getattr__(name: str) -> _Any:\n"
            "    # Private names are not re-exported above; resolve them from the source.\n"
            "    return getattr(_src, name)\n"
        )
        return "".join(out)

    worker = [n for n in names if n in WORKER_SURFACE]
    handler = [n for n in names if n not in WORKER_SURFACE]
    doc = (
        f'"""Deprecated alias of :mod:`{shim.new}` (removal in v4.0).\n\n'
        "The handler surface lives in the ``atlan-application-sdk-api`` package so the\n"
        "consolidated API host can serve an app's handler without this distribution.\n"
        f"Import from ``{shim.new}`` instead. Every name here resolves to the object\n"
        "in the api package, so behaviour is unchanged; handler names warn.\n\n"
        "Generated by ``.github/scripts/gen_api_shims.py`` — do not edit. Make changes in\n"
        "``packages/api`` and regenerate; ``guard_api_shims.py`` fails CI on hand edits.\n"
        '"""\n'
    )
    out = [doc, "\nfrom __future__ import annotations\n\n"]
    # isort sections: stdlib, then the api package (third-party to isort), then
    # application_sdk (first-party), each separated by one blank line.
    sections = [
        "import importlib as _importlib\nimport warnings as _warnings\n"
        "from typing import TYPE_CHECKING as _TYPE_CHECKING\nfrom typing import Any as _Any\n"
    ]
    if worker:
        sections.append(
            _import_block(shim.new, worker, "worker-surface, not deprecated")
        )
    by_module: dict[str, list[str]] = {}
    for n, mod in shim.extra.items():
        by_module.setdefault(mod, []).append(n)
    if by_module:
        sections.append(
            "".join(
                _import_block(
                    mod,
                    sorted(ns, key=_isort_key),
                    "not handler surface; not deprecated here",
                )
                for mod, ns in sorted(by_module.items())
            )
        )
    out.append("\n".join(sections))
    if handler:
        out.append(
            "\nif _TYPE_CHECKING:\n"
            f"    from {shim.new} import (  # noqa: F401\n"
            + "".join(f"        {n},\n" for n in handler)
            + "    )\n"
        )
    out.append(
        "\n#: Handler-surface names, served with a DeprecationWarning (removal in v4.0).\n"
        "_DEPRECATED_CONSTANTS: dict[str, str] = {\n"
        + "".join(f'    "{n}": "{shim.new}.{n}",\n' for n in handler)
        + "}\n"
    )
    all_names = sorted(set(names) | set(shim.extra))
    out.append("\n__all__ = [\n" + "".join(f'    "{n}",\n' for n in all_names) + "]\n")
    out.append(
        f"""

def __getattr__(name: str) -> _Any:
    target = _DEPRECATED_CONSTANTS.get(name)
    if target is None:
        raise AttributeError(f"module {{__name__!r}} has no attribute {{name!r}}")
    module, _, attr = target.rpartition(".")
    _warnings.warn(
        f"{shim.old}.{{name}} is deprecated and will be removed in v4.0; "
        f"import it from {{module}}",
        DeprecationWarning,
        stacklevel=2,
    )
    value = getattr(_importlib.import_module(module), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
"""
    )
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if any shim is stale"
    )
    args = parser.parse_args(argv)
    stale = []
    for shim in SHIMS:
        path = args.root / shim.path
        want = render(shim)
        have = path.read_text(encoding="utf-8") if path.exists() else ""
        if have != want:
            stale.append(shim.path)
            if not args.check:
                path.write_text(want, encoding="utf-8")
    if args.check and stale:
        print("Stale application_sdk shims (run .github/scripts/gen_api_shims.py):")
        for p in stale:
            print(f"  {p}")
        return 1
    print(f"gen_api_shims: {len(SHIMS)} shims {'checked' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
