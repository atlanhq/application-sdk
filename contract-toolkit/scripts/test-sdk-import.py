"""Validate generated Python against the SDK.

Every generated _input.py and _e2e_*.py must import cleanly and sit in a
package (an ``__init__.py`` beside it), the SDK names ``src/SdkNames.pkl``
copies by hand must match the installed SDK, and every default in a generated
``_e2e_substitutions.py`` must pass the matching field of the ``_input.py``
generated beside it. The connector constants ``src/Connectors.pkl`` copies by
hand must match pyatlan's ``AtlanConnectorType``. A ``stringMap``
ConditionalInput's generated validator must coerce and refuse the documented
value matrix (``check_string_map_matrix``).

Importing ``_input.py`` also proves it is valid Python and that the SDK's
payload-safety check accepts it: ``Input.__init_subclass__`` refuses an
unbounded field such as ``dict[str, Any]`` unless the class opts out.

Runs against the SDK in this repository. From the repo root::

    uv run --extra workflows python contract-toolkit/scripts/test-sdk-import.py
"""

import ast
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
SDK_NAMES_PKL = REPO_ROOT / "src" / "SdkNames.pkl"
CONNECTORS_PKL = REPO_ROOT / "src" / "Connectors.pkl"

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
            # BaseE2ETest subclasses are not Pydantic models. hasattr on the
            # generated class is true by construction (it assigns the name), so
            # check every name its body assigns against its SDK bases instead:
            # a name the SDK renamed or dropped is a dead class attribute.
            if dead := _dead_class_attrs(path, cls):
                logger.error(
                    "FAIL: %s — %s assigns %s, which no SDK base declares",
                    path,
                    matches[0],
                    dead,
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


def _assigned_class_attrs(path: Path, class_name: str) -> list[str]:
    """Names the body of ``class_name`` in ``path`` assigns, in source order."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    names += [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                elif isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    names.append(stmt.target.id)
            return names
    raise ValueError(f"{path}: class {class_name} not found")


def _sdk_declared_attrs(cls: type) -> set[str]:
    """Names declared (assigned or annotated) on ``cls``'s bases."""
    declared: set[str] = set()
    for base in cls.__mro__[1:]:
        declared |= set(vars(base)) | set(inspect.get_annotations(base))
    return declared


def _dead_class_attrs(path: Path, cls: type) -> list[str]:
    """Names the generated class assigns that none of its SDK bases declare."""
    declared = _sdk_declared_attrs(cls)
    return sorted(
        n for n in _assigned_class_attrs(path, cls.__name__) if n not in declared
    )


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


# ConditionalInput(stringMap = true) in the `full` example. App.pkl and
# NativeApp.pkl emit the same validator text (pinned by
# tests/conditional_string_map_test.pkl), so this covers both paths.
_STRING_MAP_INPUT = EXAMPLES_DIR / "full" / "app" / "generated" / "_input.py"
_STRING_MAP_FIELD = "dsn_mapping"
_QN = "default/snowflake/1/DB"
# (input value, expected field value) — or None as expected for "refused".
_STRING_MAP_CASES: list[tuple[object, dict[str, str] | None]] = [
    ({"DSN": _QN}, {"DSN": _QN}),
    (json.dumps({"DSN": _QN}), {"DSN": _QN}),
    ({}, {}),
    ("{}", {}),
    ("", {}),
    ("   ", {}),
    (None, {}),
    ("false", {}),
    ("[]", {}),
    ("1", {}),
    ("not json", {}),
    ({"DSN": {"host": "h"}}, None),
    ({"DSN": 1}, None),
    ({"DSN": None}, None),
]


def check_string_map_matrix() -> list[str]:
    """Run the generated ``stringMap`` validator against real values.

    The pkl tests only match emitted source. This instantiates the generated
    contract, so a change that keeps the text plausible but breaks coercion
    (``"[]"`` no longer becoming ``{}``) or typing (a non-string value no
    longer refused) fails here.
    """
    from pydantic import ValidationError

    mod, err = _load_module(_STRING_MAP_INPUT)
    if mod is None:
        return [f"{_STRING_MAP_INPUT}: cannot load ({err})"]
    cls = getattr(mod, "AppInputContract", None)
    if cls is None or _STRING_MAP_FIELD not in cls.model_fields:
        return [f"{_STRING_MAP_INPUT}: AppInputContract.{_STRING_MAP_FIELD} not found"]

    errors: list[str] = []
    for value, expected in _STRING_MAP_CASES:
        try:
            got = getattr(cls(**{_STRING_MAP_FIELD: value}), _STRING_MAP_FIELD)
        except ValidationError as exc:
            mine = [e for e in exc.errors() if e["loc"][:1] == (_STRING_MAP_FIELD,)]
            if expected is not None or not mine:
                errors.append(
                    f"{_STRING_MAP_FIELD}={value!r}: refused, expected {expected!r}"
                )
            continue
        if expected is None:
            errors.append(
                f"{_STRING_MAP_FIELD}={value!r}: accepted as {got!r}, expected refusal"
            )
        elif got != expected:
            errors.append(
                f"{_STRING_MAP_FIELD}={value!r}: got {got!r}, expected {expected!r}"
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


class _SdkNames(TypedDict):
    """``pkl eval -f json src/SdkNames.pkl``."""

    inputFields: list[str]
    extractionInputFields: list[str]
    fieldMinSdk: dict[str, str]
    inputModuleNames: list[str]
    mustacheSubstitutionFields: list[str]
    sqlMustacheSubstitutionFields: list[str]
    e2eBaseAttributes: list[str]
    e2eConnectionCategoryDefault: str
    artifactFieldTypes: list[str]
    artifactFieldTypesExtended: list[str]
    artifactFormats: list[str]


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
    result = subprocess.run(
        ["pkl", "eval", "-f", "json", str(SDK_NAMES_PKL)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [f"pkl eval {SDK_NAMES_PKL} failed: {result.stderr.strip()}"]
    names: _SdkNames = json.loads(result.stdout)
    errors = _check_input_names(names, input_files)
    errors += _check_e2e_names(names)
    errors += _check_artifact_names(names)
    return errors


def _check_input_names(names: _SdkNames, input_files: list[Path]) -> list[str]:
    from application_sdk.contracts.base import Input
    from application_sdk.templates.contracts import (
        ExtractionInput,
        TreeSelectionExtractionInput,
    )

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


def _check_e2e_names(names: _SdkNames) -> list[str]:
    """The e2e names App.pkl emits into ``_e2e_substitutions.py`` / ``_e2e_base.py``.

    A substitution field the SDK adds but the skip set lacks is redeclared in
    every connector's generated subclass with the toolkit's inferred type and
    default, shadowing the SDK's. A base attribute the SDK renames is assigned
    to a name the harness never reads.
    """
    from application_sdk.testing.e2e import BaseE2ETest
    from application_sdk.testing.e2e.substitutions import (
        MustacheSubstitutions,
        SQLMustacheSubstitutions,
    )

    errors = _diff(
        "mustacheSubstitutionFields vs MustacheSubstitutions",
        set(names["mustacheSubstitutionFields"]),
        set(MustacheSubstitutions.model_fields),
    )
    errors += _diff(
        "sqlMustacheSubstitutionFields vs SQLMustacheSubstitutions",
        set(names["sqlMustacheSubstitutionFields"]),
        set(SQLMustacheSubstitutions.model_fields),
    )
    # Walk the MRO: BaseE2ETest is composed from private mixin layers, and its
    # class attributes are declared on the lowest one (FND-4267).
    declared: set[str] = set()
    for klass in BaseE2ETest.__mro__:
        declared |= set(vars(klass)) | set(inspect.get_annotations(klass))
    if undeclared := sorted(set(names["e2eBaseAttributes"]) - declared):
        errors.append(
            f"e2eBaseAttributes: SdkNames.pkl lists {undeclared}, "
            "BaseE2ETest does not declare"
        )
    default = names["e2eConnectionCategoryDefault"]
    if BaseE2ETest.connection_category != default:
        errors.append(
            f"e2eConnectionCategoryDefault: SdkNames.pkl says {default!r}, "
            f"BaseE2ETest.connection_category defaults to "
            f"{BaseE2ETest.connection_category!r}"
        )
    return errors


def _check_artifact_names(names: _SdkNames) -> list[str]:
    """The artifact vocabulary App.pkl's typealiases accept.

    ``tests/artifact_schemas_test.pkl`` pins each typealias to its SdkNames.pkl
    set; this pins the set to the SDK, so a value one side adds is refused by
    the other at the toolkit's ``pkl eval`` or the SDK's validator.
    """
    from application_sdk.validation.artifacts import (
        ARTIFACT_FIELD_TYPES,
        ARTIFACT_FIELD_TYPES_EXTENDED,
        ARTIFACT_FORMATS,
    )

    errors = _diff(
        "artifactFieldTypes vs ARTIFACT_FIELD_TYPES",
        set(names["artifactFieldTypes"]),
        set(ARTIFACT_FIELD_TYPES),
    )
    errors += _diff(
        "artifactFieldTypesExtended vs ARTIFACT_FIELD_TYPES_EXTENDED",
        set(names["artifactFieldTypesExtended"]),
        set(ARTIFACT_FIELD_TYPES_EXTENDED),
    )
    errors += _diff(
        "artifactFormats vs ARTIFACT_FORMATS",
        set(names["artifactFormats"]),
        set(ARTIFACT_FORMATS),
    )
    return errors


# Lists Connectors.pkl's `Type` constants (they are `hidden`, so a plain
# `pkl eval` renders none) and the members of `AtlanConnectionCategory`.
_CONNECTORS_EXPR = (
    'let (r = import("pkl:reflect").Module(module)) '
    "new JsonRenderer {}.renderValue(new Dynamic { "
    "connectors = new Mapping { for (n, _ in r.moduleClass.properties) { "
    "when (module.getProperty(n) is Type) { [n] = module.getProperty(n) } } }; "
    'categories = r.typeAliases["AtlanConnectionCategory"].referent.members'
    ".toList().map((t) -> t.value) })"
)

# Connectors.pkl constants pyatlan has no member for, by name.
_PKL_ONLY_CONNECTORS = {
    # A toolkit placeholder for a connector Atlan does not register.
    "UNKNOWN_CUSTOM",
    # Registered by the toolkit ahead of pyatlan (#3615).
    "QLIKSENSE_ENTERPRISE_WINDOWS",
}

# (name, Connectors.pkl category, pyatlan category). The category picks a
# contract's generated e2e base class and its `connection_category`, so
# correcting one changes every contract that uses the constant.
_KNOWN_CATEGORY_MISMATCHES = {
    ("SAP_ECC", "warehouse", "erp"),
}


class _PklConnector(TypedDict):
    value: str
    category: str


class _PklConnectors(TypedDict):
    connectors: dict[str, _PklConnector]
    categories: list[str]


def check_connectors() -> list[str]:
    """Compare Connectors.pkl's hand-copied registry with pyatlan's.

    Nothing else fails when pyatlan adds a connector type or category: an app
    cannot write ``connector = Connectors.X`` until the toolkit copies it, and
    cannot construct a ``Type`` in a category the typealias lacks.
    """
    from pyatlan.model.enums import AtlanConnectionCategory, AtlanConnectorType

    result = subprocess.run(
        ["pkl", "eval", "-x", _CONNECTORS_EXPR, str(CONNECTORS_PKL)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [f"pkl eval {CONNECTORS_PKL} failed: {result.stderr.strip()}"]
    pkl: _PklConnectors = json.loads(result.stdout)
    return _diff_connectors(
        pkl,
        {t.name: (t.value, t.category.value) for t in AtlanConnectorType},
        {c.value for c in AtlanConnectionCategory},
    )


def _diff_connectors(
    pkl: _PklConnectors,
    sdk: dict[str, tuple[str, str]],
    sdk_categories: set[str],
) -> list[str]:
    """Connectors.pkl vs pyatlan, as ``{name: (value, category)}`` and categories."""
    errors: list[str] = []
    connectors = pkl["connectors"]
    if missing := sorted(set(sdk) - set(connectors)):
        errors.append(
            f"Connectors.pkl: pyatlan's AtlanConnectorType has {missing}, "
            "Connectors.pkl does not"
        )
    extra = set(connectors) - set(sdk)
    if unlisted := sorted(extra - _PKL_ONLY_CONNECTORS):
        errors.append(
            f"Connectors.pkl: {unlisted} are not in pyatlan's AtlanConnectorType"
        )
    if stale := sorted(_PKL_ONLY_CONNECTORS - extra):
        errors.append(
            f"_PKL_ONLY_CONNECTORS: {stale} are no longer Connectors.pkl-only; "
            "remove them"
        )
    seen: set[tuple[str, str, str]] = set()
    for name in sorted(set(sdk) & set(connectors)):
        value, category = sdk[name]
        mine = connectors[name]
        if mine["value"] != value:
            errors.append(
                f"Connectors.pkl: {name}.value is {mine['value']!r}, "
                f"pyatlan says {value!r}"
            )
        if mine["category"] != category:
            key = (name, mine["category"], category)
            if key in _KNOWN_CATEGORY_MISMATCHES:
                seen.add(key)
            else:
                errors.append(
                    f"Connectors.pkl: {name}.category is {mine['category']!r}, "
                    f"pyatlan says {category!r}"
                )
    if stale_mismatch := sorted(_KNOWN_CATEGORY_MISMATCHES - seen):
        errors.append(
            f"_KNOWN_CATEGORY_MISMATCHES: {stale_mismatch} no longer differ; "
            "remove them"
        )
    categories = set(pkl["categories"])
    if missing_cat := sorted(sdk_categories - categories):
        errors.append(
            f"AtlanConnectionCategory: pyatlan has {missing_cat}, "
            "the Connectors.pkl typealias does not"
        )
    if extra_cat := sorted(categories - sdk_categories):
        errors.append(
            f"AtlanConnectionCategory: Connectors.pkl accepts {extra_cat}, "
            "pyatlan does not"
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

    logger.info(":: Validating the stringMap ConditionalInput value matrix...")
    string_map_errors = check_string_map_matrix()
    for err in string_map_errors:
        logger.error("FAIL: %s", err)
    if not string_map_errors:
        logger.info(
            "  OK: %d stringMap cases behave as documented", len(_STRING_MAP_CASES)
        )

    logger.info(":: Comparing SdkNames.pkl with the SDK...")
    name_errors = check_sdk_names(input_files)
    for err in name_errors:
        logger.error("FAIL: %s", err)
    if not name_errors:
        logger.info("  OK: SdkNames.pkl matches the SDK")

    logger.info(":: Comparing Connectors.pkl with pyatlan...")
    connector_errors = check_connectors()
    for err in connector_errors:
        logger.error("FAIL: %s", err)
    if not connector_errors:
        logger.info("  OK: Connectors.pkl matches pyatlan")

    total = len(input_files) + len(e2e_files)
    if (
        failed
        or init_errors
        or name_errors
        or round_trip_errors
        or connector_errors
        or string_map_errors
    ):
        logger.error(
            "%d/%d files failed import test; %d missing __init__.py; "
            "%d SdkNames.pkl mismatch(es); "
            "%d substitution default(s) rejected by _input.py; "
            "%d Connectors.pkl mismatch(es); "
            "%d stringMap case(s) wrong.",
            failed,
            total,
            len(init_errors),
            len(name_errors),
            len(round_trip_errors),
            len(connector_errors),
            len(string_map_errors),
        )
        return 1

    logger.info("All %d files imported successfully.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
