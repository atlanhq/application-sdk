"""One declared layout, one env var, and one loader for in-repo golden corpora.

A golden corpus is the sanitized fixture tree a connector's integration suite
reads when it has no live source to extract from: recorded source payloads to
feed the transform, and the expected records the transform should produce.

Across the connector suites written so far, one concept picked up four directory
names (``extracted/``, ``raw/``, ``processed/``, ``extract/``), six env-var names,
and three different answers to whether there is a tenant level at all. This
module replaces that with a declared shape.

**The layout**::

    $E2E_GOLDEN_ROOT/            # or an in-repo default the suite passes
      [<tenant>/]                # optional — declare tenant_level=True to use it
        raw/                     # the transform's INPUT
        transformed/             # the records the transform should produce

``GoldenLayout``'s default stages are ``("raw", "transformed")`` — a starting
point for a NEW capture, not a description of what an existing corpus looks
like. SDK-app captures write ``raw``/``transformed``; legacy-Argo captures
write ``extracted``/``transformed`` (or the ``-metadata`` suffixed pair).
Every consumer this module has today declares its stages explicitly rather
than relying on the default. Declare yours; do not assume it.

Two rules make the divergence collapse rather than reappear:

* **``raw/`` means "the transform's input"** — not "untouched bytes from the
  source". An app with a genuine post-processing stage between extraction and
  transformation declares ``stages=("raw", "processed", "transformed")`` and
  ``input_stage="processed"``: the stage that feeds the transform becomes a
  stated fact in code, rather than something a reader infers from a test file.
* **The tenant level is optional** and off by default, because several
  connectors have no tenant axis and must not invent a synthetic directory to
  satisfy a loader.

**Scope boundary.** This contract governs the **in-repo fixture tree** only. The
upstream source buckets these corpora were captured from genuinely differ —
legacy Argo writes ``{extracted,transformed}-metadata``, an SDK app writes
``{raw,transformed}`` — and renaming those is neither possible nor intended here.
Capture from whatever the bucket holds; commit it under the layout above.

**Missing versus malformed.** These are different failures and the loader treats
them differently:

* *Not configured* — ``E2E_GOLDEN_ROOT`` unset and no default root exists. The
  corpus is declared absent, so :func:`require_golden_corpus` skips, matching the
  skip-not-fail contract in :mod:`application_sdk.testing.integration.source`.
  One skip idiom, not a module-level ``skipif`` in some repos and an in-test
  helper in others.
* *Configured but wrong* — a root that does not exist, a declared stage
  directory that is missing, a stage holding no files, an unparseable file. Every
  one of these raises with the offending path named. An empty stage is an error,
  never an empty list: a corpus loader that silently yields nothing turns a
  broken fixture tree into a passing test.

**Per-typename subdirectories.** A stage directory is commonly split into one
subdirectory per typename (``extracted/tables/``, ``extracted/reports/``,
...). :meth:`~GoldenCorpus.files` and :meth:`~GoldenCorpus.records` match with
``rglob``, so both accept a path-shaped ``pattern``:
``corpus.records("extracted", pattern="tables/*")`` selects only the
``tables/`` subdirectory — ``records()`` with the default ``pattern="*"``
still concatenates every file under the stage regardless of which subdirectory
it came from, flattening the per-typename identity out of the result. Use
:meth:`~GoldenCorpus.subdirs` to discover what subdirectories a stage holds
without hardcoding typenames.

**On ``validate=False``.** Both of this module's real consumers pass
``validate=False`` to :func:`require_golden_corpus`. Reproducing
``GoldenCorpus.from_env(...).validate()`` against each consumer's committed
fixture tree raises nothing — every declared stage already holds files for
every tenant. Neither consumer is routing around a defect in
:meth:`~GoldenCorpus.validate`: one defers the call until after it has
confirmed the root holds exactly one tenant directory, then calls it
explicitly; the other skips it because its tests iterate
:meth:`~GoldenCorpus.tenants` themselves and read stage contents directly via
:meth:`~GoldenCorpus.stage_dir`, never through :meth:`~GoldenCorpus.records`.
``validate=False`` is a sequencing choice about *when* the stage-completeness
check runs, not evidence that the check itself rejects a legitimate tree
shape.

Formats: JSON, NDJSON (``.ndjson`` / ``.jsonl``), CSV, parquet. Parquet needs
``pyarrow``, which ships in the ``[sql]`` and ``[incremental]`` extras rather
than the SDK core, so it is imported lazily and its absence names the extra.

Comparing what a run produced against the corpus's expected records is a
separate concern and lives elsewhere (see the golden-comparison helpers in
:mod:`application_sdk.testing`); this module only declares the tree and reads it.

Example::

    _LAYOUT = GoldenLayout(
        stages=("raw", "processed", "transformed"),
        input_stage="processed",
        tenant_level=True,
    )

    @pytest.fixture(scope="session")
    def corpus() -> GoldenCorpus:
        return require_golden_corpus(
            layout=_LAYOUT,
            default_root=Path(__file__).parent / "fixtures" / "golden",
        ).for_tenant("tenant-a")

    def test_transform_input_present(corpus: GoldenCorpus) -> None:
        assert corpus.records(corpus.layout.input_stage)
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import orjson

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

GOLDEN_ROOT_ENV = "E2E_GOLDEN_ROOT"
"""Single env var overriding the corpus root.

``E2E_`` because every test-harness env var in :mod:`application_sdk.testing`
uses it (``E2E_SOURCE_AVAILABLE``, ``E2E_TENANT_DEPLOYMENT_NAME``,
``E2E_WORKER_HEALTH_URL``, the ``E2E_<DATASOURCE>_*`` credential family).
``ATLAN_*`` is runtime SDK configuration read into module-level constants — a
different contract that this is not part of.
"""

_JSON_SUFFIXES = frozenset({".json"})
_NDJSON_SUFFIXES = frozenset({".ndjson", ".jsonl"})
_CSV_SUFFIXES = frozenset({".csv"})
_PARQUET_SUFFIXES = frozenset({".parquet"})

SUPPORTED_SUFFIXES = frozenset(
    _JSON_SUFFIXES | _NDJSON_SUFFIXES | _CSV_SUFFIXES | _PARQUET_SUFFIXES
)


@dataclass(frozen=True)
class GoldenLayout:
    """The stage names and tenant axis a connector's corpus declares.

    Args:
        stages: Directory names under the corpus (or tenant) root, in pipeline
            order. Defaults to the two-stage shape most connectors have.
        input_stage: The stage that feeds the transform. Must be one of
            ``stages``.
        tenant_level: Whether a tenant directory sits between the root and the
            stage directories. Off by default.
    """

    stages: tuple[str, ...] = ("raw", "transformed")
    input_stage: str = "raw"
    tenant_level: bool = False

    def __post_init__(self) -> None:
        from application_sdk.testing.integration._errors import (  # noqa: PLC0415
            GoldenLayoutError,
        )

        if not self.stages:
            raise GoldenLayoutError(
                message="A golden-corpus layout must declare at least one stage.",
                field="stages",
                constraint="non_empty",
            )
        if len(set(self.stages)) != len(self.stages):
            raise GoldenLayoutError(
                message=f"Duplicate stage names in layout: {self.stages}.",
                field="stages",
                constraint="unique",
                value_summary=", ".join(self.stages),
            )
        for stage in self.stages:
            if (
                not stage
                or "/" in stage
                or "\\" in stage
                or ".." in stage
                or stage == "."
            ):
                raise GoldenLayoutError(
                    message=(f"Stage name {stage!r} is not a single directory name."),
                    field="stages",
                    constraint="single_path_segment",
                    value_summary=stage,
                )
        if self.input_stage not in self.stages:
            raise GoldenLayoutError(
                message=(
                    f"input_stage={self.input_stage!r} is not one of the declared "
                    f"stages {self.stages}. The stage feeding the transform has to "
                    "be declared, not inferred."
                ),
                field="input_stage",
                constraint="declared_stage",
                value_summary=self.input_stage,
            )


@dataclass(frozen=True)
class GoldenCorpus:
    """A resolved golden corpus rooted at an existing directory.

    Build one with :meth:`from_env` (raises when unconfigured) or
    :func:`require_golden_corpus` (skips when unconfigured).
    """

    root: Path
    layout: GoldenLayout = field(default_factory=GoldenLayout)
    tenant: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        layout: GoldenLayout | None = None,
        default_root: Path | str | None = None,
        tenant: str | None = None,
    ) -> GoldenCorpus:
        """Resolve the corpus root from ``E2E_GOLDEN_ROOT`` or *default_root*.

        Raises:
            GoldenCorpusUnavailableError: Neither source resolved to an existing
                directory. This is the "declared absent" case
                :func:`require_golden_corpus` turns into a skip.
            GoldenCorpusLayoutError: A root resolved but is not a directory, or
                the tenant directory does not exist.
        """
        from application_sdk.testing.integration._errors import (  # noqa: PLC0415
            GoldenCorpusUnavailableError,
        )

        layout = layout or GoldenLayout()
        override = os.environ.get(GOLDEN_ROOT_ENV)
        if override is not None and not override.strip():
            raise _layout_error(
                path=Path("."),
                message=(
                    f"{GOLDEN_ROOT_ENV} is set but empty — likely an unresolved "
                    "template variable, not an intentional default."
                ),
                suggested_action=(
                    f"Point {GOLDEN_ROOT_ENV} at the corpus root, or unset it "
                    "to fall back to the in-repo default."
                ),
            )
        if override:
            root = Path(override).expanduser()
            if not root.is_dir():
                raise _layout_error(
                    path=root,
                    message=(
                        f"{GOLDEN_ROOT_ENV}={override!r} does not point at an "
                        "existing directory."
                    ),
                    suggested_action=(
                        f"Point {GOLDEN_ROOT_ENV} at the corpus root, or unset it "
                        "to fall back to the in-repo default."
                    ),
                )
        elif default_root is not None and Path(default_root).expanduser().is_dir():
            root = Path(default_root).expanduser()
        else:
            raise GoldenCorpusUnavailableError(
                message=(
                    f"No golden corpus available: {GOLDEN_ROOT_ENV} is unset and "
                    f"the default root {default_root!r} does not exist."
                ),
                service="golden-corpus",
                suggested_action=(
                    f"Set {GOLDEN_ROOT_ENV} to a corpus root, or commit the "
                    "fixture tree at the default path."
                ),
            )

        corpus = cls(root=root, layout=layout)
        return corpus.for_tenant(tenant) if tenant is not None else corpus

    @property
    def base(self) -> Path:
        """The directory the stage directories sit directly under.

        Raises:
            GoldenCorpusLayoutError: The layout declares a tenant level but no
                tenant has been selected.
        """
        if not self.layout.tenant_level:
            return self.root
        if self.tenant is None:
            raise _layout_error(
                path=self.root,
                message=(
                    "This layout declares tenant_level=True, so a tenant must be "
                    "selected before any stage can be resolved."
                ),
                suggested_action=(
                    "Call .for_tenant(name) — .tenants() lists what the corpus holds."
                ),
            )
        return self.root / self.tenant

    def tenants(self) -> tuple[str, ...]:
        """Tenant directory names, sorted. Dot-directories are not tenants.

        Raises:
            GoldenCorpusLayoutError: The layout has no tenant level, or the root
                holds no tenant directories.
        """
        if not self.layout.tenant_level:
            raise _layout_error(
                path=self.root,
                message=(
                    "This layout declares tenant_level=False, so the corpus has "
                    "no tenant directories to list."
                ),
                suggested_action=(
                    "Declare GoldenLayout(tenant_level=True) if the fixture tree "
                    "really has a tenant level."
                ),
            )
        found = tuple(
            sorted(
                p.name
                for p in self.root.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        )
        if not found:
            raise _layout_error(
                path=self.root,
                message="Corpus root holds no tenant directories.",
                suggested_action=(
                    "Commit at least one tenant directory, or declare "
                    "GoldenLayout(tenant_level=False)."
                ),
            )
        return found

    def for_tenant(self, tenant: str) -> GoldenCorpus:
        """Return this corpus scoped to *tenant*.

        Raises:
            GoldenCorpusLayoutError: The layout has no tenant level, or the
                directory is absent.
        """
        if not self.layout.tenant_level:
            raise _layout_error(
                path=self.root,
                message=(
                    f"Cannot select tenant {tenant!r}: this layout declares "
                    "tenant_level=False."
                ),
                suggested_action=(
                    "Declare GoldenLayout(tenant_level=True) to use a tenant axis."
                ),
            )
        candidate = self.root / tenant
        if not candidate.is_dir():
            known = sorted(
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
            raise _layout_error(
                path=candidate,
                message=f"Tenant directory for {tenant!r} does not exist.",
                suggested_action=(
                    f"Known tenants: {', '.join(known) or '(none captured yet)'}."
                ),
            )
        return replace(self, tenant=tenant)

    def stage_dir(self, stage: str) -> Path:
        """Directory for *stage*.

        Raises:
            GoldenCorpusLayoutError: *stage* is undeclared, or its directory is
                absent.
        """
        if stage not in self.layout.stages:
            raise _layout_error(
                path=self.base,
                message=(
                    f"Stage {stage!r} is not declared by this layout "
                    f"({', '.join(self.layout.stages)})."
                ),
                suggested_action=(
                    "Add the stage to GoldenLayout(stages=...) if the corpus "
                    "really has it."
                ),
            )
        directory = self.base / stage
        if not directory.is_dir():
            raise _layout_error(
                path=directory,
                message=f"Declared stage {stage!r} has no directory in the corpus.",
                suggested_action=(
                    "Commit the stage directory, or drop the stage from "
                    "GoldenLayout(stages=...)."
                ),
            )
        return directory

    @property
    def input_dir(self) -> Path:
        """Directory of the stage that feeds the transform."""
        return self.stage_dir(self.layout.input_stage)

    def subdirs(self, stage: str) -> tuple[str, ...]:
        """Immediate child directory names of *stage*, sorted.

        Lets a suite iterate a stage's per-typename subdirectories (see the
        module docstring) without hardcoding their names. Files directly under
        the stage are not included.
        """
        directory = self.stage_dir(stage)
        return tuple(sorted(p.name for p in directory.iterdir() if p.is_dir()))

    def files(self, stage: str, *, pattern: str = "*") -> tuple[Path, ...]:
        """Data files in *stage* matching *pattern*, sorted, never empty.

        Raises:
            GoldenCorpusLayoutError: Nothing matched. A stage that exists but
                holds nothing is a broken corpus, not an empty result.
        """
        directory = self.stage_dir(stage)
        found = tuple(
            sorted(
                p
                for p in directory.rglob(pattern)
                if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
            )
        )
        if not found:
            raise _layout_error(
                path=directory,
                message=(
                    f"Stage {stage!r} holds no {pattern!r} files in a supported "
                    f"format ({', '.join(sorted(SUPPORTED_SUFFIXES))})."
                ),
                suggested_action=(
                    "Commit the fixture files, or check the capture step wrote "
                    "into this stage."
                ),
            )
        return found

    def records(self, stage: str, *, pattern: str = "*") -> list[dict[str, Any]]:
        """Every record in *stage*, concatenated in file order.

        Raises:
            GoldenCorpusLayoutError: The stage holds no matching files, or every
                matching file parsed to zero records.
            GoldenCorpusFormatError: A file could not be parsed, or held
                something other than records.
            GoldenParquetSupportError: A parquet file was found without pyarrow
                installed.
        """
        records: list[dict[str, Any]] = []
        files = self.files(stage, pattern=pattern)
        for path in files:
            records.extend(read_records(path))
        if not records:
            raise _layout_error(
                path=self.stage_dir(stage),
                message=(
                    f"Stage {stage!r} has {len(files)} file(s) but they parsed to "
                    "zero records."
                ),
                suggested_action=(
                    "Check the capture step wrote records rather than empty containers."
                ),
            )
        logger.debug(
            "Loaded %d golden records from stage %s of %s",
            len(records),
            stage,
            self.base,
        )
        return records

    def validate(self) -> None:
        """Assert every declared stage exists and holds at least one file.

        Raises:
            GoldenCorpusLayoutError: The first stage that fails either check.
        """
        bases = (
            [self.for_tenant(t) for t in self.tenants()]
            if self.layout.tenant_level and self.tenant is None
            else [self]
        )
        for corpus in bases:
            for stage in corpus.layout.stages:
                corpus.files(stage)


def _layout_error(*, path: Path, message: str, suggested_action: str) -> Exception:
    """Build a :class:`GoldenCorpusLayoutError` carrying the offending path."""
    from application_sdk.testing.integration._errors import (  # noqa: PLC0415
        GoldenCorpusLayoutError,
    )

    return GoldenCorpusLayoutError(
        message=f"{message} (path: {path})",
        field="golden_corpus",
        constraint="declared_layout",
        value_summary=str(path),
        suggested_action=suggested_action,
    )


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read one corpus file as a list of records.

    Dispatches on the suffix: JSON, NDJSON (``.ndjson`` / ``.jsonl``), CSV,
    parquet. A ``.json`` file that does not parse as one document is retried as
    NDJSON, since captured fixtures often keep a streaming producer's filename.

    Raises:
        GoldenCorpusFormatError: Unsupported suffix, unparseable content, or a
            payload that is not records.
        GoldenParquetSupportError: Parquet file, pyarrow not installed.
    """
    suffix = path.suffix.lower()
    if suffix in _JSON_SUFFIXES:
        return _read_json(path)
    if suffix in _NDJSON_SUFFIXES:
        return _read_ndjson(path)
    if suffix in _CSV_SUFFIXES:
        return _read_csv(path)
    if suffix in _PARQUET_SUFFIXES:
        return _read_parquet(path)
    raise _format_error(
        path,
        f"Unsupported corpus file format {suffix!r}.",
        f"Supported formats: {', '.join(sorted(SUPPORTED_SUFFIXES))}.",
    )


def _format_error(path: Path, message: str, action: str) -> Exception:
    from application_sdk.testing.integration._errors import (  # noqa: PLC0415
        GoldenCorpusFormatError,
    )

    return GoldenCorpusFormatError(
        message=f"{message} (file: {path})",
        field="golden_corpus_file",
        constraint="parseable_records",
        value_summary=str(path),
        suggested_action=action,
    )


def _as_records(path: Path, payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            return list(payload)
        raise _format_error(
            path,
            "JSON array holds a non-object element.",
            "A corpus file must hold records (objects), not scalars.",
        )
    raise _format_error(
        path,
        f"JSON payload is a {type(payload).__name__}, not records.",
        "Store either one record object or an array of record objects.",
    )


def _read_json(path: Path) -> list[dict[str, Any]]:
    """Read a ``.json`` corpus file, falling back to NDJSON.

    A ``.json`` file holding one record per line rather than a single document is
    common in captured fixtures, because a connector that streams records writes
    exactly that and the capture keeps the producer's filename. Dispatching on
    the suffix alone would reject those, so an undecodable whole-file parse is
    retried line by line before it is reported.
    """
    from application_sdk.testing.integration._errors import (  # noqa: PLC0415
        GoldenCorpusFormatError,
    )

    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as exc:
        try:
            return _read_ndjson(path)
        except GoldenCorpusFormatError:
            raise _format_error(
                path,
                "File is neither valid JSON nor valid NDJSON.",
                "Fix or re-capture the file.",
            ) from exc
    return _as_records(path, payload)


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = orjson.loads(line)
            except orjson.JSONDecodeError as exc:
                raise _format_error(
                    path,
                    f"Line {lineno} is not valid JSON.",
                    "Fix or re-capture the file.",
                ) from exc
            if not isinstance(payload, dict):
                raise _format_error(
                    path,
                    f"Line {lineno} is a {type(payload).__name__}, not a record.",
                    "Every NDJSON line must be one record object.",
                )
            records.append(payload)
    return records


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise _format_error(
                path,
                "CSV file has no header row.",
                "A corpus CSV needs a header naming each field.",
            )
        return [dict(row) for row in reader]


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:  # conformance: ignore[E008] re-raised as a typed error naming the missing extra
        from application_sdk.testing.integration._errors import (  # noqa: PLC0415
            GoldenParquetSupportError,
        )

        raise GoldenParquetSupportError(
            message=(
                f"Corpus holds a parquet file but pyarrow is not installed "
                f"(file: {path})."
            ),
            resource=str(path),
            expected_state="pyarrow installed",
            actual_state="pyarrow missing",
            suggested_action=(
                "Install the SDK's [sql] or [incremental] extra, which carries "
                "pyarrow — the SDK core deliberately does not."
            ),
        ) from exc

    return [dict(row) for row in pq.read_table(path).to_pylist()]


def require_golden_corpus(
    *,
    layout: GoldenLayout | None = None,
    default_root: Path | str | None = None,
    tenant: str | None = None,
    validate: bool = True,
) -> GoldenCorpus:
    """Resolve the corpus, or skip the test when none is configured.

    The single skip idiom for this tier: an absent corpus is a skipped scenario,
    a broken one is a failure. Call it from a fixture rather than composing a
    module-level ``skipif``, so the reason reaches the report.

    Args:
        layout: The corpus's declared layout. Defaults to
            ``("raw", "transformed")`` with no tenant level.
        default_root: In-repo fallback used when ``E2E_GOLDEN_ROOT`` is unset.
        tenant: Tenant to select, for a layout that declares a tenant level.
        validate: Check every declared stage exists and holds files before
            returning. On by default, so a broken tree fails at fixture setup
            with the offending path rather than mid-assertion.

    Raises:
        GoldenCorpusLayoutError: The corpus exists but does not match the
            declared layout.
    """
    import pytest  # noqa: PLC0415

    from application_sdk.testing.integration._errors import (  # noqa: PLC0415
        GoldenCorpusUnavailableError,
    )

    try:
        corpus = GoldenCorpus.from_env(
            layout=layout, default_root=default_root, tenant=tenant
        )
    except GoldenCorpusUnavailableError as exc:
        pytest.skip(str(exc.message))

    if validate:
        corpus.validate()
    return corpus


__all__ = [
    "GOLDEN_ROOT_ENV",
    "SUPPORTED_SUFFIXES",
    "GoldenCorpus",
    "GoldenLayout",
    "read_records",
    "require_golden_corpus",
]
