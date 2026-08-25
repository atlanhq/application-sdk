"""The parquet format validator: a footer diff that reads no rows (ADR-0020).

This is the check the whole capability was named for. A production RCA traced a
73-day frozen lineage marker to one column that had become a string where the
consumer expected a timestamp, with every workflow in the chain reporting success
throughout. Nothing in the pipeline asked the one cheap question that would have
caught it.

:class:`ParquetFooterValidator` asks it. ``pyarrow.parquet.read_schema`` reads the
file **footer** — the schema pyarrow already has to parse to open the file at all —
and the validator diffs the column names and logical types in it against the app's
declaration. **No row group is touched and no row is ever read**, at any file size.
Answering "is ``START_TIME`` a timestamp?" by loading rows into a dataframe pays a
dataframe to do a metadata lookup; the two orthogonal seams in
:mod:`application_sdk.validation.protocols` exist so that cost never lands on a
caller that only ever sees JSON.

**pyarrow is an extra, not core** (``sql`` and ``incremental`` in
``pyproject.toml``). It is therefore imported *inside*
:meth:`ParquetFooterValidator.validate`, and both an absent install and a broken one
degrade to a warning plus an ``unsupported`` outcome — the same skip-with-warning
shape :mod:`application_sdk.validation.assets` uses for the optional ``rocksdict``.
A module-level import here would put a parquet reader on the import path of every
JSON-only caller, which is what
``tests/unit/validation/test_artifact_dependency_floor.py`` exists to prevent.

**Whether the artifact exists is decided before pyarrow is probed.** That is a fact
about the hand-off and needs no reader, so a producer that wrote nothing reports
``absent`` on every install rather than hiding behind our own missing extra.

**A footer establishes carriers, not contents.** Where a declared type asks for more
than metadata can settle — ``json``, whose ADR definition is "``string`` whose
content parses as JSON" — the carrier is asserted and the unparsed half is named on
the report's ``reason``. Reading rows to close that gap is the one thing this
validator will not do, and quietly reporting the partial check as a full one is the
ambiguity the whole capability exists to remove.

**parquet x model is unsupported and says so.**
:meth:`ParquetFooterValidator.supports` answers ``False`` for a
:class:`~application_sdk.validation.artifacts.ModelDeclaration` because a typed
model carries no column mapping, so a footer diff has nothing to diff against. The
wrapper turns that into an ``unsupported`` outcome. Guessing a mapping, or going
quiet, are both worse than saying so.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Iterator, Mapping, Sequence

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.validation.artifacts import (
    FORMAT_PARQUET,
    UNIT_COLUMN,
    ArtifactDeclaration,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
)

logger = get_logger(__name__)

__all__ = ["ParquetFooterValidator"]


PARQUET_SUFFIX: Final = ".parquet"
"""Suffix the parts of a directory-shaped parquet artifact are matched on.

Restated here rather than imported from ``storage/formats/utils`` — that module
pulls in the whole storage stack (and ``pandas`` under ``TYPE_CHECKING``), which
this package's standing dependency floor forbids.
"""


# ---------------------------------------------------------------------------
# The logical -> arrow mapping
# ---------------------------------------------------------------------------
#
# Reviewed once, in ADR-0020, rather than left to become per-validator folklore.
# Values are ``pyarrow.types`` predicate names resolved lazily against whatever
# pyarrow is installed: a name the installed version does not have (the ``*_view``
# types are recent) is skipped, so a newer arrow type widens acceptance where
# pyarrow supports it and never breaks the check where it does not.
#
# The load-bearing row is ``timestamp``: ``is_timestamp`` is true for
# ``timestamp[*]`` at **any unit, tz-aware or not**, and no string predicate appears
# on that row. That single asymmetry is the RCA check.

_STRING_PREDICATES: Final = ("is_string", "is_large_string", "is_string_view")

_TYPE_PREDICATES: Final[Mapping[str, tuple[str, ...]]] = {
    "string": _STRING_PREDICATES,
    "int": ("is_integer",),  # every int* and uint* width
    "float": ("is_floating",),  # float16/32/64
    "decimal": ("is_decimal",),  # decimal128/256 — a Snowflake NUMBER lands here
    "bool": ("is_boolean",),
    "timestamp": ("is_timestamp",),  # any unit, tz-aware or not
    "date": ("is_date",),  # date32/date64
    "time": ("is_time",),  # time32/time64
    "binary": (
        "is_binary",
        "is_large_binary",
        "is_fixed_size_binary",
        "is_binary_view",
    ),
    # ADR-0020 defines parquet `json` as "`string` whose content parses as JSON".
    # Only the carrier is a metadata question, so only the carrier is asserted — a
    # payload that became a struct or an int is still caught, which is what keeps
    # `json` from collapsing into `string`. The unparsed half is not passed off as a
    # full check: every `json` pass is recorded on `_ScanNotes.carrier_only` and
    # named on the report's `reason`.
    "json": _STRING_PREDICATES,
    "array": (
        "is_list",
        "is_large_list",
        "is_fixed_size_list",
        "is_list_view",
        "is_large_list_view",
    ),
    "struct": ("is_struct",),
    "map": ("is_map",),
}
"""Declared logical type -> the ``pyarrow.types`` predicates that satisfy it.

``any`` is deliberately absent: it asserts presence only, and shares that path with
an unrecognised member from a newer toolkit."""

_STRUCT_PREDICATE: Final = ("is_struct",)


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParquetFooterValidator:
    """Diff a parquet artifact's footer schema against a field-map declaration.

    Registered by
    :func:`~application_sdk.validation.wrapper.builtin_format_validators`; construct
    one directly only to pin the validator set under test.

    Units are **columns**
    (:data:`~application_sdk.validation.artifacts.UNIT_COLUMN`): one unit per
    declared field per part read. ``total``/``passed`` therefore describe the whole
    artifact including every part, and ``len(failures)`` equals ``failed`` exactly —
    unlike the streaming formats, a column cannot fail twice.

    A directory is validated part by part, matching the ``**/*.parquet`` shape SDK
    parquet hand-offs are written in. Every part is read; the scan is never sampled,
    because a drift that only reached the later parts is exactly the drift worth
    catching.
    """

    @property
    def artifact_format(self) -> str:
        """``parquet`` — the telemetry identifier. Not to be reworded once shipped."""
        return FORMAT_PARQUET

    @property
    def unit(self) -> str:
        """``column`` — what ``total``/``passed`` count."""
        return UNIT_COLUMN

    def supports(self, declaration: ArtifactDeclaration) -> bool:
        """True only for a field map. A model carries no column mapping to diff."""
        return isinstance(declaration, FieldMapDeclaration)

    def validate(
        self, path: Path, declaration: ArtifactDeclaration
    ) -> ArtifactValidationReport:
        """Read every part's footer and diff it. Never raises, never reads a row."""
        if not isinstance(declaration, FieldMapDeclaration):
            # Unreachable through the wrapper, which calls `supports()` first. Kept
            # because a direct caller can skip it, and the honest answer is the one
            # `supports()` gives rather than an AttributeError on `.fields`.
            return ArtifactValidationReport.unsupported(
                artifact_format=FORMAT_PARQUET,
                schema_source="",
                reason=(
                    "the parquet validator diffs a field map; "
                    f"{type(declaration).__name__} carries no column mapping"
                ),
            )

        if not declaration.fields:
            # The wrapper rejects this before dispatch, so this arm exists for the
            # direct caller that bypasses it. Without it a readable footer plus a
            # zero-field map is a scan over nothing, which finds nothing, which
            # derives `clean` — a silent pass reporting as declared. Everything
            # below may therefore assume at least one declared column.
            return ArtifactValidationReport.unsupported(
                artifact_format=FORMAT_PARQUET,
                schema_source="",
                reason=(
                    "the declaration names zero columns, so a footer diff would "
                    "report as declared while checking nothing"
                ),
            )

        # Path shape is resolved *before* the optional dependency is probed. Whether
        # an artifact exists is a fact about the artifact and needs no reader, so a
        # missing or empty hand-off must report `absent` on every install — reversing
        # these two would relabel it `unsupported` on a JSON-only box and hide a
        # producer that wrote nothing behind our own missing extra.
        files = _parquet_files(path)
        if not files:
            return ArtifactValidationReport.absent(
                artifact_format=FORMAT_PARQUET,
                reason=f"no parquet file at {path}",
            )

        loaded = _load_pyarrow()
        if isinstance(loaded, BaseException):
            # Installed but unusable — a partial wheel, a missing shared library, an
            # arrow too old to expose `read_schema`. Unlike absence this is *not* an
            # expected shape, so `_load_pyarrow` has already logged it with its
            # traceback; it still degrades rather than raising, because `validate()`
            # documents that it never raises and a direct caller has no wrapper to
            # catch it.
            return ArtifactValidationReport.unsupported(
                artifact_format=FORMAT_PARQUET,
                schema_source="",
                reason=(
                    "pyarrow is installed but unusable "
                    f"({type(loaded).__name__}); parquet footers cannot be read"
                ),
            )
        if loaded is None:
            # pyarrow is extra-only, so its absence is benign and expected on a
            # JSON-only install: warn and skip rather than fail the hand-off.
            logger.warning(
                "pyarrow unavailable — skipping parquet footer validation of %s; "
                "install the 'sql' or 'incremental' extra to enable it",
                path,
            )
            return ArtifactValidationReport.unsupported(
                artifact_format=FORMAT_PARQUET,
                schema_source="",
                reason="pyarrow is not installed; parquet footers cannot be read",
            )
        read_schema, arrow_types = loaded

        report = ArtifactValidationReport(artifact_format=FORMAT_PARQUET)
        notes = _ScanNotes()
        first_read_error = ""
        readable = 0

        for file in files:
            try:
                schema = read_schema(file)
            except Exception as exc:  # noqa: BLE001 - any reader failure is one part
                # One corrupt part must not throw away every other part's verdict,
                # so this is per-file: each declared column in it is a unit that
                # could not be judged, not a unit that passed.
                first_read_error = first_read_error or f"{type(exc).__name__}: {exc}"
                report.total += len(declaration.fields)
                report.failures.extend(
                    _undecodable_failures(declaration.fields, file=file, detail=exc)
                )
                continue

            readable += 1
            report.total += len(declaration.fields)
            for field in declaration.fields:
                failure = _check_field(
                    field, schema, arrow_types, file=file, notes=notes
                )
                if failure is not None:
                    report.failures.append(failure)

        if readable == 0:
            # Nothing was readable at all. That is a statement about the artifact,
            # not about any column in it.
            return ArtifactValidationReport.absent(
                artifact_format=FORMAT_PARQUET,
                reason=f"no readable parquet footer at {path}: {first_read_error}",
            )

        report.passed = report.total - len(report.failures)
        report.reason = notes.reason()
        return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _ScanNotes:
    """Assertions a footer read could only make *partially*, gathered across a scan.

    Neither kind is a failure — the artifact is not at fault for either — but both
    would otherwise be indistinguishable from a full check, and "reported clean"
    silently meaning "checked less than you asked for" is the exact ambiguity this
    capability exists to remove. They surface on the report's ``reason``, which is an
    attribute of the outcome event, so the weakening is queryable rather than
    folklore.
    """

    unmapped_types: set[str] = dataclasses.field(default_factory=set)
    """Declared types this validator has no parquet mapping for — *our* gap.

    Presence is asserted, the type is not. Flagging would blame the app for a
    mapping the SDK has not written; going quiet is worse.
    """

    carrier_only: set[str] = dataclasses.field(default_factory=set)
    """Fields declared ``json``, whose *content* a footer read cannot establish.

    ADR-0020 defines parquet ``json`` as "``string`` whose content parses as JSON",
    and only the first half of that is a metadata question. Parsing the second half
    means reading rows, which this validator never does at any file size — so the
    carrier is asserted (a payload that became a struct or an int is still caught,
    which is what keeps ``json`` from collapsing into ``string``) and the unparsed
    half is declared here rather than passed off as a full check.
    """

    def reason(self) -> str:
        """One line naming every partial assertion, or "" when all were complete."""
        clauses = []
        if self.unmapped_types:
            clauses.append(
                f"presence checked but type not asserted for "
                f"{len(self.unmapped_types)} declared type(s) this parquet "
                f"validator has no mapping for: "
                f"{', '.join(sorted(self.unmapped_types))}"
            )
        if self.carrier_only:
            clauses.append(
                f"string carrier checked but JSON content not parsed for "
                f"{len(self.carrier_only)} json field(s) — a footer read "
                f"establishes the carrier, never the content: "
                f"{', '.join(sorted(self.carrier_only))}"
            )
        return "; ".join(clauses)


def _load_pyarrow() -> tuple[Callable[..., object], object] | BaseException | None:
    """``(read_schema, pyarrow.types)``, or why it is unavailable.

    Three answers, because the caller must report three different things:

    * the pair — pyarrow is usable;
    * ``None`` — pyarrow is **not installed**. Benign and expected: it is an extra,
      so this is a normal install shape, not a fault, and carries no traceback.
    * the exception — pyarrow is installed and **unusable**: a partial wheel, a
      missing shared library, an arrow too old to expose ``read_schema``. That is a
      genuine fault, so it is logged here with its traceback (the only place with a
      live one) and handed back so the caller can name it without logging twice.

    Collapsing the last two into ``None`` would report a broken install as a missing
    one and send someone to install a package they already have.

    Imported here rather than at module scope so a JSON-only caller never loads a
    parquet reader, and resolved through the module on every call rather than bound
    once, so a test can spy on ``pyarrow.parquet.read_schema`` at its own module
    path — which is how the "no rows are read" claim is asserted against the *path
    taken* instead of against pyarrow's own behaviour. After the first call the
    import is a ``sys.modules`` lookup.
    """
    try:
        import pyarrow.parquet  # noqa: PLC0415 — optional dep: pyarrow
        from pyarrow import types  # noqa: PLC0415 — optional dep: pyarrow

        # Resolved inside the try: an arrow whose `parquet` module exists but has no
        # `read_schema` is an incompatible install, not a missing one, and belongs on
        # the same degrade path as a half-imported wheel rather than raising an
        # AttributeError out of a function documented never to raise.
        loaded = (pyarrow.parquet.read_schema, types)
    # NB: keep the directive short enough that ruff-format leaves this on one line —
    # E008 anchors on the `except` line, and a wrapped `except (\n ImportError\n):`
    # moves the anchor off the comment and silently un-suppresses the finding.
    except ImportError:  # conformance: ignore[E008] pyarrow is an optional extra
        # Benign: pyarrow is extra-only, so this is an expected install shape and
        # not an error. The warning belongs to the caller, which knows *which*
        # artifact is being skipped and emits it outside any except block so the
        # ImportError traceback stays out of the log — exactly the shape the
        # rocksdict degrade in `validation/assets.py` uses.
        return None  # conformance: ignore[E007] optional-dep probe; the caller warns and reports `unsupported`
    except Exception as exc:  # noqa: BLE001 - a broken install must never reach the hand-off
        # Not benign, so unlike absence it keeps its traceback. Anything an
        # incompatible pyarrow raises on import lands here; the alternative is this
        # escaping into a hand-off that a direct caller has no wrapper to catch.
        logger.warning(
            "pyarrow is installed but could not be loaded, so parquet footers "
            "cannot be read: %s",
            exc,
            exc_info=True,
        )
        return exc
    return loaded


def _parquet_files(path: Path) -> tuple[Path, ...]:
    """Every part of the artifact at ``path``, in a stable order.

    A file is itself; a directory is its ``**/*.parquet`` parts, matching how SDK
    parquet hand-offs are written. A named file is read whatever its suffix — the
    caller said that is the artifact, and second-guessing it from the suffix is the
    path-shape inference this capability exists to avoid.
    """
    try:
        if path.is_file():
            return (path,)
        if path.is_dir():
            return tuple(sorted(path.rglob(f"*{PARQUET_SUFFIX}")))
    except OSError as exc:
        # Not an expected shape — a permission or filesystem fault, not "the app
        # wrote nothing" — so it keeps its traceback.
        logger.warning(
            "Artifact validation: could not stat parquet artifact %s: %s",
            path,
            exc,
            exc_info=True,
        )
    return ()


def _undecodable_failures(
    fields: Sequence[DeclaredField], *, file: Path, detail: BaseException
) -> Iterator[ArtifactValidationFailure]:
    """One ``undecodable`` unit per declared column of an unreadable part.

    Per-column rather than per-part so ``total - passed`` stays equal to
    ``len(failures)``: every declared column in that part genuinely went unchecked.

    ``fields`` is never empty — a zero-field declaration is rejected as
    ``unsupported`` before any part is opened — so a corrupt part always yields at
    least one failure and can never round down to a clean report.
    """
    message = f"{type(detail).__name__}: {detail}"
    for field in fields:
        yield ArtifactValidationFailure(
            kind="undecodable",
            field=field.path,
            file=str(file),
            errors=[message],
        )


def _check_field(
    field: DeclaredField,
    schema: object,
    arrow_types: object,
    *,
    file: Path,
    notes: _ScanNotes,
) -> ArtifactValidationFailure | None:
    """Check one declared field against one footer schema, or ``None`` if it holds.

    Records any *partial* assertion on ``notes`` — a type with no mapping, or a
    ``json`` field whose content a footer cannot reach — so the report can say which
    of its passes were complete.
    """
    arrow_type = _resolve(schema, field.path, arrow_types)
    if arrow_type is None:
        if not field.required:
            return None
        return ArtifactValidationFailure(
            kind="missing",
            field=field.path,
            expected=field.type,
            file=str(file),
            errors=[f"declared column {field.path!r} is not in the parquet footer"],
        )

    predicates = _TYPE_PREDICATES.get(field.type)
    if predicates is None:
        # `any`, or an extension member from a newer toolkit this SDK has no parquet
        # mapping for. Presence has been asserted; the type has not, and the caller
        # is told which is which through the report's `reason`. `any` is excluded
        # because it asserts presence *by definition* — declaring it is not a gap.
        if field.type != "any":
            notes.unmapped_types.add(field.type)
        return None

    if _satisfies(arrow_type, predicates, arrow_types):
        if field.type == "json":
            # The carrier held. The content is a row-level question this validator
            # never asks, so record the half that was not established rather than
            # letting a `json` pass read as identical to a `string` pass — the
            # collapse ADR-0020 warns defeats a JSON-blob hop's check.
            notes.carrier_only.add(field.path)
        return None
    return ArtifactValidationFailure(
        kind="type_mismatch",
        field=field.path,
        expected=field.type,
        actual=str(arrow_type),
        file=str(file),
        errors=[
            f"column {field.path!r} is {arrow_type}, which does not satisfy the "
            f"declared type {field.type!r}"
        ],
    )


def _satisfies(
    arrow_type: object, predicates: Sequence[str], arrow_types: object
) -> bool:
    """Whether any named ``pyarrow.types`` predicate accepts ``arrow_type``.

    A predicate the installed pyarrow does not expose is skipped rather than being
    an error: the ``*_view`` types are recent, and a validator that refused to run
    on an older arrow would take the whole check offline to gain nothing.
    """
    for name in predicates:
        predicate = getattr(arrow_types, name, None)
        if predicate is not None and predicate(arrow_type):
            return True
    return False


def _resolve(schema: object, declared_path: str, arrow_types: object) -> object | None:
    """The arrow type at ``declared_path``, or ``None`` when the column is absent.

    An exact top-level match wins first, so a parquet file whose column is literally
    named ``payload.rows`` resolves to that column rather than being read as a walk
    into a struct. Only when no such column exists is the dotted path walked through
    ``struct`` children — the nested addressing ADR-0020 chose over a recursive type
    grammar.
    """
    exact = _child(schema, declared_path)
    if exact is not None:
        return exact.type
    if "." not in declared_path:
        return None

    current: object = schema
    parts = declared_path.split(".")
    for index, part in enumerate(parts):
        child = _child(current, part)
        if child is None:
            return None
        if index == len(parts) - 1:
            return child.type
        current = child.type
        if not _satisfies(current, _STRUCT_PREDICATE, arrow_types):
            return None
    return None


def _child(container: object, name: str) -> object | None:
    """First field named ``name`` on a ``pa.Schema`` or ``pa.StructType``.

    Scanned rather than looked up through ``Schema.field(name)`` so duplicate column
    names — legal in parquet — resolve to the first occurrence deterministically
    instead of depending on that lookup's behaviour.

    ``container`` is only ever a schema or a type :func:`_resolve` has already
    confirmed is a struct, and both iterate their fields. There is deliberately no
    guard for anything else: swallowing a ``TypeError`` here would turn "this SDK
    walked a type it did not expect" into a column silently reported missing, and
    the wrapper already fails the whole call open.
    """
    # `container` is typed `object` because the arrow types are only imported inside
    # `_load_pyarrow`, so there is no annotation to narrow it to. Both `pa.Schema`
    # and `pa.StructType` iterate their fields at runtime.
    for field in container:  # type: ignore[attr-defined]  # Schema/StructType are iterable
        if getattr(field, "name", None) == name:
            return field
    return None
