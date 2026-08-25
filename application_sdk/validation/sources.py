"""The two schema sources: where an artifact's declaration comes from (ADR-0020).

This module implements one of the wrapper's two orthogonal plug-in seams —
:class:`~application_sdk.validation.protocols.SchemaSource`. Two implementations
ship, and they are not variations on one theme; they exist because the two cases
they serve have opposite economics:

* :class:`ContractSource` loads the app's generated
  ``app/generated/artifact_schemas.json`` — versioned, pinned, statically
  diffable, and readable by non-Python consumers. Every field in it is authored by
  the app; the SDK ships no field list.
* :class:`ModelSource` resolves to an executable typed model, so **nothing is
  authored at all**. This is what makes the 500-type / 4000-property asset case
  tractable: no one could reasonably re-declare that by hand, and the model
  already *is* the declaration.

**There is no inline source, and there will not be one** — no literal field map,
no dict escape hatch, not even for a three-field artifact. The contracts FND-397
found in the field were prose comments spread across each app's source:
load-bearing, well written, and exactly the trap, because a comment can faithfully
document a workaround, so drift gets recorded *as the spec*. A "just this once"
inline map is how that state is reached. An app whose storage facade bypasses the
framework still calls :func:`~application_sdk.validation.wrapper.validate_artifact`
— it just passes a :class:`ContractSource`. The escape hatch is about *where the
call happens*, never about *where the declaration lives*.

**Two answers, not one.** "This artifact has no declaration" and "the declaration
file was there but unreadable" are different facts and are kept apart on purpose:
:meth:`~application_sdk.validation.protocols.SchemaSource.resolve` returns ``None``
for the first and raises :class:`ArtifactDeclarationError` for the second. Collapsed
into one answer, a loader that failed to read a perfectly good file would be
reported as ``not_declared`` — which on a public boundary is a *finding against the
app*. The SDK would be blaming an app for its own read failure. The wrapper turns
the raise into an ``absent`` outcome plus a warning, so nothing reaches the caller
either way (ADR-0020: the validation scaffold is defense in depth; it may never
break a real hand-off).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Final, Mapping

import orjson

from application_sdk.constants import CONTRACT_GENERATED_DIR
from application_sdk.errors.leaves import DataIntegrityError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.validation.artifacts import (
    FORMAT_NDJSON,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
)

logger = get_logger(__name__)

__all__ = [
    "ARTIFACT_SCHEMAS_ENVELOPE_VERSION",
    "ARTIFACT_SCHEMAS_FILENAME",
    "ArtifactDeclarationError",
    "ContractSource",
    "ModelSource",
    "artifact_schema_paths",
    "declared_artifact_fields",
]


ARTIFACT_SCHEMAS_FILENAME: Final = "artifact_schemas.json"
"""Name of the generated declaration artifact, as the contract toolkit emits it."""

ARTIFACT_SCHEMAS_ENVELOPE_VERSION: Final = 1
"""Envelope version of ``artifact_schemas.json`` this loader understands.

The toolkit bumps it only on a breaking change to the *file's shape* — never when
an app edits its own declarations — so a version this loader does not recognise
means the file is structured in a way it cannot safely parse. That degrades to
``absent`` with the version named, rather than a best-effort parse of a shape
nobody promised.
"""


@dataclass(kw_only=True)
class ArtifactDeclarationError(DataIntegrityError):
    """A declaration artifact exists but could not be turned into a declaration.

    Raised by a :class:`~application_sdk.validation.protocols.SchemaSource` whose
    input is present but unusable: unparseable JSON, an envelope version this SDK
    does not understand, a malformed schema entry, a model that cannot be delegated
    to. Distinct from ``resolve()`` returning ``None``, which means the honest
    "this artifact has no declaration".

    A typed :class:`~application_sdk.errors.leaves.DataIntegrityError` rather than a
    bare ``Exception``, because it does not only travel to
    :func:`~application_sdk.validation.wrapper.validate_artifact`, which swallows it.
    :func:`declared_artifact_fields` re-raises it to registration-time callers that
    have no wrapper underneath them, and an unclassified exception reaching one of
    those carries no audience, no retry disposition and no ``suggested_action`` — so
    the one error whose entire content is "your generated contract is unreadable"
    would arrive with nothing telling its owner what to change.

    ``DATA_INTEGRITY`` is the right category and ``APP_OWNER`` the right audience: the
    file is generated from the app's own ``contract/app.pkl``, so the app owner is
    who fixes it. Not retryable — a malformed file does not become well-formed on a
    second read.
    """

    code: ClassVar[str] = "DATA_INTEGRITY_ARTIFACT_DECLARATION"
    suggested_action: str | None = (
        "Regenerate the app's contract so app/generated/artifact_schemas.json "
        "matches the toolkit's output, or upgrade the SDK if the file was written "
        "by a newer contract toolkit than this SDK reads."
    )


# ---------------------------------------------------------------------------
# Locating the generated declaration file
# ---------------------------------------------------------------------------


def artifact_schema_paths(
    *, entrypoint: str = "", generated_dir: Path | None = None
) -> tuple[Path, ...]:
    """Where an entrypoint's declaration file is looked for, in order.

    A multi-entrypoint app's file is re-exported to
    ``{generated}/{entrypoint}/artifact_schemas.json`` by the standard
    per-entrypoint re-export; a single-entrypoint app's lands flat at
    ``{generated}/artifact_schemas.json``. An entrypoint name is known in both
    cases, so the per-entrypoint path is tried first and the flat path second —
    the same order ``handler/service.py`` already resolves ``manifest.json`` in.

    **The flat fallback is safe by construction, not by luck.** It would be the
    silent-wrong-answer failure mode if a bundle root could emit a shared file: an
    entrypoint that declares nothing would quietly be checked against someone
    else's declarations. The contract toolkit makes that impossible — declaring
    ``artifactSchemas`` on a bundle root is a *generation error*
    (``App.pkl``'s ``_bundleArtifactSchemaCheck``) — so the flat path can only ever
    hold a single-entrypoint app's own declarations.

    Args:
        entrypoint: Entrypoint name, or "" for a single-entrypoint app.
        generated_dir: Generated contract directory. Defaults to
            :data:`~application_sdk.constants.CONTRACT_GENERATED_DIR`, read at call
            time so a relocated directory is honoured.

    Returns:
        One or two paths, most specific first.
    """
    root = generated_dir if generated_dir is not None else Path(CONTRACT_GENERATED_DIR)
    flat = root / ARTIFACT_SCHEMAS_FILENAME
    if not entrypoint:
        return (flat,)
    return (root / entrypoint / ARTIFACT_SCHEMAS_FILENAME, flat)


def declared_artifact_fields(
    *, entrypoint: str = "", generated_dir: Path | None = None
) -> tuple[str, ...]:
    """Every contract field name declared for one entrypoint.

    Answers "which of this entrypoint's ``FileReference`` fields carry a
    declaration?" without naming one — the question a registration-time check asks,
    where the answer is a set difference against the contract model's own file
    fields rather than a validation run.

    Returns a tuple of names, never a mapping: the declaration bodies are the
    wrapper's business, and handing out a dict here would be a dict-shaped public
    API on the source seam.

    Args:
        entrypoint: Entrypoint name, or "" for a single-entrypoint app.
        generated_dir: Generated contract directory; defaults as for
            :func:`artifact_schema_paths`.

    Returns:
        Declared contract field names, in the generated file's own order. Empty
        when the app declares nothing — the normal unadopted state.

    Raises:
        ArtifactDeclarationError: The file is present but unreadable or malformed.
            Callers outside the wrapper decide what to do with it; inside
            :func:`~application_sdk.validation.wrapper.validate_artifact` it
            degrades to a warning and an ``absent`` outcome.
    """
    for path in artifact_schema_paths(
        entrypoint=entrypoint, generated_dir=generated_dir
    ):
        schemas = _load_schemas(path)
        if schemas is not None:
            return tuple(schemas)
    return ()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_field(raw: object, *, field_key: str, path: Path) -> DeclaredField:
    """Build one :class:`DeclaredField` from a raw entry in a schema's ``fields``."""
    where = f"{path}: schema '{field_key}'"
    if not isinstance(raw, dict):
        raise ArtifactDeclarationError(
            message=f"{where} has a field entry that is not an object",
            location=str(path),
            expectation="each entry in 'fields' is a JSON object",
            observed=type(raw).__name__,
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ArtifactDeclarationError(
            message=f"{where} has a field with no usable 'name'",
            location=str(path),
            expectation="every field carries a non-empty string 'name'",
        )

    declared_type = raw.get("type", "any")
    if not isinstance(declared_type, str) or not declared_type:
        raise ArtifactDeclarationError(
            message=f"{where} field '{name}' has a non-string 'type'",
            location=str(path),
            expectation="'type' is a non-empty string",
            observed=type(declared_type).__name__,
        )

    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ArtifactDeclarationError(
            message=f"{where} field '{name}' has a non-boolean 'required'",
            location=str(path),
            expectation="'required' is a JSON boolean",
            observed=type(required).__name__,
        )

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ArtifactDeclarationError(
            message=f"{where} field '{name}' has a non-string 'description'",
            location=str(path),
            expectation="'description' is a string",
            observed=type(description).__name__,
        )

    # The logical-type vocabulary is deliberately *not* policed here. A newer
    # toolkit may emit a type this SDK has no mapping for, and the honest answer is
    # for the format validator to resolve that one field to unsupported — naming
    # the type — rather than for the loader to declare the whole file malformed and
    # drop every other assertion in it. The toolkit's own typealias already rejects
    # typos at generation time.
    return DeclaredField(
        path=name,
        # Widened on purpose: an unrecognised member from a newer toolkit is passed
        # through for the validator to report, per the comment above.
        type=declared_type,  # type: ignore[arg-type]
        required=required,
        description=description,
    )


def _parse_schemas(path: Path, raw_bytes: bytes) -> Mapping[str, FieldMapDeclaration]:
    """Parse one ``artifact_schemas.json`` into declarations keyed by contract field."""
    try:
        document = orjson.loads(raw_bytes)
    except orjson.JSONDecodeError as exc:
        raise ArtifactDeclarationError(
            # The decoder's text goes in `observed`, not in `message`: interpolating
            # it here would make one dashboard group per parse position (E015).
            message=f"{path}: not valid JSON",
            location=str(path),
            expectation="a JSON object written by the contract toolkit",
            observed=str(exc),
            cause=exc,
        ) from exc

    if not isinstance(document, dict):
        raise ArtifactDeclarationError(
            message=f"{path}: top level is not a JSON object",
            location=str(path),
            expectation="a JSON object at the top level",
            observed=type(document).__name__,
        )

    version = document.get("version")
    if version != ARTIFACT_SCHEMAS_ENVELOPE_VERSION:
        raise ArtifactDeclarationError(
            message=(
                f"{path}: envelope version {version!r} is not the version this SDK "
                f"reads ({ARTIFACT_SCHEMAS_ENVELOPE_VERSION}); upgrade the SDK or "
                f"regenerate the contract"
            ),
            location=str(path),
            expectation=f"version {ARTIFACT_SCHEMAS_ENVELOPE_VERSION}",
            observed=repr(version),
        )

    schemas = document.get("schemas")
    if not isinstance(schemas, dict):
        raise ArtifactDeclarationError(
            message=f"{path}: 'schemas' is not a JSON object",
            location=str(path),
            expectation="'schemas' maps contract field names to declarations",
            observed=type(schemas).__name__,
        )

    declarations: dict[str, FieldMapDeclaration] = {}
    for field_key, entry in schemas.items():
        where = f"{path}: schema '{field_key}'"
        if not isinstance(entry, dict):
            raise ArtifactDeclarationError(
                message=f"{where} is not a JSON object",
                location=str(path),
                expectation="each schema is a JSON object",
                observed=type(entry).__name__,
            )

        artifact_format = entry.get("format")
        if not isinstance(artifact_format, str) or not artifact_format:
            raise ArtifactDeclarationError(
                message=f"{where} declares no 'format'",
                location=str(path),
                expectation="a non-empty string 'format'",
                observed=type(artifact_format).__name__,
            )

        fields = entry.get("fields")
        if not isinstance(fields, list):
            raise ArtifactDeclarationError(
                message=f"{where} has no 'fields' list",
                location=str(path),
                expectation="a 'fields' list",
                observed=type(fields).__name__,
            )
        # The toolkit refuses to generate a zero-field schema, because one reports
        # as declared while asserting nothing — the "looks adopted, validates
        # nothing" state this capability exists to remove. Refuse to load one too,
        # so a hand-edited file cannot reintroduce it behind the toolkit's back.
        if not fields:
            raise ArtifactDeclarationError(
                message=(
                    f"{where} declares zero fields, which would report as declared "
                    f"while checking nothing"
                ),
                location=str(path),
                expectation="at least one declared field",
                observed="0 fields",
            )

        declarations[field_key] = FieldMapDeclaration(
            fields=tuple(
                _parse_field(f, field_key=field_key, path=path) for f in fields
            ),
            artifact_format=artifact_format,
        )
    return MappingProxyType(declarations)


@functools.lru_cache(maxsize=None)
def _load_schemas(path: Path) -> Mapping[str, FieldMapDeclaration] | None:
    """Load and parse ``path``, or ``None`` when the app generated no such file.

    Cached per path, mirroring ``_relationship_field_names`` in
    :mod:`application_sdk.validation.assets`: the generated file is baked into the
    image and cannot change for the lifetime of the process, while the interceptor
    resolves a declaration once per ``FileReference`` per task. Re-reading and
    re-parsing the same bytes at every hand-off buys nothing.

    Failures are not cached — ``lru_cache`` memoises returns, not raises — so a
    transient read error is retried rather than pinned for the process lifetime.
    """
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        # A known-benign probe, not a problem: the toolkit emits no file at all for
        # an app that declares nothing, so absence is the normal unadopted state.
        logger.debug("No artifact schema declarations at %s", path)
        return None
    except OSError as exc:
        raise ArtifactDeclarationError(
            message=f"{path}: could not be read",
            location=str(path),
            observed=str(exc),
            cause=exc,
        ) from exc
    return _parse_schemas(path, raw_bytes)


# ---------------------------------------------------------------------------
# ContractSource — the app's generated artifact_schemas.json
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSource:
    """One artifact's declaration, loaded from the app's generated contract.

    Declarations are keyed by ``(entrypoint, contract field name)``. The entrypoint
    dimension is the *file's location* and the contract field name is the key
    inside it, which is why nothing has to be re-keyed when a single-entrypoint app
    grows into a bundle.

    Nothing is inferred from the shape of a storage path — path-shape inference is
    precisely what made the earlier upload-time hook match nothing and silently
    validate zero records. The runtime knows which contract field it is
    materialising, so that is what this is keyed on.

    Example::

        source = ContractSource(field="raw_queries", entrypoint="extract")
        report = validate_artifact(local_path, source)

    Args:
        field: Name of the ``FileReference`` field on the entrypoint's input or
            output contract model — the key inside the generated file.
        entrypoint: Entrypoint name for a multi-entrypoint app; "" for a
            single-entrypoint app, whose file sits flat in the generated directory.
        generated_dir: Root of the generated contract directory. Defaults to
            :data:`~application_sdk.constants.CONTRACT_GENERATED_DIR`, read at
            ``resolve()`` time rather than frozen at construction.
    """

    field: str
    entrypoint: str = ""
    generated_dir: Path | None = None

    @property
    def kind(self) -> str:
        """``contract`` — the telemetry identifier. Not to be reworded once shipped."""
        return "contract"

    @property
    def candidate_paths(self) -> tuple[Path, ...]:
        """Declaration files this source reads, most specific first.

        See :func:`artifact_schema_paths` for why the flat fallback is safe.
        """
        return artifact_schema_paths(
            entrypoint=self.entrypoint, generated_dir=self.generated_dir
        )

    def resolve(self) -> FieldMapDeclaration | None:
        """Load this field's declaration, or ``None`` when it has none.

        ``None`` covers both "the app generated no declaration file" and "the file
        exists but declares nothing for this field". Both are the same honest fact,
        and both become the ``not_declared`` outcome.

        Raises:
            ArtifactDeclarationError: The file is present but unreadable or
                malformed. :func:`~application_sdk.validation.wrapper.validate_artifact`
                turns that into a warning and an ``absent`` outcome; it never
                reaches the app.
        """
        for path in self.candidate_paths:
            schemas = _load_schemas(path)
            if schemas is None:
                continue
            # The first file that exists answers for this entrypoint, declaration
            # present or not. Falling through to the flat file when a per-entrypoint
            # file exists but omits this field would answer one entrypoint's question
            # with another scope's declarations — the fallback hazard the toolkit's
            # bundle-root refusal exists to remove.
            return schemas.get(self.field)
        return None


# ---------------------------------------------------------------------------
# ModelSource — an executable typed model
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _delegation_error(model: type) -> str:
    """Why ``model`` cannot be delegated to, or "" when it can.

    Reflection cached per class, exactly as ``_relationship_field_names`` in
    :mod:`application_sdk.validation.assets` reads a class's own fields once and
    memoises the answer. The check is deliberately shallow — a callable ``validate``
    is present — because its job is to fail with a sentence naming the class rather
    than with an ``AttributeError`` from inside a per-record scan.
    """
    validate = getattr(model, "validate", None)
    if validate is None:
        return f"{model.__qualname__} has no 'validate' method to delegate to"
    if not callable(validate):
        return f"{model.__qualname__}.validate is not callable"
    return ""


@dataclass(frozen=True)
class ModelSource:
    """A declaration that *is* an executable typed model — e.g. pyatlan_v9's ``Asset``.

    Nothing is authored, and that is the whole point: the asset case is 500+ types
    and 4000+ properties with diamond inheritance, which no one could reasonably
    re-declare by hand, and the model already carries every one of them. The check
    delegates to the model's own ``.validate()``, which
    :mod:`application_sdk.validation.assets` already does per record.

    Example::

        from pyatlan_v9.model.assets import Asset

        report = validate_artifact(transformed_dir, ModelSource(model=Asset))

    Args:
        model: The typed model class to delegate to. Must expose a callable
            ``validate``.
        artifact_format: Which hand-off this model stands in for. Defaults to
            ``ndjson``, the only cell that can delegate; ``parquet`` is accepted and
            resolves to ``unsupported``, because a model carries no column mapping
            and a footer diff would have nothing to diff against.
    """

    model: type
    artifact_format: str = FORMAT_NDJSON

    @property
    def kind(self) -> str:
        """``model`` — the telemetry identifier. Not to be reworded once shipped."""
        return "model"

    def resolve(self) -> ModelDeclaration:
        """Resolve to "delegate to this model".

        Never ``None``: the model *is* the declaration, so a ``ModelSource`` that
        exists has one by construction.

        Raises:
            ArtifactDeclarationError: ``model`` is not a class, or exposes nothing
                to delegate to. Degrades to a warning and an ``absent`` outcome
                inside :func:`~application_sdk.validation.wrapper.validate_artifact`.
        """
        if not isinstance(self.model, type):
            raise ArtifactDeclarationError(
                message=(
                    f"ModelSource.model must be a class, got "
                    f"{type(self.model).__name__}"
                ),
                location="ModelSource.model",
                expectation="a class exposing a callable validate()",
                observed=type(self.model).__name__,
            )
        problem = _delegation_error(self.model)
        if problem:
            raise ArtifactDeclarationError(
                message=f"ModelSource: {problem}",
                location=f"{self.model.__module__}.{self.model.__qualname__}",
                expectation="a class exposing a callable validate()",
            )
        return ModelDeclaration(model=self.model, artifact_format=self.artifact_format)
