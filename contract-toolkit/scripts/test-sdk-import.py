"""Validate that every generated _input.py and _e2e_*.py imports cleanly against the SDK.

Requires: pip install git+https://github.com/atlanhq/application-sdk.git@main
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


def _load_module(path: Path) -> tuple[object | None, str | None]:
    """Return (module, error_msg) pair."""
    module_name = f"test_{path.parent.name}_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None, "could not create module spec"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as e:
        return None, f"import error: {e}"


def test_input(path: Path) -> bool:
    """Verify _input.py imports and exposes AppInputContract."""
    mod, err = _load_module(path)
    if mod is None:
        print(f"FAIL: {path} — {err}")
        return False
    if not hasattr(mod, "AppInputContract"):
        print(f"FAIL: {path} — AppInputContract class not found")
        return False
    cls = mod.AppInputContract
    if not hasattr(cls, "model_fields"):
        print(f"FAIL: {path} — AppInputContract is not a Pydantic model")
        return False
    print(f"  OK: {path} ({len(cls.model_fields)} fields)")
    return True


def test_e2e(path: Path) -> bool:
    """Verify an _e2e_*.py file imports without error."""
    mod, err = _load_module(path)
    if mod is None:
        print(f"FAIL: {path} — {err}")
        return False
    print(f"  OK: {path}")
    return True


def main() -> int:
    failed = 0

    input_files = sorted(EXAMPLES_DIR.rglob("_input.py"))
    if not input_files:
        print("ERROR: no _input.py files found")
        return 1
    print(f":: Testing {len(input_files)} _input.py files against SDK...")
    for path in input_files:
        if not test_input(path):
            failed += 1

    e2e_files = sorted(p for p in EXAMPLES_DIR.rglob("_e2e_*.py"))
    if e2e_files:
        print(f"\n:: Testing {len(e2e_files)} _e2e_*.py files against SDK...")
        for path in e2e_files:
            if not test_e2e(path):
                failed += 1

    print()
    total = len(input_files) + len(e2e_files)
    if failed:
        print(f"{failed}/{total} files failed import test.")
        return 1

    print(f"All {total} files imported successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
