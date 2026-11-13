"""Assert an App run's output against declared schemas, value rules included.

The fixture kit (:mod:`~application_sdk.testing.integration.fixtures`) runs an
App in process. This module answers the next question an integration suite asks:
*is what the run wrote the shape it should be?* It replaces
:func:`~application_sdk.testing.integration.validation.validate_with_pandera`
without pandas or pandera on the path.

Usage::

    from application_sdk.testing.integration.output_schema import (
        assert_output_matches,
        load_pandera_schemas,
    )

    result = await executor.execute_app(MyApp, MyInput(...))
    assert_output_matches(run_root, load_pandera_schemas("tests/integration/schema/transformed"))

or, declared natively::

    TABLE = OutputSchema(
        typename="table",
        min_records=1,
        fields=(
            FieldRule("typeName", type="string", allowed=("Table", "View")),
            FieldRule("attributes.qualifiedName", type="string", pattern=r".+/.+/.+/.+"),
        ),
    )
    assert_output_matches(run_root, [TABLE])

**Two passes, each on an existing seam.** Presence and logical type are checked by
the ADR-0020 format validators unchanged —
:class:`~application_sdk.validation.ndjson.NdjsonValidator` or
:class:`~application_sdk.validation.parquet.ParquetFooterValidator`, handed a
:class:`~application_sdk.validation.artifacts.FieldMapDeclaration` built from the
rules — so a field's type means exactly what it means at runtime. Value rules
(null, allowed values, pattern, length, range, uniqueness) and the record count
then take one more pass:

* NDJSON through :func:`~application_sdk.validation.ndjson.iter_ndjson_lines`, the
  one NDJSON walk in the tree, one record at a time.
* parquet through ``pyarrow`` ``iter_batches``, reading **only** the columns a value
  rule names, one batch at a time; the record count comes from the footer. pyarrow
  is imported inside the function, as the footer validator does.

Memory is bounded by one record (NDJSON) or one batch (parquet), plus the distinct
values of any field declared ``unique`` — that set is the rule.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, cast

import orjson
import yaml

from application_sdk.testing.integration._errors import OutputSchemaDeclarationError
from application_sdk.validation.artifacts import (
    ARTIFACT_FIELD_TYPES_EXTENDED,
    ELEMENT_STEP,
    FORMAT_NDJSON,
    FORMAT_PARQUET,
    ArtifactFieldTypeExtended,
    ArtifactFormat,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    FieldPathStep,
    parse_field_path,
)
from application_sdk.validation.ndjson import (
    NDJSON_SUFFIXES,
    NdjsonValidator,
    iter_ndjson_lines,
)
from application_sdk.validation.parquet import ParquetFooterValidator

__all__ = [
    "FieldRule",
    "OutputSchema",
    "OutputValidationReport",
    "OutputViolation",
    "RuleKind",
    "TypenameReport",
    "assert_output_matches",
    "load_pandera_schemas",
    "validate_output",
]

Scalar = str | int | float | bool
"""A JSON scalar an ``allowed`` rule may list."""

RuleKind = Literal[
    "absent",
    "unsupported",
    "undecodable",
    "missing",
    "type",
    "null",
    "allowed",
    "pattern",
    "length",
    "range",
    "unique",
    "record_count",
]
"""Which rule a violation broke. The first three are about the artifact as a whole:
nothing to read, no validator able to read it, or a unit that could not be parsed."""

_Refuse = Callable[[str, str], OutputSchemaDeclarationError]
"""Builds the error for one refused pandera key: ``(key, why) -> error``."""

MAX_EXAMPLES_PER_RULE: Final = 5
"""Examples kept per ``(field, rule)``. The count is always exact; only the listed
examples are capped, so a uniformly broken field is one line, not 10k."""


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRule:
    """What one field of every record must satisfy.

    ``path`` uses the ADR-0020 grammar: ``attributes.qualifiedName`` descends into
    a nested object, ``attributes.columns[].name`` into every element of an array.

    Value rules are checked on non-null values only. ``null`` is governed by
    ``nullable`` alone, the same split pandera makes.
    """

    path: str
    type: ArtifactFieldTypeExtended = "any"
    """Logical type, checked by the ADR-0020 format validator. ``any`` asserts
    presence only."""
    required: bool = True
    """The key must be present in every record. When False, every other rule
    applies only where it is present."""
    nullable: bool = True
    """When False, a present ``null`` fails."""
    allowed: tuple[Scalar, ...] | None = None
    """The value must be one of these (pandera ``isin``, ``equal_to``)."""
    pattern: str | None = None
    """A regex the **whole** string value must match (``re.fullmatch``)."""
    min_length: int | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None
    unique: bool = False
    """No non-null value may repeat across the artifact."""

    def __post_init__(self) -> None:
        if not self.path:
            raise OutputSchemaDeclarationError(
                message="FieldRule.path must be a non-empty field path",
                field="path",
            )
        if self.type not in ARTIFACT_FIELD_TYPES_EXTENDED:
            raise OutputSchemaDeclarationError(
                message=(
                    f"FieldRule {self.path!r}: type {self.type!r} is not an ADR-0020 "
                    f"logical type ({', '.join(sorted(ARTIFACT_FIELD_TYPES_EXTENDED))})"
                ),
                field="type",
                value_summary=str(self.type),
            )
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise OutputSchemaDeclarationError(
                    message=f"FieldRule {self.path!r}: pattern does not compile: {exc}",
                    field="pattern",
                    value_summary=self.pattern,
                ) from exc

    @property
    def has_value_rules(self) -> bool:
        """Whether anything beyond presence and type needs the value itself."""
        return (
            not self.nullable
            or self.unique
            or any(
                bound is not None
                for bound in (
                    self.allowed,
                    self.pattern,
                    self.min_length,
                    self.max_length,
                    self.minimum,
                    self.maximum,
                    self.exclusive_minimum,
                    self.exclusive_maximum,
                )
            )
        )


@dataclass(frozen=True)
class OutputSchema:
    """The declared shape of one typename's output directory.

    ``typename`` is the directory under the run's ``subdirectory`` —
    ``"table"`` for ``<run_root>/transformed/table/*.json``. It may contain ``/``.
    """

    typename: str
    fields: tuple[FieldRule, ...] = ()
    min_records: int | None = None
    max_records: int | None = None
    artifact_format: ArtifactFormat | None = None
    """``ndjson`` or ``parquet``. ``None`` detects it from the files present and
    fails when there are both or neither."""

    def __post_init__(self) -> None:
        if not self.typename:
            raise OutputSchemaDeclarationError(
                message="OutputSchema.typename must be a non-empty directory name",
                field="typename",
            )
        seen: set[str] = set()
        for rule in self.fields:
            if rule.path in seen:
                raise OutputSchemaDeclarationError(
                    message=f"OutputSchema {self.typename!r} declares {rule.path!r} twice",
                    field="fields",
                    value_summary=rule.path,
                )
            seen.add(rule.path)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputViolation:
    """One example of a broken rule."""

    rule: RuleKind
    field: str
    """The declared path, or ``""`` for a whole-artifact violation."""
    message: str
    file: str = ""
    line: int = 0
    """1-based line (NDJSON) or row (parquet) within ``file``; 0 when not about one."""


@dataclass(frozen=True)
class RuleFailures:
    """Every failure of one rule on one field, counted exactly, examples capped."""

    field: str
    rule: RuleKind
    count: int
    examples: tuple[OutputViolation, ...]


@dataclass(frozen=True)
class TypenameReport:
    """The verdict for one :class:`OutputSchema`."""

    typename: str
    path: str
    artifact_format: str
    records: int
    failures: tuple[RuleFailures, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    def format_report(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        head = (
            f"[{status}] {self.typename} ({self.artifact_format or '?'}, "
            f"{self.records} records) — {self.path}"
        )
        lines = [head]
        for group in self.failures:
            label = group.field or "<artifact>"
            lines.append(f"    {label}: {group.rule} x{group.count}")
            for example in group.examples:
                where = ""
                if example.file:
                    where = (
                        f" ({example.file}:{example.line})"
                        if example.line
                        else f" ({example.file})"
                    )
                lines.append(f"      - {example.message}{where}")
            hidden = group.count - len(group.examples)
            if hidden > 0:
                lines.append(f"      ... and {hidden} more")
        return "\n".join(lines)


@dataclass(frozen=True)
class OutputValidationReport:
    """The verdict across every declared typename."""

    typenames: tuple[TypenameReport, ...]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.typenames)

    @property
    def failed(self) -> tuple[TypenameReport, ...]:
        return tuple(t for t in self.typenames if not t.ok)

    def format_report(self) -> str:
        passed = sum(1 for t in self.typenames if t.ok)
        lines = [
            f"Output schema validation: {passed}/{len(self.typenames)} typenames passed"
        ]
        lines.extend(t.format_report() for t in self.typenames)
        return "\n".join(lines)


class _Collector:
    """Counts failures per ``(field, rule)`` and keeps the first few examples."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, RuleKind], int] = {}
        self._examples: dict[tuple[str, RuleKind], list[OutputViolation]] = {}

    def add(self, violation: OutputViolation) -> None:
        key = (violation.field, violation.rule)
        self._counts[key] = self._counts.get(key, 0) + 1
        examples = self._examples.setdefault(key, [])
        if len(examples) < MAX_EXAMPLES_PER_RULE:
            examples.append(violation)

    def freeze(self) -> tuple[RuleFailures, ...]:
        return tuple(
            RuleFailures(
                field=key[0],
                rule=key[1],
                count=count,
                examples=tuple(self._examples[key]),
            )
            for key, count in self._counts.items()
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def validate_output(
    output_root: str | Path,
    schemas: Iterable[OutputSchema],
    *,
    subdirectory: str = "transformed",
) -> OutputValidationReport:
    """Check ``<output_root>/<subdirectory>/<typename>`` against each schema.

    Never raises for bad output; that is what the report is for. Raises
    :class:`~application_sdk.testing.integration._errors.OutputSchemaDeclarationError`
    only for an empty schema list, which would otherwise pass vacuously.

    Args:
        output_root: The run's local output root — the directory holding
            ``transformed/`` and ``raw/``.
        schemas: One :class:`OutputSchema` per typename directory.
        subdirectory: Joined between the root and each typename. ``""`` for none.
    """
    declared = tuple(schemas)
    if not declared:
        raise OutputSchemaDeclarationError(
            message="validate_output was given no schemas, so it would check nothing",
            field="schemas",
        )
    base = Path(output_root) / subdirectory if subdirectory else Path(output_root)
    return OutputValidationReport(
        typenames=tuple(_validate_typename(base / s.typename, s) for s in declared)
    )


def assert_output_matches(
    output_root: str | Path,
    schemas: Iterable[OutputSchema],
    *,
    subdirectory: str = "transformed",
) -> OutputValidationReport:
    """:func:`validate_output`, raising ``AssertionError`` with the report on failure.

    Returns the report on success, for a suite that wants to assert more on it.
    """
    report = validate_output(output_root, schemas, subdirectory=subdirectory)
    if not report.ok:
        raise AssertionError(report.format_report())
    return report


# ---------------------------------------------------------------------------
# One typename
# ---------------------------------------------------------------------------


def _validate_typename(path: Path, schema: OutputSchema) -> TypenameReport:
    collector = _Collector()
    artifact_format = schema.artifact_format or _detect_format(path)
    if artifact_format is None:
        collector.add(
            OutputViolation(
                rule="absent",
                field="",
                message=f"no NDJSON or parquet output at {path}",
            )
        )
        return _report(schema, path, "", 0, collector)
    if artifact_format == "mixed":
        collector.add(
            OutputViolation(
                rule="unsupported",
                field="",
                message=(
                    f"{path} holds both NDJSON and parquet parts; set "
                    f"OutputSchema.artifact_format to say which one is the artifact"
                ),
            )
        )
        return _report(schema, path, "", 0, collector)

    if schema.fields:
        _type_pass(path, schema, artifact_format, collector)

    if artifact_format == FORMAT_NDJSON:
        records = _ndjson_value_pass(path, schema.fields, collector)
    else:
        records = _parquet_value_pass(path, schema.fields, collector)

    if records is not None:
        _check_record_count(schema, records, collector)
    return _report(schema, path, artifact_format, records or 0, collector)


def _report(
    schema: OutputSchema,
    path: Path,
    artifact_format: str,
    records: int,
    collector: _Collector,
) -> TypenameReport:
    return TypenameReport(
        typename=schema.typename,
        path=str(path),
        artifact_format=artifact_format,
        records=records,
        failures=collector.freeze(),
    )


def _detect_format(path: Path) -> str | None:
    """``ndjson``, ``parquet``, ``mixed`` (both present) or None (neither)."""
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in NDJSON_SUFFIXES:
            return FORMAT_NDJSON
        return FORMAT_PARQUET if suffix == ".parquet" else None
    if not path.is_dir():
        return None
    has_ndjson = has_parquet = False
    for candidate in path.rglob("*"):
        if not candidate.is_file():
            continue
        suffix = candidate.suffix.lower()
        has_ndjson = has_ndjson or suffix in NDJSON_SUFFIXES
        has_parquet = has_parquet or suffix == ".parquet"
    if has_ndjson and has_parquet:
        return "mixed"
    if has_ndjson:
        return FORMAT_NDJSON
    return FORMAT_PARQUET if has_parquet else None


def _check_record_count(
    schema: OutputSchema, records: int, collector: _Collector
) -> None:
    if schema.min_records is not None and records < schema.min_records:
        collector.add(
            OutputViolation(
                rule="record_count",
                field="",
                message=f"expected at least {schema.min_records} records, found {records}",
            )
        )
    if schema.max_records is not None and records > schema.max_records:
        collector.add(
            OutputViolation(
                rule="record_count",
                field="",
                message=f"expected at most {schema.max_records} records, found {records}",
            )
        )


# ---------------------------------------------------------------------------
# Pass 1: presence and type, through the ADR-0020 validators
# ---------------------------------------------------------------------------

_KIND_TO_RULE: Final[Mapping[str, RuleKind]] = {
    "missing": "missing",
    "type_mismatch": "type",
    "undecodable": "undecodable",
    "invalid": "type",
}


def _type_pass(
    path: Path, schema: OutputSchema, artifact_format: str, collector: _Collector
) -> None:
    declaration = FieldMapDeclaration(
        fields=tuple(
            DeclaredField(path=r.path, type=r.type, required=r.required)
            for r in schema.fields
        ),
        artifact_format=artifact_format,
    )
    validator = (
        NdjsonValidator()
        if artifact_format == FORMAT_NDJSON
        else ParquetFooterValidator()
    )
    report: ArtifactValidationReport = validator.validate(path, declaration)
    if report.verdict is not None:
        # absent / unsupported / not_declared: the artifact as a whole could not be
        # checked. In a test that is a failure, never a skip.
        collector.add(
            OutputViolation(
                rule="unsupported" if report.outcome == "unsupported" else "absent",
                field="",
                message=f"{report.outcome}: {report.reason}",
            )
        )
        return
    for failure in report.failures:
        collector.add(
            OutputViolation(
                rule=_KIND_TO_RULE.get(failure.kind, "type"),
                field=failure.field,
                message="; ".join(failure.errors) or failure.kind,
                file=failure.file,
                line=failure.line,
            )
        )


# ---------------------------------------------------------------------------
# Pass 2: values and record count
# ---------------------------------------------------------------------------

_ABSENT: Final = object()
"""The path did not resolve. Distinct from a JSON ``null``, which did."""


@dataclass
class _ValuePlan:
    rule: FieldRule
    steps: tuple[FieldPathStep, ...]
    regex: re.Pattern[str] | None
    allowed: frozenset[Scalar] | None
    seen: set[object] = field(default_factory=set)


def _plan(fields: Sequence[FieldRule]) -> list[_ValuePlan]:
    return [
        _ValuePlan(
            rule=r,
            steps=parse_field_path(r.path),
            regex=re.compile(r.pattern) if r.pattern is not None else None,
            allowed=frozenset(r.allowed) if r.allowed is not None else None,
        )
        for r in fields
        if r.has_value_rules
    ]


def _values_at(node: object, steps: tuple[FieldPathStep, ...]) -> Iterator[object]:
    """Every value ``steps`` addresses in ``node``; nothing where it does not resolve.

    Presence is the type pass's job, so an unresolved path yields nothing here
    rather than a second ``missing`` for the same record.
    """
    if not steps:
        yield node
        return
    head, rest = steps[0], steps[1:]
    if head is ELEMENT_STEP:
        if isinstance(node, list):
            for element in node:
                yield from _values_at(element, rest)
        return
    if isinstance(node, dict) and head in node:
        yield from _values_at(node[head], rest)


def _check_value(
    plan: _ValuePlan,
    value: object,
    file: str,
    line: int,
    collector: _Collector,
) -> None:
    rule = plan.rule

    def fail(kind: RuleKind, message: str) -> None:
        collector.add(
            OutputViolation(
                rule=kind, field=rule.path, message=message, file=file, line=line
            )
        )

    if value is None:
        if not rule.nullable:
            fail("null", f"{rule.path} is null but declared non-nullable")
        return

    if plan.allowed is not None and not _in_allowed(value, plan.allowed):
        fail(
            "allowed",
            f"{rule.path}={value!r} is not one of {sorted(map(repr, plan.allowed))}",
        )

    if plan.regex is not None:
        if not isinstance(value, str):
            fail(
                "pattern",
                f"{rule.path} pattern applies to strings; found {type(value).__name__}",
            )
        elif plan.regex.fullmatch(value) is None:
            fail("pattern", f"{rule.path}={value!r} does not match {rule.pattern!r}")

    if rule.min_length is not None or rule.max_length is not None:
        if not isinstance(value, (str, list)):
            fail(
                "length",
                f"{rule.path} length applies to strings and arrays; found {type(value).__name__}",
            )
        else:
            size = len(value)
            if rule.min_length is not None and size < rule.min_length:
                fail(
                    "length",
                    f"{rule.path} has length {size}, below min_length {rule.min_length}",
                )
            if rule.max_length is not None and size > rule.max_length:
                fail(
                    "length",
                    f"{rule.path} has length {size}, above max_length {rule.max_length}",
                )

    bounds = (
        rule.minimum,
        rule.maximum,
        rule.exclusive_minimum,
        rule.exclusive_maximum,
    )
    if any(b is not None for b in bounds):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            fail(
                "range",
                f"{rule.path} range applies to numbers; found {type(value).__name__}",
            )
        else:
            if rule.minimum is not None and value < rule.minimum:
                fail("range", f"{rule.path}={value!r} is below minimum {rule.minimum}")
            if rule.maximum is not None and value > rule.maximum:
                fail("range", f"{rule.path}={value!r} is above maximum {rule.maximum}")
            if rule.exclusive_minimum is not None and value <= rule.exclusive_minimum:
                fail(
                    "range",
                    f"{rule.path}={value!r} is not above {rule.exclusive_minimum}",
                )
            if rule.exclusive_maximum is not None and value >= rule.exclusive_maximum:
                fail(
                    "range",
                    f"{rule.path}={value!r} is not below {rule.exclusive_maximum}",
                )

    if rule.unique:
        key = _hashable(value)
        if key in plan.seen:
            fail("unique", f"{rule.path}={value!r} repeats")
        else:
            plan.seen.add(key)


def _in_allowed(value: object, allowed: frozenset[Scalar]) -> bool:
    # ``True == 1`` in Python; keep a bool from satisfying an int (and vice versa).
    if isinstance(value, bool):
        return any(isinstance(a, bool) and a is value for a in allowed)
    if not isinstance(value, (str, int, float)):
        return False
    return any(not isinstance(a, bool) and a == value for a in allowed)


def _hashable(value: object) -> object:
    if isinstance(value, (dict, list)):
        return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)
    if isinstance(value, bool):
        return ("bool", value)
    return value


def _ndjson_value_pass(
    path: Path, fields: Sequence[FieldRule], collector: _Collector
) -> int | None:
    plans = _plan(fields)
    records = 0
    try:
        for file, line, raw in iter_ndjson_lines(path):
            records += 1
            if fields and not plans:
                # Presence, type and decodability are the type pass's; nothing
                # left to read the value for.
                continue
            try:
                record = orjson.loads(raw)
            except orjson.JSONDecodeError:
                # The type pass reports this when fields are declared; with none,
                # it never ran, so it is reported here.
                if not fields:
                    collector.add(
                        OutputViolation(
                            rule="undecodable",
                            field="",
                            message="not valid JSON",
                            file=file,
                            line=line,
                        )
                    )
                continue
            for plan in plans:
                for value in _values_at(record, plan.steps):
                    _check_value(plan, value, file, line, collector)
    except OSError as exc:
        collector.add(
            OutputViolation(
                rule="absent", field="", message=f"could not read {path}: {exc}"
            )
        )
        return None
    if records == 0:
        # With fields, the type pass has already said ``absent``. Either way a
        # record-count verdict on nothing would only repeat it.
        if not fields:
            collector.add(
                OutputViolation(
                    rule="absent", field="", message=f"no NDJSON records at {path}"
                )
            )
        return None
    return records


def _parquet_value_pass(
    path: Path, fields: Sequence[FieldRule], collector: _Collector
) -> int | None:
    from application_sdk.validation.parquet import (  # noqa: PLC0415 — private helper, same package
        _parquet_files,
    )

    files = _parquet_files(path)
    if not files:
        if not fields:
            collector.add(
                OutputViolation(
                    rule="absent", field="", message=f"no parquet file at {path}"
                )
            )
        return None
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow
    except ImportError:  # conformance: ignore[E008] pyarrow is an optional extra
        if not fields:
            # With fields, the footer validator has already reported this.
            collector.add(
                OutputViolation(
                    rule="unsupported",
                    field="",
                    message="pyarrow is not installed; parquet output cannot be read",
                )
            )
        return (
            None  # conformance: ignore[E007] reported as an unsupported violation above
        )

    plans = _plan(fields)
    columns = sorted({str(p.steps[0]) for p in plans})
    records = 0
    for file in files:
        try:
            parquet_file = pq.ParquetFile(file)
        except Exception as exc:  # noqa: BLE001 - one unreadable part is one violation
            collector.add(
                OutputViolation(
                    rule="undecodable",
                    field="",
                    message=f"{type(exc).__name__}: {exc}",
                    file=str(file),
                )
            )
            continue
        offset = records
        records += parquet_file.metadata.num_rows
        if not plans:
            continue
        present = set(parquet_file.schema_arrow.names)
        wanted = [c for c in columns if c in present]
        if not wanted:
            continue
        row = offset
        for batch in parquet_file.iter_batches(columns=wanted):
            for record in batch.to_pylist():
                row += 1
                for plan in plans:
                    for value in _values_at(record, plan.steps):
                        _check_value(plan, value, str(file), row - offset, collector)
    return records


# ---------------------------------------------------------------------------
# Reading pandera YAML
# ---------------------------------------------------------------------------

_PANDERA_DTYPES: Final[Mapping[str, ArtifactFieldTypeExtended]] = {
    "str": "string",
    "string": "string",
    "object": "any",
    "int": "int",
    "int8": "int",
    "int16": "int",
    "int32": "int",
    "int64": "int",
    "Int64": "int",
    "float": "float",
    "float32": "float",
    "float64": "float",
    "bool": "bool",
    "boolean": "bool",
    "datetime64[ns]": "timestamp",
    "datetime64[ns, UTC]": "timestamp",
}
"""pandera dtype -> ADR-0020 logical type. ``object`` asserted nothing in pandera
beyond presence, so it maps to ``any``."""

_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_type",
        "api",
        "version",
        "columns",
        "checks",
        "index",
        "dtype",
        "coerce",
        "strict",
        "name",
        "title",
        "description",
        "unique",
        "report_duplicates",
        "ordered",
        "unique_column_names",
        "add_missing_columns",
        "drop_invalid_rows",
        "metadata",
    }
)
_COLUMN_KEYS: Final = frozenset(
    {
        "dtype",
        "nullable",
        "required",
        "unique",
        "coerce",
        "regex",
        "checks",
        "name",
        "title",
        "description",
        "metadata",
        "drop_invalid_rows",
        "report_duplicates",
    }
)
_MUST_BE_FALSY: Final = (
    "index",
    "dtype",
    "coerce",
    "strict",
    "unique",
    "ordered",
    "add_missing_columns",
    "drop_invalid_rows",
)
"""Top-level keys that change what pandera checks when set. The fleet sets none of
them; one that is set is refused rather than silently dropped."""


def load_pandera_schemas(schema_dir: str | Path) -> tuple[OutputSchema, ...]:
    """Read a directory of pandera YAML schemas into :class:`OutputSchema` values.

    The migration path off
    :func:`~application_sdk.testing.integration.validation.validate_with_pandera`:
    point this at the same directory and pass the result to
    :func:`assert_output_matches`. pandera need not be installed.

    Each file maps to a typename directory: ``table.yaml`` to ``table``,
    ``raw/table.yaml`` to ``raw/table``, and ``table/schema.yaml`` to ``table``.

    Translated (the subset the fleet uses): column ``dtype``, ``nullable``,
    ``required``, ``unique``; checks ``isin``, ``equal_to``, ``str_matches``,
    ``str_startswith``, ``str_length``, ``greater_than``,
    ``greater_than_or_equal_to``, ``less_than``, ``less_than_or_equal_to``,
    ``in_range``; schema checks ``check_record_count_ge`` / ``_le`` / ``_eq``.

    A column key pandera does not reserve (``equal_to: X`` beside ``dtype``) is
    check shorthand, and pandera enforces it, so it is translated as a check too.

    Where the result is deliberately not identical to pandera:

    * ``str_matches`` keeps pandera's prefix semantics (``re.match``) by appending
      ``(?s:.*)`` to the pattern, because :attr:`FieldRule.pattern` matches the
      whole value. Same verdicts.
    * Two cases come out **looser**, because presence here is per record and
      pandera's is per merged dataframe. ``required: true`` with ``nullable: true``
      becomes ``required=False`` — pandera only failed when *no* record carried
      the key. ``required: false`` with ``nullable: false`` lets a record omit the
      key — pandera failed when *some* records carried it and others did not.

    Anything else — a check it does not translate, an unknown top-level key, a
    schema option that changes pandera's behaviour — raises
    :class:`~application_sdk.testing.integration._errors.OutputSchemaDeclarationError`
    naming the file and key. Nothing is dropped silently.
    """
    base = Path(schema_dir)
    if not base.is_dir():
        raise OutputSchemaDeclarationError(
            message=f"pandera schema directory not found: {base}",
            field="schema_dir",
            value_summary=str(base),
        )
    files = sorted([*base.rglob("*.yaml"), *base.rglob("*.yml")])
    if not files:
        raise OutputSchemaDeclarationError(
            message=f"no pandera YAML schemas under {base}",
            field="schema_dir",
            value_summary=str(base),
        )
    return tuple(_load_pandera_file(f, _typename_for(f, base)) for f in files)


def _typename_for(file: Path, base: Path) -> str:
    relative = file.relative_to(base).with_suffix("")
    if relative.name == "schema" and len(relative.parts) > 1:
        relative = relative.parent
    return relative.as_posix()


def _mapping(value: object) -> dict[str, object] | None:
    """``value`` as a string-keyed mapping, or None when it is not a mapping."""
    if not isinstance(value, dict):
        return None
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def _number(value: object, at: str, refuse: _Refuse) -> float | None:
    """A numeric check argument, refusing anything that is not a number."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise refuse(at, f"expected a number, found {value!r}")
    return value


def _count(value: object, at: str, refuse: _Refuse) -> int | None:
    """An integer check argument (a length or a record count)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise refuse(at, f"expected an integer, found {value!r}")
    return value


def _load_pandera_file(file: Path, typename: str) -> OutputSchema:
    def refuse(key: str, why: str) -> OutputSchemaDeclarationError:
        return OutputSchemaDeclarationError(
            message=f"{file}: {key}: {why}",
            field=key,
            value_summary=str(file),
        )

    with open(file, "rb") as handle:
        document = _mapping(yaml.safe_load(handle))
    if document is None:
        raise refuse("<root>", "not a YAML mapping")
    schema_type = document.get("schema_type", "dataframe")
    if schema_type != "dataframe":
        raise refuse(
            "schema_type", f"only 'dataframe' is supported, found {schema_type!r}"
        )
    api = document.get("api", "pandas")
    if api != "pandas":
        raise refuse("api", f"only the pandas API is supported, found {api!r}")
    for key in document:
        if key not in _TOP_LEVEL_KEYS:
            raise refuse(key, "unknown top-level pandera key")
    for key in _MUST_BE_FALSY:
        if document.get(key):
            raise refuse(
                key, f"set to {document[key]!r}, which this reader does not translate"
            )

    min_records, max_records = _record_count_checks(document.get("checks"), refuse)

    columns = _mapping(document.get("columns") or {})
    if columns is None:
        raise refuse("columns", "must be a mapping of column name to spec")
    rules = tuple(
        _column_rule(name, spec or {}, refuse) for name, spec in columns.items()
    )
    return OutputSchema(
        typename=typename,
        fields=rules,
        min_records=min_records,
        max_records=max_records,
    )


def _record_count_checks(
    checks: object, refuse: _Refuse
) -> tuple[int | None, int | None]:
    min_records: int | None = None
    max_records: int | None = None
    for name, args in _iter_checks(checks, refuse, "checks"):
        at = f"checks.{name}"
        count = _count(
            _check_value_arg(args, ("value", "expected_record_count")), at, refuse
        )
        if count is None:
            raise refuse(at, "no record count given")
        if name == "check_record_count_ge":
            min_records = count
        elif name == "check_record_count_le":
            max_records = count
        elif name == "check_record_count_eq":
            min_records = max_records = count
        else:
            raise refuse(at, "schema-level check this reader does not translate")
    return min_records, max_records


@dataclass
class _ColumnValues:
    """Value rules gathered from one column's pandera checks."""

    allowed: tuple[Scalar, ...] | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: float | None = None
    exclusive_maximum: float | None = None


def _column_rule(name: str, raw_spec: object, refuse: _Refuse) -> FieldRule:
    where = f"columns.{name}"
    spec = _mapping(raw_spec)
    if spec is None:
        raise refuse(where, "column spec must be a mapping")
    for key in ("coerce", "regex", "drop_invalid_rows"):
        if spec.get(key):
            raise refuse(
                f"{where}.{key}", "set to true, which this reader does not translate"
            )

    dtype = spec.get("dtype")
    if dtype is None:
        logical: ArtifactFieldTypeExtended = "any"
    elif str(dtype) in _PANDERA_DTYPES:
        logical = _PANDERA_DTYPES[str(dtype)]
    else:
        raise refuse(f"{where}.dtype", f"no logical-type mapping for {dtype!r}")

    nullable = bool(spec.get("nullable", False))
    required = bool(spec.get("required", True)) and not nullable
    values = _ColumnValues()

    for check, args in _iter_checks(spec.get("checks"), refuse, f"{where}.checks"):
        _translate_check(check, args, f"{where}.checks.{check}", values, refuse)
    # pandera reads any other column key as check shorthand — ``equal_to: X``
    # beside ``dtype`` is enforced exactly as it would be under ``checks``.
    for check, args in spec.items():
        if check not in _COLUMN_KEYS:
            _translate_check(check, args, f"{where}.{check}", values, refuse)

    return FieldRule(
        path=name,
        type=logical,
        required=required,
        nullable=nullable,
        unique=bool(spec.get("unique", False)),
        allowed=values.allowed,
        pattern=values.pattern,
        min_length=values.min_length,
        max_length=values.max_length,
        minimum=values.minimum,
        maximum=values.maximum,
        exclusive_minimum=values.exclusive_minimum,
        exclusive_maximum=values.exclusive_maximum,
    )


def _translate_check(
    check: str, args: object, at: str, values: _ColumnValues, refuse: _Refuse
) -> None:
    """Fold one pandera column check into ``values``, or refuse it."""
    if check == "isin":
        options = _check_value_arg(args, ("value", "allowed_values"))
        if not isinstance(options, list):
            raise refuse(at, f"expected a list, found {options!r}")
        values.allowed = tuple(
            _scalar(o, at, refuse) for o in cast("list[object]", options)
        )
    elif check == "equal_to":
        values.allowed = (_scalar(_check_value_arg(args, ("value",)), at, refuse),)
    elif check in ("str_matches", "str_startswith"):
        text = _check_value_arg(args, ("value", "pattern", "string"))
        if not isinstance(text, str):
            raise refuse(at, f"expected a string, found {text!r}")
        # pandera matches at the start only (``re.match``); FieldRule.pattern
        # matches the whole value, so a trailing any-suffix keeps the semantics.
        body = f"(?:{text})" if check == "str_matches" else re.escape(text)
        values.pattern = f"{body}(?s:.*)"
    elif check == "str_length":
        bounds = _mapping(args)
        if bounds is None:
            raise refuse(at, "expected min_value / max_value")
        values.min_length = _count(bounds.get("min_value"), at, refuse)
        values.max_length = _count(bounds.get("max_value"), at, refuse)
    elif check in ("greater_than", "gt"):
        values.exclusive_minimum = _number(
            _check_value_arg(args, ("value", "min_value")), at, refuse
        )
    elif check in ("greater_than_or_equal_to", "ge"):
        values.minimum = _number(
            _check_value_arg(args, ("value", "min_value")), at, refuse
        )
    elif check in ("less_than", "lt"):
        values.exclusive_maximum = _number(
            _check_value_arg(args, ("value", "max_value")), at, refuse
        )
    elif check in ("less_than_or_equal_to", "le"):
        values.maximum = _number(
            _check_value_arg(args, ("value", "max_value")), at, refuse
        )
    elif check == "in_range":
        bounds = _mapping(args)
        if bounds is None:
            raise refuse(at, "expected min_value / max_value")
        low = _number(bounds.get("min_value"), at, refuse)
        high = _number(bounds.get("max_value"), at, refuse)
        if bounds.get("include_min", True):
            values.minimum = low
        else:
            values.exclusive_minimum = low
        if bounds.get("include_max", True):
            values.maximum = high
        else:
            values.exclusive_maximum = high
    else:
        raise refuse(at, "column check this reader does not translate")


def _scalar(value: object, at: str, refuse: _Refuse) -> Scalar:
    if isinstance(value, (str, int, float, bool)):
        return value
    raise refuse(at, f"expected a scalar, found {value!r}")


def _iter_checks(
    checks: object, refuse: _Refuse, where: str
) -> Iterator[tuple[str, object]]:
    """Yield ``(check name, args)`` from either shape pandera writes.

    The mapping shape (``isin: {value: [...]}``) and the list shape
    (``- {value: ..., options: {check_name: isin}}``).
    """
    if checks is None:
        return
    as_mapping = _mapping(checks)
    if as_mapping is not None:
        yield from as_mapping.items()
        return
    if not isinstance(checks, list):
        raise refuse(where, f"expected a mapping or list of checks, found {checks!r}")
    for raw_item in cast("list[object]", checks):
        item = _mapping(raw_item)
        if item is None:
            raise refuse(where, f"list entry is not a mapping: {raw_item!r}")
        options = _mapping(item.get("options")) or {}
        name = options.get("check_name")
        if not isinstance(name, str) or not name:
            raise refuse(where, f"list entry carries no options.check_name: {item!r}")
        yield name, {k: v for k, v in item.items() if k != "options"}


def _check_value_arg(args: object, keys: tuple[str, ...]) -> object:
    """A check's single argument, whether written bare or under a known key."""
    as_mapping = _mapping(args)
    if as_mapping is None:
        return args
    for key in keys:
        if key in as_mapping:
            return as_mapping[key]
    return None
