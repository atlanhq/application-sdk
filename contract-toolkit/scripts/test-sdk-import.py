"""Validate that every generated _input.py imports cleanly against the SDK.

Requires: pip install git+https://github.com/atlanhq/application-sdk.git@main
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


def test_import(path: Path) -> bool:
    """Load a _input.py file as a module and verify AppInputContract exists."""
    module_name = f"test_{path.parent.name}_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            print(f"FAIL: {path} — could not create module spec")
            return False
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"FAIL: {path} — import error: {e}")
        return False

    if not hasattr(mod, "AppInputContract"):
        print(f"FAIL: {path} — AppInputContract class not found")
        return False

    # Verify it's a proper Pydantic model with model_fields
    cls = mod.AppInputContract
    if not hasattr(cls, "model_fields"):
        print(f"FAIL: {path} — AppInputContract is not a Pydantic model")
        return False

    print(f"  OK: {path} ({len(cls.model_fields)} fields)")
    return True


def main() -> int:
    input_files = sorted(EXAMPLES_DIR.rglob("_input.py"))
    if not input_files:
        print("ERROR: no _input.py files found")
        return 1

    print(f":: Testing {len(input_files)} _input.py files against SDK...")
    failed = 0
    for path in input_files:
        if not test_import(path):
            failed += 1

    print()
    if failed:
        print(f"{failed}/{len(input_files)} files failed import test.")
        return 1

    print(f"All {len(input_files)} files imported successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
