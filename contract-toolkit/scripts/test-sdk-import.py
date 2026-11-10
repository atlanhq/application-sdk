"""Validate generated Python against the SDK.

Every generated _input.py and _e2e_*.py must import cleanly, and the SDK names
``src/SdkNames.pkl`` copies by hand must match the installed SDK.

Requires: pip install git+https://github.com/atlanhq/application-sdk.git@main
"""

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SDK_NAMES_PKL = REPO_ROOT / "src" / "SdkNames.pkl"

# Inputs.pkl's ClassName: the only names a nested ObjectField class can take.
_CLASS_NAME_RE = re.compile(r"[A-Z][A-Za-z0-9]*")


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
        logger.warning("Failed to import module %s: %s", path, e, exc_info=True)
        return None, f"import error: {e}"


def test_input(path: Path) -> bool:
    """Verify _input.py imports and exposes AppInputContract."""
    mod, err = _load_module(path)
    if mod is None:
        logger.error("FAIL: %s — %s", path, err)
        return False
    if not hasattr(mod, "AppInputContract"):
        logger.error("FAIL: %s — AppInputContract class not found", path)
        return False
    cls = mod.AppInputContract
    if not hasattr(cls, "model_fields"):
        logger.error("FAIL: %s — AppInputContract is not a Pydantic model", path)
        return False
    logger.info("  OK: %s (%d fields)", path, len(cls.model_fields))
    return True


_E2E_CLASS_SUFFIX = {
    "_e2e_base": "GeneratedE2EBase",
    "_e2e_credential": "CredentialBody",
    "_e2e_substitutions": "MustacheSubstitutions",
}


def test_e2e(path: Path) -> bool:
    """Verify an _e2e_*.py file imports and exposes the expected generated class."""
    mod, err = _load_module(path)
    if mod is None:
        logger.error("FAIL: %s — %s", path, err)
        return False

    suffix = _E2E_CLASS_SUFFIX.get(path.stem)
    if suffix:
        # Prefer the longest name (most-specific subclass) so we don't accidentally
        # pick up the imported base class (e.g. CredentialBody itself).
        matches = sorted(
            [n for n in dir(mod) if n.endswith(suffix) and not n.startswith("_")],
            key=len,
            reverse=True,
        )
        if not matches:
            logger.error("FAIL: %s — no class ending in '%s' found", path, suffix)
            return False
        cls = getattr(mod, matches[0])
        if path.stem == "_e2e_base":
            # BaseE2ETest subclasses are not Pydantic models; verify required harness attrs.
            for attr in (
                "connector_short_name",
                "argo_package_name",
                "argo_template_name",
            ):
                if not hasattr(cls, attr):
                    logger.error(
                        "FAIL: %s — %s missing required attr '%s'",
                        path,
                        matches[0],
                        attr,
                    )
                    return False
            logger.info("  OK: %s (%s)", path, matches[0])
        else:
            if not hasattr(cls, "model_fields"):
                logger.error("FAIL: %s — %s is not a Pydantic model", path, matches[0])
                return False
            logger.info(
                "  OK: %s (%s, %d fields)", path, matches[0], len(cls.model_fields)
            )
    else:
        logger.info("  OK: %s", path)
    return True


def _imported_class_names(path: Path) -> set[str]:
    """Class-shaped names a generated module imports at module level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if _CLASS_NAME_RE.fullmatch(name):
                    names.add(name)
    return names


def _diff(label: str, pkl: set[str], sdk: set[str]) -> list[str]:
    errors = []
    if missing := sorted(sdk - pkl):
        errors.append(f"{label}: SDK declares {missing}, SdkNames.pkl does not")
    if extra := sorted(pkl - sdk):
        errors.append(f"{label}: SdkNames.pkl lists {extra}, the SDK does not declare")
    return errors


def check_sdk_names(input_files: list[Path]) -> list[str]:
    """Compare SdkNames.pkl's hand-copied names with the installed SDK.

    App.pkl skips a generated field the base class already declares and refuses
    a nested class that would shadow a module-level name of ``_input.py``. A
    stale set does not break the import test: the generated field silently
    shadows the SDK's (losing its type and validators), or a nested class
    replaces an imported name.
    """
    from application_sdk.contracts.base import Input
    from application_sdk.templates.contracts import (
        ExtractionInput,
        TreeSelectionExtractionInput,
    )

    result = subprocess.run(
        ["pkl", "eval", "-f", "json", str(SDK_NAMES_PKL)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [f"pkl eval {SDK_NAMES_PKL} failed: {result.stderr.strip()}"]
    names = json.loads(result.stdout)
    extraction = set(names["extractionInputFields"])
    module_names = set(names["inputModuleNames"])

    errors = _diff(
        "inputFields vs Input", set(names["inputFields"]), set(Input.model_fields)
    )
    errors += _diff(
        "extractionInputFields vs ExtractionInput",
        extraction,
        set(ExtractionInput.model_fields),
    )
    errors += _diff(
        "extractionInputFields vs TreeSelectionExtractionInput",
        extraction,
        set(TreeSelectionExtractionInput.model_fields),
    )
    if "AppInputContract" not in module_names:
        errors.append("inputModuleNames: missing AppInputContract")
    for path in input_files:
        if unlisted := sorted(_imported_class_names(path) - module_names):
            errors.append(
                f"inputModuleNames: {path.relative_to(REPO_ROOT)} imports {unlisted}, "
                "which a nested ObjectField class of the same name would shadow"
            )
    return errors


def main() -> int:
    failed = 0

    input_files = sorted(EXAMPLES_DIR.rglob("_input.py"))
    if not input_files:
        logger.error("ERROR: no _input.py files found")
        return 1
    logger.info(":: Testing %d _input.py files against SDK...", len(input_files))
    for path in input_files:
        if not test_input(path):
            failed += 1

    e2e_files = sorted(p for p in EXAMPLES_DIR.rglob("_e2e_*.py"))
    if e2e_files:
        logger.info(":: Testing %d _e2e_*.py files against SDK...", len(e2e_files))
        for path in e2e_files:
            if not test_e2e(path):
                failed += 1

    logger.info(":: Comparing SdkNames.pkl with the SDK...")
    name_errors = check_sdk_names(input_files)
    for err in name_errors:
        logger.error("FAIL: %s", err)
    if not name_errors:
        logger.info("  OK: SdkNames.pkl matches the SDK")

    total = len(input_files) + len(e2e_files)
    if failed or name_errors:
        logger.error(
            "%d/%d files failed import test; %d SdkNames.pkl mismatch(es).",
            failed,
            total,
            len(name_errors),
        )
        return 1

    logger.info("All %d files imported successfully.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
