"""Registration-time guard: every entrypoint ``FileReference`` needs a declared schema.

Data crosses app boundaries as files, and at every hand-off the producer's idea
of the artifact's shape and the consumer's idea of it are independent beliefs
that nothing checks (ADR-0020).  ``artifactSchemas`` in the app's pkl contract is
where that shape is written down; the toolkit renders it to
``app/generated/artifact_schemas.json``, keyed by the name of the
``FileReference`` field it describes.

This module answers one question at App registration: *does every
``FileReference`` on an entry point's public boundary have a declaration?*

**Boundary, not everything.**  The rule applies to every
:class:`~application_sdk.app.entrypoint.EntryPointMetadata`'s ``input_type`` and
``output_type`` and to nothing else:

===============================================  ============  ==============================================
Surface                                          Declaration   Why
===============================================  ============  ==============================================
An entry point's ``input_type``/``output_type``  **required**  Public by definition — another app or the DAG reads it
An internal ``@task`` contract                   optional      App-internal processing; the app owns the risk
===============================================  ============  ==============================================

The boundary needs no special-casing: the default ``run()`` method is registered
as an *implicit* entry point carrying the same ``EntryPointMetadata`` as an
explicit ``@entrypoint``, so "every entry point" already means "every public
boundary".  ``@task`` contracts never become ``EntryPointMetadata``, so they are
excluded by construction rather than by a filter that could drift.

**Warns in 3.x, raises in 4.0.**  A missing declaration is the same class of
defect as the ``EntryPointContractError`` family this runs alongside — a public
interface that is not fully described — but making it fatal immediately would
break every app in the fleet at once, so 3.x gets a deprecation window and the
warning names the removal version.  Conformance ``K016`` reports the same defect
statically, in review, before a worker is ever built.

This is *not* a false-positive-prone content check: "no declaration exists for
this field" is a structural fact about two files, so it needs a deprecation
window rather than the graduation gate the content checks need.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin

import orjson

from application_sdk.constants import CONTRACT_GENERATED_DIR
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.app.entrypoint import EntryPointMetadata

__all__ = [
    "ARTIFACT_SCHEMA_REMOVAL_VERSION",
    "warn_undeclared_artifact_schemas",
]

_logger = get_logger(__name__)

#: SDK version in which a missing boundary declaration stops being a warning and
#: starts being an ``EntryPointContractError``.  Named in the warning text so a
#: reader never has to go looking for the deadline.
ARTIFACT_SCHEMA_REMOVAL_VERSION = "4.0"

_ARTIFACT_SCHEMAS_FILENAME = "artifact_schemas.json"


def _declared_artifact_schema_keys(entrypoint_name: str) -> frozenset[str] | None:
    """Return the contract field names declared in this entry point's schemas file.

    Searches both generated layouts, nested first — mirroring how the handler
    locates an entry point's form configmap
    (``handler/service.py``): a multi-entrypoint (bundle) app nests each entry
    point's file under ``app/generated/<wire-name>/``, while a
    single-entrypoint app emits it flat at ``app/generated/``.  A bundle root
    never emits a shared file (declaring ``artifactSchemas`` there is a
    generation error), so the flat fallback cannot silently answer for the
    wrong entry point.

    **The fallback is between files, never between fields.**  The first file
    that exists is the final answer; unioning the two would let one entry
    point's boundary be satisfied by another scope's declarations.

    **Absent and unreadable are different answers.**  An absent file means the
    app declares nothing, which is exactly the state the caller warns about.  A
    file that is present but malformed means *we cannot tell*, and treating
    that as "declares nothing" would turn one bad JSON blob into a warning on
    every boundary field.  Never raises either way: a guard that broke
    registration while reading an advisory file would break the very thing it
    exists to protect.

    Args:
        entrypoint_name: The entry point's kebab-case wire name, which is also
            its directory name in the generated tree.

    Returns:
        The declared contract field names — empty when no file exists — or
        ``None`` when a file exists but could not be understood.
    """
    generated = Path(CONTRACT_GENERATED_DIR)
    for candidate in (
        generated / entrypoint_name / _ARTIFACT_SCHEMAS_FILENAME,
        generated / _ARTIFACT_SCHEMAS_FILENAME,
    ):
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        try:
            envelope: Any = orjson.loads(raw)
        except orjson.JSONDecodeError:
            _logger.warning(
                "Artifact-schema declarations at %s are not valid JSON; skipping "
                "the boundary check for this entry point rather than reporting "
                "every field as undeclared.",
                candidate,
                exc_info=True,
            )
            return None
        schemas = envelope.get("schemas") if isinstance(envelope, dict) else None
        if not isinstance(schemas, dict):
            _logger.warning(
                "Artifact-schema declarations at %s have no top-level 'schemas' "
                "object; skipping the boundary check for this entry point rather "
                "than reporting every field as undeclared.",
                candidate,
            )
            return None
        return frozenset(str(key) for key in schemas)
    return frozenset()


def _mentions_file_reference(annotation: Any) -> bool:
    """Whether *annotation* is, or contains, a :class:`FileReference`.

    Walks unions, containers and ``Annotated`` metadata rather than testing for
    identity, so ``FileReference | None``, ``list[FileReference]`` and
    ``dict[str, FileReference]`` all count.  A field that can carry an artifact
    at all is a field whose artifact needs describing, regardless of how the
    annotation wraps it.
    """
    if annotation is FileReference:
        return True
    if isinstance(annotation, type):
        # A concrete subclass of FileReference is still a FileReference.
        return issubclass(annotation, FileReference)
    origin = get_origin(annotation)
    if origin is None:
        return False
    args = get_args(annotation)
    if origin is Annotated:
        # Only the first arg is the type; the rest is metadata.
        return _mentions_file_reference(args[0]) if args else False
    return any(_mentions_file_reference(arg) for arg in args)


def _boundary_artifact_fields(contract: type) -> list[str]:
    """Return the names of *contract*'s ``FileReference``-bearing fields.

    Inherited fields count: ``model_fields`` resolves the full MRO, and a
    declaration is keyed by field name regardless of which base contributed it.

    Returns an empty list for anything that is not a Pydantic model — the
    entry-point contract checks in ``_ep_registration`` have already rejected a
    non-``Input``/``Output`` boundary type by the time this runs, so this is a
    belt-and-braces guard rather than a live code path.
    """
    model_fields = getattr(contract, "model_fields", None)
    if not isinstance(model_fields, dict):
        return []
    return [
        name
        for name, field_info in model_fields.items()
        if _mentions_file_reference(getattr(field_info, "annotation", None))
    ]


def warn_undeclared_artifact_schemas(
    app_name: str,
    entry_points: dict[str, EntryPointMetadata],
) -> None:
    """Warn for every boundary ``FileReference`` field with no declared schema.

    Emits both a :class:`DeprecationWarning` (so ``-W error`` and test suites
    can make it fatal ahead of 4.0) and a ``warning`` log line (so it is visible
    in a container's logs at worker build, where nobody is watching Python's
    warning filter) — the same pairing used for the other 4.0 deprecations in
    this SDK.

    Never raises.  Registration must not fail because an advisory guard could
    not read a file.

    Args:
        app_name: The registered app name, for the message.
        entry_points: The app's built entry points, keyed by wire name.
    """
    try:
        for ep_name, ep in entry_points.items():
            declared = _declared_artifact_schema_keys(ep_name)
            if declared is None:
                continue  # Unreadable declarations — logged above; don't guess.
            for direction, contract in (
                ("input", ep.input_type),
                ("output", ep.output_type),
            ):
                for field_name in _boundary_artifact_fields(contract):
                    if field_name in declared:
                        continue
                    message = (
                        f"App '{app_name}' entry point '{ep_name}': "
                        f"{direction} contract '{contract.__name__}' declares a "
                        f"FileReference field '{field_name}' with no artifact "
                        f"schema. An entry point's contracts are a public "
                        f"boundary — whatever reads this artifact has no way to "
                        f"check it matches what was written. Declare it in the "
                        f"app's pkl contract as "
                        f'artifactSchemas {{ ["{field_name}"] = new ArtifactSchema '
                        f"{{ ... }} }} and regenerate, so it lands in "
                        f"{CONTRACT_GENERATED_DIR}/{_ARTIFACT_SCHEMAS_FILENAME}. "
                        f"Internal @task contracts are exempt; entry-point "
                        f"contracts are not. This is a warning today and will "
                        f"be an error in v{ARTIFACT_SCHEMA_REMOVAL_VERSION}."
                    )
                    warnings.warn(message, DeprecationWarning, stacklevel=3)
                    _logger.warning("%s", message)
    except Exception:
        _logger.warning(
            "Artifact-schema declaration guard failed for app '%s'; skipping it. "
            "Registration is unaffected.",
            app_name,
            exc_info=True,
        )
