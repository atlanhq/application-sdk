"""Validate generated Python against the SDK.

Every generated _input.py and _e2e_*.py must import cleanly and sit in a
package (an ``__init__.py`` beside it), the SDK names ``src/SdkNames.pkl``
copies by hand must match the installed SDK, and every default in a generated
``_e2e_substitutions.py`` must pass the matching field of the ``_input.py``
generated beside it.

Importing ``_input.py`` also proves it is valid Python and that the SDK's
payload-safety check accepts it: ``Input.__init_subclass__`` refuses an
unbounded field such as ``dict[str, Any]`` unless the class opts out.

Runs against the SDK in this repository. From the repo root::

    uv run --extra workflows python contract-toolkit/scripts/test-sdk-import.py
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


def _generated_class(mod: object, suffix: str) -> type | None:
    """The most-specific public class in ``mod`` whose name ends in ``suffix``."""
    matches = sorted(
        (n for n in dir(mod) if n.endswith(suffix) and not n.startswith("_")),
        key=len,
        reverse=True,
    )
    return getattr(mod, matches[0]) if matches else None


# Known substitution/input disagreements, each tracked by an issue. An entry
# that stops failing is reported too, so the exemption is removed with the fix.
_KNOWN_DISAGREEMENTS: dict[tuple[str, str], str] = {
    # SqlTree: substitution `dict[str, Any] | None = None` vs input `dict[str, str]`.
    ("examples/full/app/generated/_e2e_substitutions.py", "schemas"): "FND-5019",
}


def check_substitution_round_trip(subs_path: Path) -> list[str]:
    """Validate each substitution default against the generated input field.

    App.pkl types a widget twice: ``widgetInputField`` for ``_input.py`` and
    ``getSubsFieldLine`` for ``_e2e_substitutions.py``. The e2e harness
    substitutes the dumped substitution default into the DAG arg, which the
    app then validates with its input contract, so a default the input field
    rejects fails every default e2e run. Only fields the generated subclass
    declares are checked; inherited SDK fields are not generated here.
    """
    from pydantic import BaseModel, TypeAdapter, ValidationError

    input_path = subs_path.with_name("_input.py")
    if not input_path.exists():
        return []
    subs_mod, err = _load_module(subs_path)
    input_mod, input_err = _load_module(input_path)
    if subs_mod is None or input_mod is None:
        return [f"{subs_path}: cannot load for round-trip ({err or input_err})"]
    subs_cls = _generated_class(subs_mod, "MustacheSubstitutions")
    input_cls = getattr(input_mod, "AppInputContract", None)
    if subs_cls is None or input_cls is None:
        return [f"{subs_path}: substitutions or AppInputContract class not found"]

    inherited: set[str] = set()
    for base in subs_cls.__mro__[1:]:
        if isinstance(base, type) and issubclass(base, BaseModel):
            inherited |= set(base.model_fields)

    rel = subs_path.relative_to(REPO_ROOT).as_posix()
    errors: list[str] = []
    for name, field in subs_cls.model_fields.items():
        if name in inherited:
            continue
        # Both files are generated from the same uiConfig key, so a substitution
        # with no input field (SDK base-class fields count) means the generators
        # diverged on the field's name or inclusion.
        if name not in input_cls.model_fields:
            errors.append(
                f"{rel}: substitution field '{name}' has no AppInputContract field"
            )
            continue
        default = field.get_default(call_default_factory=True)
        dumped = TypeAdapter(field.annotation).dump_python(default, mode="json")
        mine = []
        try:
            input_cls.model_validate({name: dumped})
        except ValidationError as exc:
            # Other required input fields are absent by construction; only
            # an error on this field is a ladder disagreement.
            mine = [e for e in exc.errors() if e["loc"][:1] == (name,)]
        issue = _KNOWN_DISAGREEMENTS.get((rel, name))
        if mine and issue is None:
            errors.append(
                f"{rel}: default {dumped!r} for '{name}' fails "
                f"AppInputContract: {mine[0]['msg']}"
            )
        elif not mine and issue is not None:
            errors.append(
                f"{rel}: '{name}' now passes; remove its _KNOWN_DISAGREEMENTS "
                f"entry ({issue})"
            )
    return errors


def check_init_files(input_files: list[Path]) -> list[str]:
    """Every generated ``_input.py`` needs an ``__init__.py`` beside it.

    The import test loads each file by path, which works without one; an app
    imports ``app.generated._input`` as a package and does not.
    """
    return [
        f"{path.relative_to(REPO_ROOT)}: no __init__.py beside it "
        "(required for Python imports)"
        for path in input_files
        if not (path.parent / "__init__.py").is_file()
    ]


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
    if stray := sorted(set(names["fieldMinSdk"]) - extraction):
        errors.append(f"fieldMinSdk: {stray} are not base-class fields")
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

    logger.info(":: Checking __init__.py beside every _input.py...")
    init_errors = check_init_files(input_files)
    for err in init_errors:
        logger.error("FAIL: %s", err)
    if not init_errors:
        logger.info("  OK: every _input.py is in a package")

    e2e_files = sorted(p for p in EXAMPLES_DIR.rglob("_e2e_*.py"))
    if e2e_files:
        logger.info(":: Testing %d _e2e_*.py files against SDK...", len(e2e_files))
        for path in e2e_files:
            if not test_e2e(path):
                failed += 1

    subs_files = [p for p in e2e_files if p.stem == "_e2e_substitutions"]
    logger.info(
        ":: Validating %d _e2e_substitutions.py defaults against _input.py...",
        len(subs_files),
    )
    round_trip_errors: list[str] = []
    for path in subs_files:
        round_trip_errors += check_substitution_round_trip(path)
    for err in round_trip_errors:
        logger.error("FAIL: %s", err)
    if not round_trip_errors:
        logger.info("  OK: every substitution default passes its input field")

    logger.info(":: Comparing SdkNames.pkl with the SDK...")
    name_errors = check_sdk_names(input_files)
    for err in name_errors:
        logger.error("FAIL: %s", err)
    if not name_errors:
        logger.info("  OK: SdkNames.pkl matches the SDK")

    total = len(input_files) + len(e2e_files)
    if failed or init_errors or name_errors or round_trip_errors:
        logger.error(
            "%d/%d files failed import test; %d missing __init__.py; "
            "%d SdkNames.pkl mismatch(es); "
            "%d substitution default(s) rejected by _input.py.",
            failed,
            total,
            len(init_errors),
            len(name_errors),
            len(round_trip_errors),
        )
        return 1

    logger.info("All %d files imported successfully.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
