"""Ship the SDK's contract ledger inside the wheel without a second committed copy.

The SDK keeps exactly one ledger, ``contract_schema.lock.json`` at the
repository root. B005 in a consumer app needs that ledger at runtime (to tell an
SDK-retired template field from an app-made removal), so the wheel must carry it
as ``conformance/data/contract_schema.lock.json``.

Two build paths reach this hook:

* a build from the repository checkout (``uv build`` in ``packages/conformance``,
  a ``git+...#subdirectory=packages/conformance`` install, or the root
  project's path dependency) reads the root ledger two directories up;
* a wheel built from the sdist reads the copy the sdist carries (the sdist's
  ``force-include`` in ``pyproject.toml`` puts it there).

Anything else fails the build: a wheel without the ledger silently disables the
SDK-retirement exemption for every consumer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

LEDGER_NAME = "contract_schema.lock.json"
PACKAGED_LEDGER = f"conformance/data/{LEDGER_NAME}"


class LedgerBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        if (root / "PKG-INFO").is_file():
            source = root / PACKAGED_LEDGER
        else:
            source = root.parent.parent / LEDGER_NAME
        if not source.is_file():
            raise FileNotFoundError(
                f"SDK contract ledger not found at {source}; the conformance "
                "wheel must ship the repository-root contract_schema.lock.json."
            )
        build_data["force_include"][str(source)] = PACKAGED_LEDGER
