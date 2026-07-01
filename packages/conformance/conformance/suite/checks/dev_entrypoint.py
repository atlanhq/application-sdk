"""T004 DevEntrypointRequiresAppModule — root main.py must not require ATLAN_APP_MODULE.

``application_sdk.main.main()`` is the production, env/CLI-driven launcher:
internally it always calls ``AppConfig.from_args_and_env(args)``, which raises
``MissingAppModuleError`` unless ``ATLAN_APP_MODULE`` (or ``--app``) is set.
That is correct in production — the base image's own CMD sets the env var and
never even executes the repo's ``main.py`` — but ``main.py`` is also what CI's
``connector-integration-tests`` composite action runs directly
(``python main.py``) to boot the app for local/dev-mode testing. The
bootstrapped ``tests-reusable.yaml`` path exposes no input to inject
``ATLAN_APP_MODULE`` into that job, so a root ``main.py`` that delegates
straight to ``application_sdk.main.main()`` fails every PR with
``MissingAppModuleError`` / "App server failed to start within 60s" (BLDX-1520).

The proven fix — already used by ``atlan-metabase-app``, ``atlan-openapi-app``,
``atlan-mysql-app`` — is for ``main.py`` to delegate to a local dev entrypoint
(conventionally ``app/run_dev.py``) that constructs the ``App`` subclass
directly and calls ``run_dev_combined(MyApp, ...)``: no env var required.

Discovery
---------
Scans ``main.py`` at the repo root only. If absent, T004 no-ops — a missing
entrypoint is a different concern (out of scope for this rule).

Inline suppression
-------------------
Add ``# conformance: ignore[T004] <reason>`` on the offending call's line (or
the comment-only line directly above it) — e.g. for a utility/CSA app that
genuinely has no local dev-mode boot path and relies on
``ATLAN_APP_MODULE`` being set out-of-band even for CI.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    collect_import_origins,
    make_cli_main,
    make_finding,
    qualify_chained_attr_call,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"

# The production, env/CLI-driven launcher. Any call resolving here means
# `python main.py` requires ATLAN_APP_MODULE/--app to boot.
_PRODUCTION_ENTRYPOINT_ORIGIN = "application_sdk.main.main"

_MESSAGE = (
    "Calls 'application_sdk.main.main()' directly from main.py — this is the "
    "production, ATLAN_APP_MODULE-driven launcher. main.py is also what CI's "
    "connector-integration-tests action runs directly ('python main.py') to "
    "boot the app for local/dev-mode testing, and the bootstrapped "
    "tests-reusable.yaml path has no way to inject ATLAN_APP_MODULE into that "
    "job — so this fails every PR with MissingAppModuleError. Delegate "
    "instead to a local dev entrypoint (conventionally app/run_dev.py) that "
    "constructs your App subclass directly and calls "
    "'run_dev_combined(MyApp, ...)' — see atlan-metabase-app, "
    "atlan-openapi-app, or atlan-mysql-app for the reference pattern. "
    "Suppress with '# conformance: ignore[T004] <reason>'."
)

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Discover the root ``main.py``.

    Returns an empty list when no root ``main.py`` is present, so T004
    simply no-ops on repos that have not yet added one (e.g. libraries).
    """
    main_py = root / "main.py"
    return [main_py] if main_py.is_file() else []


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single ``main.py`` source *text* for T004 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return _check_t004(tree, file, directives)


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single ``main.py`` file for T004 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def _module_level_rebind_lines(tree: ast.AST, name: str) -> list[int]:
    """Line numbers of top-level statements that rebind *name* to something else.

    Only module-scope ``def``/``class``/assignment targeting *name* count —
    they shadow an earlier import of the same name for any call that comes
    after them.  Nested rebindings (inside a function/class body) create a
    new local scope and don't affect module-level call sites, so they're
    deliberately not walked.
    """
    lines: list[int] = []
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name == name:
                lines.append(stmt.lineno)
        elif isinstance(stmt, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in stmt.targets
            ):
                lines.append(stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                lines.append(stmt.lineno)
    return lines


def _check_t004(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit T004 findings for calls resolving to application_sdk.main.main."""
    findings: list[Finding] = []
    origins = collect_import_origins(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # from application_sdk.main import main [as X]; X()
        if isinstance(func, ast.Name):
            if origins.get(func.id) == _PRODUCTION_ENTRYPOINT_ORIGIN and not any(
                rebind_line < node.lineno
                for rebind_line in _module_level_rebind_lines(tree, func.id)
            ):
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="T004",
                        node=node,
                        message=_MESSAGE,
                        directives=directives,
                    )
                )

        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                # import application_sdk.main as sdkmain; sdkmain.main()
                module_origin = origins.get(func.value.id, "")
                if f"{module_origin}.{func.attr}" == _PRODUCTION_ENTRYPOINT_ORIGIN:
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="T004",
                            node=node,
                            message=_MESSAGE,
                            directives=directives,
                        )
                    )
            else:
                # import application_sdk.main; application_sdk.main.main()
                qualified = qualify_chained_attr_call(func, origins)
                if qualified == _PRODUCTION_ENTRYPOINT_ORIGIN:
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="T004",
                            node=node,
                            message=_MESSAGE,
                            directives=directives,
                        )
                    )

    return findings


main = make_cli_main(
    scan_text,
    description="T004: scan root main.py for direct application_sdk.main.main() calls.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())
