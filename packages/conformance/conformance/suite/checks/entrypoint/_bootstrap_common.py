"""Shared helpers for the entrypoint-conformance checks (P017–P018, BLDX-1411)."""

from __future__ import annotations

import ast


def collect_import_origins(tree: ast.AST) -> dict[str, str]:
    """Map each bound name to its fully-qualified import origin (module.name).

    Walks the entire tree so lazy / in-function imports are caught.

    Examples::

        from application_sdk.execution import create_worker
        -> {"create_worker": "application_sdk.execution.create_worker"}

        from fastapi import FastAPI
        -> {"FastAPI": "fastapi.FastAPI"}

        import uvicorn
        -> {"uvicorn": "uvicorn"}

        from uvicorn import run
        -> {"run": "uvicorn.run"}
    """
    origins: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname if alias.asname else alias.name.split(".")[0]
                origins[bound] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:  # skip relative imports
                continue
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname if alias.asname else alias.name
                origins[bound] = f"{module}.{alias.name}" if module else alias.name
    return origins


def is_app_receiver(name: str, origin: str) -> bool:
    """True if the call receiver is plausibly an Atlan App or SDK object.

    Receiver restriction reduces false positives for lifecycle-method checks:
    Temporal's ``Client.start_workflow`` and stdlib ``asyncio.start_server``
    share the same method names but are not app-lifecycle calls.
    """
    return name in {"self", "app"} or origin.startswith("application_sdk")
