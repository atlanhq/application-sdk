"""Shared decorator-provenance helpers for P008, P013, P014, and the determinism
series (P020–P024).

Both ``_framework_transfer`` (P008) and ``_typed_boundaries`` (P013/P014) need
to gate on import provenance to distinguish the SDK's ``@task``/``@entrypoint``
from third-party decorators with the same local name (e.g. ``@celery.task``,
``@flask.entrypoint``).  The determinism checks (``suite.checks.determinism``,
P020–P024) consume the same helpers — plus :func:`is_interaction_decorator` and
the ``sdk``/``non_sdk_interaction_names`` provenance — to classify an ``App``
subclass's workflow-context methods.  This module centralises that logic so a
single change propagates to every consumer.

Provenance model
----------------
*  ``from celery import task`` — binds the name ``"task"`` to a non-SDK symbol;
   recorded in :attr:`ImportProvenance.non_sdk_task_names`.
*  ``import celery`` — introduces a non-SDK module alias; ``@celery.task``
   is excluded via :attr:`ImportProvenance.non_sdk_module_names`.
*  ``from application_sdk.contracts import FetchInput`` — records ``"FetchInput"``
   in :attr:`ImportProvenance.sdk_contract_names` (valid by provenance).
*  ``import application_sdk.contracts as c`` — records ``"c"`` in
   :attr:`ImportProvenance.sdk_contract_module_aliases` so that ``c.FetchInput``
   annotations are recognised as valid SDK contracts.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ._constants import _SDK_MODULE_PREFIX

_SDK_CONTRACT_MODULE_PREFIXES: tuple[str, ...] = (
    "application_sdk.contracts",
    "application_sdk.handler.contracts",
)


@dataclass(frozen=True)
class ImportProvenance:
    """Import-provenance sets needed for decorator and annotation gating."""

    non_sdk_task_names: frozenset[str]
    """Local names bound to ``task`` from a non-SDK ``from … import task``."""

    non_sdk_ep_names: frozenset[str]
    """Local names bound to ``entrypoint`` from a non-SDK ``from … import entrypoint``."""

    non_sdk_module_names: frozenset[str]
    """Local names of imported non-SDK top-level modules (``import celery`` → ``"celery"``)."""

    sdk_task_names: frozenset[str]
    """Local names bound to ``task`` from an SDK import (including aliases).

    Tracks ``from application_sdk.app import task as sdk_task`` → ``{"sdk_task"}``.
    Used by :func:`is_task_decorator` to recognise aliased SDK decorators.
    """

    sdk_ep_names: frozenset[str]
    """Local names bound to ``entrypoint`` from an SDK import (including aliases).

    Tracks ``from application_sdk.app import entrypoint as ep`` → ``{"ep"}``.
    """

    sdk_interaction_names: frozenset[str]
    """Local names bound to ``signal`` / ``query`` / ``update`` from an SDK import.

    Tracks ``from application_sdk.app import signal, query as q`` → ``{"signal", "q"}``.
    Used by :func:`is_interaction_decorator` to recognise the SDK's workflow-context
    runtime-interaction decorators (which run in the deterministic replay context).
    """

    non_sdk_interaction_names: frozenset[str]
    """Local names bound to ``signal`` / ``query`` / ``update`` from a *non*-SDK import."""

    sdk_contract_names: frozenset[str]
    """Local names imported from ``application_sdk.contracts.*`` — valid by provenance."""

    sdk_contract_module_aliases: frozenset[str]
    """Local aliases for ``import application_sdk.contracts as c`` style imports."""


def collect_import_provenance(tree: ast.AST) -> ImportProvenance:
    """Walk *tree* and return all import-provenance sets needed by P008/P013/P014."""
    non_sdk_task: set[str] = set()
    non_sdk_ep: set[str] = set()
    non_sdk_mods: set[str] = set()
    sdk_task: set[str] = set()
    sdk_ep: set[str] = set()
    sdk_interaction: set[str] = set()
    non_sdk_interaction: set[str] = set()
    sdk_contracts: set[str] = set()
    sdk_contract_mod_aliases: set[str] = set()

    _INTERACTION_NAMES = {"signal", "query", "update"}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            is_sdk = module == _SDK_MODULE_PREFIX or module.startswith(
                _SDK_MODULE_PREFIX + "."
            )
            if is_sdk:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "task":
                        sdk_task.add(local)
                    if alias.name == "entrypoint":
                        sdk_ep.add(local)
                    if alias.name in _INTERACTION_NAMES:
                        sdk_interaction.add(local)
            else:
                for alias in node.names:
                    if alias.name == "task":
                        non_sdk_task.add(alias.asname or alias.name)
                    if alias.name == "entrypoint":
                        non_sdk_ep.add(alias.asname or alias.name)
                    if alias.name in _INTERACTION_NAMES:
                        non_sdk_interaction.add(alias.asname or alias.name)
            # SDK contract provenance from `from application_sdk.contracts import X`
            if is_sdk and any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _SDK_CONTRACT_MODULE_PREFIXES
            ):
                for alias in node.names:
                    sdk_contracts.add(alias.asname or alias.name)

        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                is_sdk = name == _SDK_MODULE_PREFIX or name.startswith(
                    _SDK_MODULE_PREFIX + "."
                )
                if not is_sdk:
                    non_sdk_mods.add(alias.asname or name.split(".")[0])
                # `import application_sdk.contracts as c` style
                elif any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in _SDK_CONTRACT_MODULE_PREFIXES
                ):
                    sdk_contract_mod_aliases.add(alias.asname or name.split(".")[-1])

    return ImportProvenance(
        non_sdk_task_names=frozenset(non_sdk_task),
        non_sdk_ep_names=frozenset(non_sdk_ep),
        non_sdk_module_names=frozenset(non_sdk_mods),
        sdk_task_names=frozenset(sdk_task),
        sdk_ep_names=frozenset(sdk_ep),
        sdk_interaction_names=frozenset(sdk_interaction),
        non_sdk_interaction_names=frozenset(non_sdk_interaction),
        sdk_contract_names=frozenset(sdk_contracts),
        sdk_contract_module_aliases=frozenset(sdk_contract_mod_aliases),
    )


def is_task_decorator(dec: ast.expr, prov: ImportProvenance) -> bool:
    """True if *dec* is the SDK ``@task`` decorator (not a third-party one).

    Recognises both the canonical ``@task`` name and SDK aliases such as
    ``from application_sdk.app import task as sdk_task`` → ``@sdk_task``.
    """
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        # Positively confirmed SDK alias (from application_sdk.app import task as X)
        if dec.id in prov.sdk_task_names:
            return True
        # Bare 'task' name — assume SDK unless shadowed by a non-SDK import
        return dec.id == "task" and dec.id not in prov.non_sdk_task_names
    if isinstance(dec, ast.Attribute):
        return dec.attr == "task" and (
            not isinstance(dec.value, ast.Name)
            or dec.value.id not in prov.non_sdk_module_names
        )
    return False


def is_entrypoint_decorator(dec: ast.expr, prov: ImportProvenance) -> bool:
    """True if *dec* is the SDK ``@entrypoint`` decorator (not a third-party one).

    Recognises both the canonical ``@entrypoint`` name and SDK aliases such as
    ``from application_sdk.app import entrypoint as ep`` → ``@ep``.
    """
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        # Positively confirmed SDK alias
        if dec.id in prov.sdk_ep_names:
            return True
        # Bare 'entrypoint' name — assume SDK unless shadowed by a non-SDK import
        return dec.id == "entrypoint" and dec.id not in prov.non_sdk_ep_names
    if isinstance(dec, ast.Attribute):
        return dec.attr == "entrypoint" and (
            not isinstance(dec.value, ast.Name)
            or dec.value.id not in prov.non_sdk_module_names
        )
    return False


_INTERACTION_DECORATOR_NAMES = frozenset({"signal", "query", "update"})


def is_interaction_decorator(dec: ast.expr, prov: ImportProvenance) -> bool:
    """True if *dec* is an SDK ``@signal`` / ``@query`` / ``@update`` decorator.

    These declare workflow-context runtime interactions on an ``App`` subclass —
    they are relayed into the generated ``@workflow.defn`` class and therefore run
    in the deterministic replay context.  Recognises the canonical
    names, SDK aliases (``from application_sdk.app import query as q`` → ``@q``),
    and ``@workflow.signal`` style attribute access, while excluding third-party
    decorators that merely share a name (``@strawberry.field`` style is unaffected;
    a non-SDK ``from foo import query`` is not flagged).
    """
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        if dec.id in prov.sdk_interaction_names:
            return True
        # Bare name — assume SDK unless shadowed by a non-SDK import.
        return (
            dec.id in _INTERACTION_DECORATOR_NAMES
            and dec.id not in prov.non_sdk_interaction_names
        )
    if isinstance(dec, ast.Attribute):
        # e.g. @workflow.signal — attribute access through a module is the SDK/
        # Temporal seam; a non-SDK module alias (import celery; @celery.update)
        # is excluded.
        return dec.attr in _INTERACTION_DECORATOR_NAMES and (
            not isinstance(dec.value, ast.Name)
            or dec.value.id not in prov.non_sdk_module_names
        )
    return False
