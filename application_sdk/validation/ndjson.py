"""The NDJSON format validator, and the shared NDJSON walk (ADR-0020).

This is the cheap half of the two-seam design. NDJSON is checked by streaming the
artifact line by line and asserting, per record, that every declared field is
present and carries a JSON value the declared logical type accepts. One pass,
constant memory, **zero new dependencies** — stdlib plus ``orjson``, which is
already core. A caller that only ever sees JSON must never load a dataframe
library to answer "is ``START_TIME`` a timestamp?", and
``tests/unit/validation/test_ndjson_validator.py`` proves that of the real call
path rather than of the import graph alone.

**Memory.** The scan holds one record at a time, so peak memory tracks the widest
record rather than the artifact — a directory of 20k records across many files
peaks the same as a directory of 200. The one thing that does grow is the failure
list, exactly as :class:`~application_sdk.validation.assets.AssetValidationReport`
grows its own: an artifact where *every* record fails holds one failure per failing
record. That is the report's stated contract — the failure list is unbounded and
the two *output* surfaces are what the shared cap bounds — and it is what keeps
``undecodable``, derived from that list, exact.

**Every record is scanned.** There is no sampling and no early exit, so the scalar
counts always describe the whole artifact. A record that fails to decode is counted
as ``undecodable`` and the scan continues, the same posture the asset path takes:
one malformed line must not abort the batch and take every other record's verdict
with it.

**One walk, not two.** :func:`iter_ndjson_lines` is the single NDJSON traversal in
the tree. It was lifted here from :mod:`application_sdk.validation.assets`, which
now imports it, because it was already format-generic — it yields
``(file, 1-based line, raw bytes)``, accepts a file or a directory, and skips
blanks. A second walk would be a second set of decisions about blank lines, file
ordering and directory recursion to keep in sync.

**Which cells this is.** Both NDJSON cells, because the wrapper dispatches on
*format*: it picks the validator claiming ``ndjson`` and then asks it which kinds of
declaration it can check, so a second ndjson-claiming validator would never be
reached. :meth:`NdjsonValidator.validate` splits them.

* NDJSON x ``ContractSource`` — a per-record declared-key presence and JSON type
  check, the code in this module.
* NDJSON x ``ModelSource`` — per-record decode plus the model's own ``.validate()``,
  which is :func:`~application_sdk.validation.assets.validate_transformed_dir`. It
  predates the wrapper and FND-690 folded it in behind this seam rather than
  reimplementing it, so the check, its isolation posture and its shipped outcome
  event are all unchanged — only the way it is reached is new. The delegation is a
  **deferred import, and only circularity requires it**: ``assets.py`` imports
  :func:`iter_ndjson_lines` from this module at module scope, so a module-level
  import back would cycle (measured: ``ImportError``, partially initialized
  module). It buys no dependency-floor benefit — this package's ``__init__``
  imports ``assets`` at module scope, so ``pyatlan_v9`` is already loaded by the
  time anything can import this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Final, Iterator, Mapping

import orjson

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.validation.artifacts import (
    FORMAT_NDJSON,
    UNIT_RECORD,
    ArtifactDeclaration,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
)

logger = get_logger(__name__)

__all__ = ["NdjsonValidator", "iter_ndjson_lines"]


# ---------------------------------------------------------------------------
# The shared NDJSON walk
# ---------------------------------------------------------------------------


#: Filename suffixes the walk treats as NDJSON record parts, lower-cased.
#:
#: **One tuple, both branches.** The directory walk and the single-file check
#: read it from here precisely so they cannot drift apart again — that drift is
#: the defect this constant exists to prevent (see :func:`iter_ndjson_lines`).
#:
#: ``.json`` is what the transform stage writes today. ``.jsonl`` and
#: ``.ndjson`` are the two conventional names for the same
#: one-record-per-line format, accepted so an app that follows either
#: convention is validated rather than silently skipped. All three are
#: read identically — the suffix selects files, it does not change parsing.
NDJSON_SUFFIXES: Final[tuple[str, ...]] = (".json", ".jsonl", ".ndjson")


def iter_ndjson_lines(path: str | Path) -> Iterator[tuple[str, int, bytes]]:
    """Yield ``(file, 1-based line number, raw bytes)`` for every non-blank line.

    Accepts a directory (walked recursively, sorted for stable ordering) or a
    single file. Either way only files whose suffix is in
    :data:`NDJSON_SUFFIXES` are read. A missing path yields nothing.

    **Both branches apply the same suffix rule, and that symmetry is
    load-bearing.** The single-file branch used to accept any file, while the
    directory branch globbed ``*.json`` — so the same subtree validated
    differently depending on whether the caller named the directory or one file
    inside it.

    That asymmetry produced false validation findings in production. The upload
    hook's ``_resolve_transformed_target`` returns any single file whose path
    contains ``transformed``, so a connector calling
    ``upload(".../transformed/transformed-count.txt")`` handed this walk a
    sidecar containing a bare record count. It was read as NDJSON, the integer
    failed to decode as an asset, and the run reported one phantom
    ``undeserializable`` — on every run, for a file that is not an asset part at
    all and that the directory walk had always correctly ignored.

    A file with an unrecognised suffix is therefore skipped rather than parsed.
    The caller sees "nothing to validate" (the wrapper's ``absent`` outcome),
    which is the honest answer for a path that holds no record parts — strictly
    better than inventing a failure.

    The directory branch walks once and keeps files whose case-folded suffix is
    in :data:`NDJSON_SUFFIXES`, sorting the result so ordering stays stable and
    independent of which extensions a given app happens to write. Both branches
    read the same tuple through the same case-folded test, which is what keeps
    them from diverging again — including on case, where a per-suffix glob would
    be case-sensitive on POSIX and would accept ``PART-0.JSON`` only when the
    caller named the file rather than its parent directory.
    """
    root = Path(path)
    if root.is_dir():
        # One walk, filtered by the same case-folded suffix test the file
        # branch uses — a glob per suffix would be POSIX case-sensitive, so
        # PART-0.JSON would be read when named directly and skipped when the
        # caller named its parent directory. Sorted once at the end so
        # ordering is stable and independent of which extensions an app
        # happens to write.
        files = sorted(
            str(candidate)
            for candidate in root.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in NDJSON_SUFFIXES
        )
    elif root.is_file():
        if root.suffix.lower() not in NDJSON_SUFFIXES:
            # Debug, not warning: a sidecar next to the parts is normal, and the
            # upload path can legitimately point here. It must not read as a
            # fault on every run.
            logger.debug(
                "Skipping file with non-record suffix in NDJSON walk "
                "(not a record part): %s",
                root,
            )
            files = []
        else:
            files = [str(root)]
    else:
        files = []
    for file_path in files:
        with open(file_path, "rb") as handle:
            for line_no, raw in enumerate(handle, start=1):
                stripped = raw.strip()
                if stripped:
                    yield file_path, line_no, stripped


# ---------------------------------------------------------------------------
# The logical-type -> JSON mapping
# ---------------------------------------------------------------------------
#
# Straight from the ADR's per-format mapping table, which was reviewed once there
# precisely so it would not become per-validator folklore. The load-bearing row is
# ``timestamp``: JSON has no timestamp type at all, so a timestamp always arrives
# as a string or a number, and the check that matters is whether that string is
# actually ISO-8601 rather than free-form text. That is the NDJSON expression of
# the distinction the whole capability exists to make — a production RCA traced a
# 73-day frozen lineage marker to one column that had become a string where the
# consumer expected a timestamp, with every workflow in the chain reporting
# success throughout.
#
# JSON ``null`` never reaches these: it is handled first and always passes. See
# ``NdjsonValidator.validate``.


def _is_integral(value: object) -> bool:
    """JSON number with no fractional part.

    ``bool`` is excluded explicitly. Python makes ``bool`` a subclass of ``int``,
    so a bare ``isinstance(value, int)`` would quietly accept ``true`` for an
    ``int`` field — a type confusion this check exists to catch.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """Any JSON number. ``int`` satisfies ``float``: JSON has one number type."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_DECIMAL_TEXT: Final = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
"""Shape of a decimal carried as a string.

The ADR accepts a string carrier for ``decimal`` because a string is lossless where
a double is not — a Snowflake ``NUMBER`` round-trips exactly through one and not
the other. That is an argument for *numeric text*, not for any text, so the string
has to look like a number; otherwise a ``decimal`` field carrying prose would pass.
"""

_BASE64_TEXT: Final = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
)
"""Shape of base64 text, checked without decoding it.

Deliberately a shape check: ``base64.b64decode`` would allocate the decoded bytes
for every record, which is the one thing a constant-memory streaming scan must not
do — and for the field type whose whole purpose is carrying a large blob.
"""


def _is_decimal(value: object) -> bool:
    return _is_number(value) or (
        isinstance(value, str) and _DECIMAL_TEXT.match(value) is not None
    )


def _is_base64(value: object) -> bool:
    return isinstance(value, str) and _BASE64_TEXT.match(value) is not None


def _parses_as(parser: Callable[[str], object], value: object) -> bool:
    """True when ``value`` is a string ``parser`` accepts.

    ``datetime.fromisoformat`` and its ``date``/``time`` siblings are the stdlib's
    ISO-8601 readers, and since 3.11 they cover the forms that actually appear in
    warehouse output, including a trailing ``Z``. Using them keeps the dependency
    floor at zero.
    """
    if not isinstance(value, str):
        return False
    try:
        parser(value)
    except ValueError:
        # conformance: ignore[E007] the raise IS the answer, and it is reported — a rejected value becomes a type_mismatch failure naming the field, the declared type and the observed one; logging would be one line per field per record on a mismatched column
        return False
    return True


_DATETIME_SEPARATORS: Final = ("T", "t", " ")
"""Characters ISO-8601 allows between the date and the time.

Required *before* parsing, because ``datetime.fromisoformat`` is happy to read a
date-only string as midnight: without this gate ``"2026-08-25"`` would satisfy a
declared ``timestamp``, while the ``date`` check already rejects
``"2026-08-25T10:11:12"``. The ADR maps ``date`` and ``timestamp`` as separate
rows, and a one-directional acceptance is how two logical types quietly collapse
into one.

It costs nothing in coverage: every datetime form ``fromisoformat`` accepts carries
one of these — extended (``2026-08-25T10:11:12``), space-separated, lowercase
``t``, and basic (``20260825T101112``) — and the separator-less ``20260825101112``
is rejected by the stdlib on every version in the matrix anyway. Requiring it also
makes the answer independent of how lenient a given Python is about *which*
character sits in that position.
"""


def _is_timestamp(value: object) -> bool:
    """ISO-8601 date-time string or epoch number — the ADR's two carriers.

    A date-only string is **not** a timestamp here; see
    :data:`_DATETIME_SEPARATORS`.

    Epoch numbers are accepted without a range check: the vocabulary distinguishes
    a timestamp from a *string*, not epoch-seconds from epoch-millis, and guessing
    the unit from magnitude is how a validator starts asserting things the
    declaration never said.
    """
    if _is_number(value):
        return True
    if not isinstance(value, str) or not any(
        sep in value for sep in _DATETIME_SEPARATORS
    ):
        return False
    return _parses_as(datetime.fromisoformat, value)


def _is_date(value: object) -> bool:
    return _parses_as(date.fromisoformat, value)


def _is_time(value: object) -> bool:
    return _parses_as(time.fromisoformat, value)


def _is_json(value: object) -> bool:
    """A nested object/array, or a JSON blob carried in a string.

    The JSON-in-string arm re-parses, which is why ``json`` is the one logical type
    whose check is not O(1) in the value's size. It is also the only way to tell the
    hop this member exists for — a column that is physically a string and
    semantically JSON — from a column that is merely a string.
    """
    if isinstance(value, (dict, list)):
        return True
    if not isinstance(value, str):
        return False
    try:
        orjson.loads(value)
    except orjson.JSONDecodeError:
        # conformance: ignore[E007] as in `_parses_as` — the decode failing is the predicate's answer, it surfaces as a reported type_mismatch, and logging it would be one line per record
        return False
    return True


_CHECKERS: Final[Mapping[str, Callable[[object], bool]]] = {
    # The stable floor: every one of these must stay mapped.
    "string": lambda v: isinstance(v, str),
    "int": _is_integral,
    "float": _is_number,
    "bool": lambda v: isinstance(v, bool),
    "timestamp": _is_timestamp,
    "date": _is_date,
    "json": _is_json,
    "any": lambda v: True,
    # Extension members. Present because NDJSON can map them; a member this
    # validator cannot map degrades to presence-only rather than failing the
    # file — see :meth:`NdjsonValidator._plan`.
    "decimal": _is_decimal,
    "binary": _is_base64,
    "time": _is_time,
    "array": lambda v: isinstance(v, list),
    "struct": lambda v: isinstance(v, dict),
    "map": lambda v: isinstance(v, dict),
}
"""Logical type -> "does this JSON value satisfy it?"."""


def _json_type_name(value: object) -> str:
    """Name the JSON type of ``value`` for the report's ``actual`` field.

    JSON's vocabulary, not Python's, because that is what the reader is looking at
    in the file. The one place they diverge and it matters is ``bool``, tested
    before ``int`` — Python would otherwise call ``true`` an integer.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# ---------------------------------------------------------------------------
# Dotted-path resolution
# ---------------------------------------------------------------------------

_MISSING: Final = object()
"""Sentinel for "the path does not resolve", kept distinct from a JSON ``null``
that resolved perfectly well."""


def _resolve(record: object, parts: tuple[str, ...]) -> object:
    """Walk a dotted path through nested JSON objects.

    Returns :data:`_MISSING` when any segment is absent, or when a segment has to
    be traversed *through* something that is not an object — ``payload.rows`` where
    ``payload`` is a string resolves to nothing, and saying so is more useful than
    pretending ``rows`` was merely absent. :func:`_stopped_at` reconstructs which
    it was, on the cold path only.
    """
    current = record
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _stopped_at(record: object, parts: tuple[str, ...]) -> str:
    """Human-readable reason a dotted path did not resolve.

    Only ever called while building a failure, so it re-walks rather than making
    the hot path carry a second return value for every field of every record.
    """
    current = record
    walked: list[str] = []
    for part in parts:
        if not isinstance(current, dict):
            where = ".".join(walked) or "the record"
            return f"{where} is {_json_type_name(current)}, not an object"
        if part not in current:
            walked.append(part)
            return f"'{'.'.join(walked)}' is absent"
        walked.append(part)
        current = current[part]
    return "is absent"


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Check:
    """One declared field, pre-resolved for the scan.

    Built once per :meth:`NdjsonValidator.validate` call rather than once per
    record: splitting a dotted path and looking a checker up are per-*declaration*
    facts, and doing them inside the loop would repeat them a few million times for
    no new information.
    """

    path: str
    parts: tuple[str, ...]
    declared_type: str
    required: bool
    checker: Callable[[object], bool] | None
    """``None`` when the declared type has no NDJSON mapping — presence is still
    asserted, the type is not. See :meth:`NdjsonValidator._plan`."""


@dataclass(frozen=True)
class NdjsonValidator:
    """Streams an NDJSON artifact and checks every record against a field map.

    The :class:`~application_sdk.validation.protocols.FormatValidator` for
    :data:`~application_sdk.validation.artifacts.FORMAT_NDJSON`. Registered by
    :func:`~application_sdk.validation.wrapper.builtin_format_validators`, so an
    app gets it without naming it.

    Three behaviours are worth knowing before reading a report it produced:

    * **A JSON ``null`` passes any declared type.** The vocabulary has no
      nullability axis, and the parquet validator diffs a *footer schema*, which
      cannot see nulls at all — an arrow ``timestamp`` column is a timestamp column
      whether or not every value in it is null. Flagging nulls here would make the
      two formats give different verdicts on the same declaration, and would fire
      constantly on real warehouse output. ``required`` therefore means "the key is
      in the record", which is the assertion parquet's column-presence diff makes
      too.
    * **A record that is not a JSON object counts as ``undecodable``**, not as one
      ``missing`` per declared field. A bare array or scalar has no addressable
      fields at all, so the honest report is "this record could not be checked",
      and it keeps ``undecodable`` meaning exactly that across both the decode
      failure and the wrong-shape case.
    * **A declared type with no NDJSON mapping degrades to presence-only**, names
      itself in the report's ``reason``, and logs a warning — it does not fail the
      artifact and does not drop the other fields' assertions. This mirrors the
      loader, which deliberately does not police the type vocabulary so that a
      newer toolkit's type cannot invalidate every other assertion in the file.
    """

    @property
    def artifact_format(self) -> str:
        """``ndjson`` — the telemetry identifier. Not to be reworded once shipped."""
        return FORMAT_NDJSON

    @property
    def unit(self) -> str:
        """``record`` — this validator counts records, not columns."""
        return UNIT_RECORD

    def supports(self, declaration: ArtifactDeclaration) -> bool:
        """True for a field-map declaration, and for a delegatable model.

        Both NDJSON cells live behind this one validator, because dispatch is by
        *format*: the wrapper picks the validator whose ``artifact_format`` matches
        and then asks it which kinds of declaration it can check. A second
        ndjson-claiming validator would never be reached.

        A model declaration answers True only when the model is one this cell can
        actually decode into — see
        :func:`~application_sdk.validation.assets.supports_asset_model`. A model that
        merely *looks* delegatable to
        :class:`~application_sdk.validation.sources.ModelSource` (any class with a
        callable ``validate``) gets a reported ``unsupported``, not a scan that
        reports every record as undecodable.
        """
        if isinstance(declaration, FieldMapDeclaration):
            return True
        if isinstance(declaration, ModelDeclaration):
            from application_sdk.validation.assets import (  # noqa: PLC0415 — deferred because a module-level import would cycle: assets.py imports this module's iter_ndjson_lines at module scope
                supports_asset_model,
            )

            return supports_asset_model(declaration.model)
        return False

    def validate(
        self, path: Path, declaration: ArtifactDeclaration
    ) -> ArtifactValidationReport:
        """Scan every record under ``path`` against ``declaration``.

        Dispatches on which kind of declaration it was handed — the two NDJSON cells
        are the same walk asking a different question of each record, so they share a
        validator and split here:

        * a field map is **diffed** — declared key presence plus JSON type, below;
        * a model is **delegated to** — decode the record into the model and let it
          validate itself, in
          :func:`~application_sdk.validation.assets.validate_assets_as_artifact`.

        Args:
            path: A single NDJSON file, or a directory of ``*.json`` parts.
            declaration: What to check against. Called only after :meth:`supports`
                said yes, so this is a
                :class:`~application_sdk.validation.artifacts.FieldMapDeclaration` or
                a delegatable
                :class:`~application_sdk.validation.artifacts.ModelDeclaration`.

        Returns:
            A report whose scalar counts describe the whole artifact. ``absent``
            when there was nothing to read — a missing path, or a directory with no
            records in it. Reporting that as ``clean`` would be the exact failure
            this capability was built to remove: zero records checked, and a pass
            on the board.
        """
        if isinstance(declaration, ModelDeclaration):
            from application_sdk.validation.assets import (  # noqa: PLC0415 — deferred for the same reason as in supports(): a module-level import would cycle
                validate_assets_as_artifact,
            )

            return validate_assets_as_artifact(path, declaration)

        if not isinstance(declaration, FieldMapDeclaration):
            # Unreachable via the wrapper, which honours ``supports``. Guarded
            # anyway: an app may call a validator directly, and reading ``.fields``
            # off something that is neither declaration would raise into that caller.
            return ArtifactValidationReport.absent(
                reason=(
                    f"the ndjson validator was handed a "
                    f"{type(declaration).__name__}, not a field map or a model"
                ),
            )

        checks, unmapped = self._plan(declaration.fields)
        report = ArtifactValidationReport()
        if unmapped:
            # Named on the event, not only in a log line: a field whose type is not
            # being asserted is exactly the kind of quiet downgrade that otherwise
            # reads as a clean check.
            report.reason = (
                "presence checked but type not asserted for "
                + ", ".join(f"{p}:{t}" for p, t in unmapped)
                + " — no ndjson mapping for that declared type"
            )
            logger.warning(
                "Artifact validation: the ndjson validator has no mapping for "
                "declared type(s) %s; presence is still asserted for those fields",
                ", ".join(f"{p}:{t}" for p, t in unmapped),
            )

        try:
            for file_path, line_no, raw in iter_ndjson_lines(path):
                report.total += 1
                try:
                    record = orjson.loads(raw)
                except orjson.JSONDecodeError as exc:
                    report.failures.append(
                        ArtifactValidationFailure(
                            kind="undecodable",
                            file=file_path,
                            line=line_no,
                            errors=[f"not valid JSON: {exc}"],
                        )
                    )
                    continue

                if not isinstance(record, dict):
                    report.failures.append(
                        ArtifactValidationFailure(
                            # ``actual`` is left unset on purpose: the shared
                            # renderer pairs it with ``expected`` as "declared X,
                            # found Y", and there is no declared type to pair it
                            # with when it is the whole record that is wrong. The
                            # message below names the shape instead.
                            kind="undecodable",
                            file=file_path,
                            line=line_no,
                            errors=[
                                f"record is a JSON {_json_type_name(record)}, not an "
                                f"object, so it carries no addressable fields"
                            ],
                        )
                    )
                    continue

                before = len(report.failures)
                self._check_record(record, checks, file_path, line_no, report)
                if len(report.failures) == before:
                    report.passed += 1
        except OSError as exc:
            # The artifact turned out not to be readable after all. That is an
            # ``absent`` artifact, not a broken validator, and whatever was counted
            # before the failure describes a partial scan nobody should act on.
            logger.warning(
                "Artifact validation: could not read the ndjson artifact at %s: %s",
                path,
                exc,
                exc_info=True,
            )
            return ArtifactValidationReport.absent(
                reason=f"artifact could not be read: {exc}",
            )

        if report.total == 0:
            return ArtifactValidationReport.absent(
                reason=f"no ndjson records found at {path}",
            )
        return report

    # -- internals -------------------------------------------------------

    @staticmethod
    def _plan(
        fields: tuple[DeclaredField, ...],
    ) -> tuple[tuple[_Check, ...], tuple[tuple[str, str], ...]]:
        """Pre-resolve the declaration into per-field checks, once per scan.

        Returns the checks plus every ``(path, type)`` whose declared type this
        validator has no mapping for. Those keep their presence assertion and lose
        only their type assertion — the loader deliberately does not police the type
        vocabulary, on the grounds that one unrecognised type from a newer toolkit
        must not invalidate every other assertion in the file, and dropping the
        field entirely here would undo that at the next layer down.
        """
        checks: list[_Check] = []
        unmapped: list[tuple[str, str]] = []
        for declared in fields:
            checker = _CHECKERS.get(declared.type)
            if checker is None:
                unmapped.append((declared.path, declared.type))
            checks.append(
                _Check(
                    path=declared.path,
                    parts=tuple(declared.path.split(".")),
                    declared_type=declared.type,
                    required=declared.required,
                    checker=checker,
                )
            )
        return tuple(checks), tuple(unmapped)

    @staticmethod
    def _check_record(
        record: dict,
        checks: tuple[_Check, ...],
        file_path: str,
        line_no: int,
        report: ArtifactValidationReport,
    ) -> None:
        """Append a failure for every declared field this record does not satisfy.

        A record can break on several fields at once, which is why this appends
        rather than returning at the first problem: the report's ``failed`` counts
        records and ``len(failures)`` counts problems, and collapsing them would
        lose which fields to go and fix.
        """
        for check in checks:
            value = _resolve(record, check.parts)
            if value is _MISSING:
                if check.required:
                    report.failures.append(
                        ArtifactValidationFailure(
                            kind="missing",
                            field=check.path,
                            expected=check.declared_type,
                            file=file_path,
                            line=line_no,
                            errors=[
                                f"required field '{check.path}' does not resolve: "
                                f"{_stopped_at(record, check.parts)}"
                            ],
                        )
                    )
                continue
            # A JSON null satisfies every declared type — see the class docstring.
            if value is None or check.checker is None or check.checker(value):
                continue
            report.failures.append(
                ArtifactValidationFailure(
                    kind="type_mismatch",
                    field=check.path,
                    expected=check.declared_type,
                    actual=_json_type_name(value),
                    file=file_path,
                    line=line_no,
                    errors=[
                        f"field '{check.path}' is declared {check.declared_type} but "
                        f"carries a JSON {_json_type_name(value)}"
                    ],
                )
            )
