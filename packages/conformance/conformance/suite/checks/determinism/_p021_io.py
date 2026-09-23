"""P021 — SideEffectIoInWorkflow.

Flags a curated, high-signal set of side-effecting I/O calls inside workflow-context
methods of an ``App`` subclass: file access, network calls, environment reads,
spawning threads/processes, SDK object-store calls, and P023's data-scale
inventory.  Workflow code is replayed deterministically and must not touch the
outside world — I/O belongs in a ``@task`` activity.

Remediation is structural ("move it into a @task"), so findings are routed to
residue rather than auto-fixed.  The list is a deliberate high-signal subset, not
an exhaustive enumeration of every I/O API.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._p023_blocking_async import is_data_scale_io
from ._workflow_methods import resolve_call_target, workflow_method_nodes

RULE_ID = "P021"

# Module-prefixed I/O surfaces: any call under these roots is side-effecting.
_IO_PREFIXES = (
    "requests.",
    "httpx.",
    "urllib.request.",
    "http.client.",
    "socket.",
    "subprocess.",
    "threading.",
    "multiprocessing.",
    "concurrent.futures.",
    # Every public shutil entry point mutates or reads the filesystem. This is
    # also what makes P023's workflow-context skip a real dedup rather than a
    # hole: tree ops in workflow context are reported here, once.
    "shutil.",
)
# Exact callables (builtins / specific functions) that perform I/O or env reads.
_IO_EXACT = frozenset(
    {
        "open",
        "os.getenv",
        "os.system",
        "os.popen",
        "os.environ.get",
        "os.listdir",
    }
)
_IO_SUFFIXES = (".iterdir",)
_ENV_OBJECT = "os.environ"
_SDK_STORAGE_PREFIX = "application_sdk.storage."
_SDK_STORAGE_IO = frozenset(
    {
        "check_object_store_access",
        "check_run_storage_access",
        "delete",
        "delete_prefix",
        "download",
        "download_file",
        "download_file_chunked",
        "download_prefix",
        "exists",
        "fetch",
        "get_file_meta",
        "get_file_size",
        "list_data_keys",
        "list_data_keys_with_meta",
        "list_data_objects",
        "list_keys",
        "list_keys_with_meta",
        "materialize_file_reference",
        "materialize_file_refs",
        "persist_file_reference",
        "persist_file_refs",
        "put_json",
        "read_expected_digest",
        "sha256_file",
        "upload",
        "upload_file",
        "upload_file_from_bytes",
        "upload_prefix",
        "verify_object_store_access",
        "write_digest_sidecar",
    }
)

_HINT = (
    "Move it into a @task method — workflow code is replayed and must be "
    "deterministic, so file/network/env I/O belongs in an activity."
)


def _is_own_method(target: str) -> bool:
    return target.startswith("self.") and target.count(".") == 1


def _is_io_target(target: str) -> bool:
    return (
        target in _IO_EXACT
        or any(target.startswith(p) for p in _IO_PREFIXES)
        or (
            not _is_own_method(target)
            and (target.endswith(_IO_SUFFIXES) or is_data_scale_io(target))
        )
        or (
            target.startswith(_SDK_STORAGE_PREFIX)
            and target.rsplit(".", 1)[-1] in _SDK_STORAGE_IO
        )
    )


def _is_chained_io_target(target: str) -> bool:
    return target.endswith(_IO_SUFFIXES) or is_data_scale_io(target)


def check_p021(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P021 findings for side-effecting I/O in workflow context."""
    methods = workflow_method_nodes(tree)
    if not methods:
        return []
    bindings = collect_import_bindings(tree)
    findings: list[Finding] = []
    for method in methods:
        for node in ast.walk(method):
            target: str | None = None
            label: str | None = None
            if isinstance(node, ast.Call):
                target = resolve_call_target(node.func, bindings)
                if target is not None:
                    if _is_io_target(target):
                        label = f"{target}()"
                elif isinstance(node.func, ast.Attribute):
                    target = f"{ast.unparse(node.func.value)}.{node.func.attr}"
                    if _is_chained_io_target(target):
                        label = f"{target}()"
            elif isinstance(node, ast.Subscript):
                # os.environ["X"] read.
                if resolve_call_target(node.value, bindings) == _ENV_OBJECT:
                    target = _ENV_OBJECT
                    label = f"{_ENV_OBJECT}[...]"
            if label is None:
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=node,
                    message=f"Side-effecting I/O '{label}' in workflow context. {_HINT}",
                    directives=directives,
                )
            )
    return findings
