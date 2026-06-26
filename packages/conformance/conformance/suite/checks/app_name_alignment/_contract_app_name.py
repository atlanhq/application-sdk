"""Read app name from committed contract artifacts and the ``.env.example`` file.

Two artifact sources are read, both with regex (no PyYAML dependency — matching
the conformance package's established pattern in ``checks/actions_pinning.py`` and
``checks/bootstrap_drift.py``):

Contract name (primary)
    ``atlan.yaml`` at the repo root, matched by the column-0-anchored pattern
    ``^name:\\s*(\\S+)`` (ignores the indented ``- name:`` lines under
    ``entrypoints:``).

Contract name (fallback)
    ``app/generated/manifest.json`` (single-EP root) or
    ``app/generated/<first-sorted-subdir>/manifest.json`` (multi-EP), read via
    stdlib ``json``.  Used only when ``atlan.yaml`` is absent.  This fallback is
    skipped when multiple EP subdirs with different ``app_name`` values are found
    (bundle scenario — no reliable single reference).

Env name
    ``ATLAN_APPLICATION_NAME=<value>`` in ``.env.example``, matched by
    ``^ATLAN_APPLICATION_NAME\\s*=\\s*(\\S+)``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_ATLAN_YAML_NAME_RE = re.compile(r"^name:\s*(\S+)", re.MULTILINE)
_ENV_APP_NAME_RE = re.compile(r"^ATLAN_APPLICATION_NAME\s*=\s*(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class ContractAppNameScan:
    """App name values read from committed contract artifacts."""

    contract_name: str | None
    """Name from ``atlan.yaml`` ``name:`` (or ``manifest.json`` fallback).
    ``None`` when no contract artifact is present."""

    contract_source: str | None
    """Repo-relative path to the file the contract name was read from, for
    human-readable finding messages."""

    env_name: str | None
    """Value of ``ATLAN_APPLICATION_NAME`` in ``.env.example``.
    ``None`` when no ``.env.example`` is present or the variable is absent."""

    env_line: int | None
    """1-based line number of the ``ATLAN_APPLICATION_NAME=`` entry in
    ``.env.example``, for finding anchoring."""


def _read_atlan_yaml(root: Path) -> tuple[str | None, str | None]:
    """Read the top-level ``name:`` from ``atlan.yaml``.

    Returns ``(name, source_path)`` or ``(None, None)`` when absent.
    """
    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.is_file():
        return None, None
    try:
        text = atlan_yaml.read_text(encoding="utf-8")
    except OSError:
        return None, None
    m = _ATLAN_YAML_NAME_RE.search(text)
    if m:
        return m.group(1), "atlan.yaml"
    return None, None


def _read_manifest_fallback(root: Path) -> tuple[str | None, str | None]:
    """Read ``app_name`` from ``app/generated/`` manifest(s) as a fallback.

    Returns ``(name, source_path)`` or ``(None, None)`` when:
    * ``app/generated/`` is absent.
    * No ``manifest.json`` is found.
    * Multiple EP subdirs have different ``app_name`` values (bundle → ambiguous).
    """
    generated = root / "app" / "generated"
    if not generated.is_dir():
        return None, None

    # Single-EP: root-level manifest.json
    root_manifest = generated / "manifest.json"
    if root_manifest.is_file():
        name = _app_name_from_manifest(root_manifest)
        if name:
            return name, "app/generated/manifest.json"
        return None, None

    # Multi-EP: gather app_name values from each subdir's manifest
    ep_names: dict[str, str] = {}
    for child in sorted(generated.iterdir()):
        if not child.is_dir():
            continue
        m = child / "manifest.json"
        if m.is_file():
            name = _app_name_from_manifest(m)
            if name:
                ep_names[child.name] = name

    if not ep_names:
        return None, None

    unique_names = set(ep_names.values())
    if len(unique_names) == 1:
        ep_dir = next(iter(ep_names))
        return unique_names.pop(), f"app/generated/{ep_dir}/manifest.json"

    # Multiple different app_name values — bundle; can't pick one reference.
    return None, None


def _app_name_from_manifest(manifest_path: Path) -> str | None:
    """Extract the ``app_name`` value from a ``manifest.json`` file.

    The field is nested inside the first ``dag`` activity dict, e.g.::

        {"dag": {"extract": {"app_name": "mssql", ...}, ...}}
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dag = data.get("dag", {})
    for activity in dag.values():
        if isinstance(activity, dict) and "app_name" in activity:
            value = activity["app_name"]
            if isinstance(value, str) and value:
                return value
    return None


def _read_env_example(root: Path) -> tuple[str | None, int | None]:
    """Read ``ATLAN_APPLICATION_NAME`` from ``.env.example``.

    Returns ``(value, line_number)`` or ``(None, None)`` when absent or unset.
    """
    env_file = root / ".env.example"
    if not env_file.is_file():
        return None, None
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return None, None
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _ENV_APP_NAME_RE.match(line)
        if m:
            return m.group(1), lineno
    return None, None


def scan_contract(root: Path) -> ContractAppNameScan:
    """Read the contract and env app names from the repo at *root*.

    Parameters
    ----------
    root:
        Repo root directory.

    Returns
    -------
    :class:`ContractAppNameScan`
        Populated with whatever was found; ``None`` fields indicate absence.
    """
    # Contract name — atlan.yaml first, then manifest fallback
    contract_name, contract_source = _read_atlan_yaml(root)
    if contract_name is None:
        contract_name, contract_source = _read_manifest_fallback(root)

    # Env name
    env_name, env_line = _read_env_example(root)

    return ContractAppNameScan(
        contract_name=contract_name,
        contract_source=contract_source,
        env_name=env_name,
        env_line=env_line,
    )
