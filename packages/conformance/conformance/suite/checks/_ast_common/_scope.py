"""Runtime scope detection — is this repo the SDK or a consumer app?

The conformance suite runs against two kinds of repo, and some rules only apply
to one of them (see :class:`~conformance.suite.schema.disposition.RuleScope`).
This module answers "which kind am I scanning?" so the runner can filter rules
to the surface they govern *without* the fleet having to configure anything: the
signal is the repo's own ``[project].name``.

This generalises into one mechanism what the D-series detector did inline (its
``_is_self_check`` package-name skip): the SDK and its sibling packages publish
the contracts that the APP-scoped rules enforce, so they are never subject to
them.

Detection is deliberately best-effort: a repo with no parseable
``pyproject.toml`` / no ``[project].name`` yields ``None``, which the runner
treats as "scope unknown — run every rule" (the pre-feature behaviour).  An
explicit ``--scope`` flag always overrides detection.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from conformance.suite.schema.disposition import RuleScope

# Distribution-name prefix that identifies the SDK and its sibling packages
# (``atlan-application-sdk``, ``atlan-application-sdk-conformance``, …).  Kept in
# sync with ``checks.dependency_conformance.SDK_PACKAGE``.
SDK_PACKAGE_PREFIX = "atlan-application-sdk"


def _normalise_name(name: str) -> str:
    """PEP 503 normalisation: lowercase + ``-``/``_``/``.`` collapsed to ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _project_name(pyproject_text: str) -> str | None:
    """Return ``[project].name`` from a pyproject.toml string, else ``None``."""
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    return str(name) if isinstance(name, str) else None


def is_sdk_package_name(name: str) -> bool:
    """True if *name* is the SDK distribution or a hyphen-extended sibling.

    Hyphen-anchored (after PEP 503 normalisation): matches
    ``atlan-application-sdk`` exactly and siblings like
    ``atlan-application-sdk-conformance``, but **not** a substring lookalike such
    as ``atlan-application-sdk2``.

    This is the single source of truth for "is this the SDK?" — both
    ``detect_scope`` (runner-side scope detection) and
    ``dependency_conformance._is_self_check`` (the D-series self-exemption) call
    it, so the two cannot drift apart on matching semantics.
    """
    norm = _normalise_name(name)
    prefix = _normalise_name(SDK_PACKAGE_PREFIX)
    return norm == prefix or norm.startswith(prefix + "-")


def detect_scope(root: Path) -> RuleScope | None:
    """Resolve the active :class:`RuleScope` for the repo rooted at *root*.

    Reads ``<root>/pyproject.toml`` and classifies by ``[project].name``:

    * SDK / sibling package (see :func:`is_sdk_package_name`) → :attr:`RuleScope.SDK`
    * any other name                                          → :attr:`RuleScope.APP`
    * no/unparseable pyproject, or no name                    → ``None`` (scope unknown)

    ``None`` means "do not filter" — the runner runs every rule, preserving the
    behaviour from before scope existed.
    """
    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    name = _project_name(text)
    if name is None:
        return None
    return RuleScope.SDK if is_sdk_package_name(name) else RuleScope.APP
