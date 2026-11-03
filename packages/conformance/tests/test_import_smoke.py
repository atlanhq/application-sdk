"""Every conformance module imports on its own; every CLI command runs (FND-4176).

0.51.0 shipped a circular import that only fired when
``_entrypoint_contract_fields`` (or anything importing it) was the first
conformance module loaded — which is what ``gen-contract-ledger`` does. The
rest of this suite imports modules in an order that never hits it, so it stayed
green. This test runs ``.github/scripts/import_smoke.py`` against this
environment: each module in a fresh ``python -I``, then every console script
the distribution declares and every subcommand with ``--help``.

The release workflows run the same script against the built wheel in a clean
venv (``.github/scripts/conformance_wheel_gate.py``); this copy is what puts
it in the PR checks.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / ".github/scripts/import_smoke.py"


def test_every_module_imports_first_and_every_command_runs() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--python",
            sys.executable,
            "--package",
            "conformance",
            "--dist",
            "atlan-application-sdk-conformance",
            "--commands-from",
            "conformance.cli:_COMMANDS",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
