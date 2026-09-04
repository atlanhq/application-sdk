"""Registration-time guard: every entrypoint ``FileReference`` needs a declared schema.

Data crosses app boundaries as files, and at every hand-off the producer's idea
of the artifact's shape and the consumer's idea of it are independent beliefs
that nothing checks (ADR-0020).  ``artifactSchemas`` in the app's pkl contract is
where that shape is written down, keyed by the name of the ``FileReference``
field it describes.  The toolkit renders it per entry point, and *where* is
decided by the generated tree rather than by the Python entry-point count: a
bundle emits ``app/generated/<wire-name>/artifact_schemas.json`` for each entry
point, while an app with one generated contract — including a route/card-split
app with several ``@entrypoint``\\ s behind one card — emits one flat
``app/generated/artifact_schemas.json``.
See ``docs/concepts/apps.md`` ("Declaring artifact schemas").

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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin

import orjson

from application_sdk.app._generated_tree import GeneratedLayout, generated_layout
from application_sdk.constants import CONTRACT_GENERATED_DIR
from application_sdk.contracts.types import FileReference
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.app.entrypoint import EntryPointMetadata

__all__ = [
    "ARTIFACT_SCHEMA_REMOVAL_VERSION",
    # Re-exported from application_sdk.app._generated_tree, which owns the
    # classification. Kept as a name here because this module's docstrings and
    # `_declared_artifact_schema_keys`'s signature are written in terms of it.
    "GeneratedLayout",
    "warn_undeclared_artifact_schemas",
]

_logger = get_logger(__name__)

#: SDK version in which a missing boundary declaration stops being a warning and
#: starts being an ``EntryPointContractError``.  Named in the warning text so a
#: reader never has to go looking for the deadline.
ARTIFACT_SCHEMA_REMOVAL_VERSION = "4.0"

_ARTIFACT_SCHEMAS_FILENAME = "artifact_schemas.json"


@dataclass(frozen=True)
class _Declarations:
    """What the generated tree says about one entry point's artifact schemas."""

    keys: frozenset[str] = frozenset()
    """Declared contract field names.  Empty when nothing is declared."""

    path: Path | None = None
    """The file that answered, or — when none exists — the file this app's
    toolkit output will actually write, so a warning names a location the author
    can act on.  ``None`` when the generated layout is ``unknown`` and naming one
    would be a guess; the caller describes both shapes instead.  Render it with
    :meth:`display_path`, never with ``str()``."""

    readable: bool = True
    """``False`` when a file exists but could not be understood.  The caller
    skips the entry point entirely rather than reporting every field as
    undeclared off one bad JSON blob."""

    @property
    def display_path(self) -> str:
        """``path`` rendered with forward slashes on every platform.

        ``str(Path(...))`` uses the OS separator, so the same app would report
        ``app\\generated\\artifact_schemas.json`` on Windows and
        ``app/generated/artifact_schemas.json`` everywhere else.  The path is
        repo-relative and names a committed file read cross-platform — the docs,
        the pkl contract and conformance K016's finding all spell it with
        forward slashes, so the warning must too, or a Windows developer cannot
        match the message against any of them.
        """
        return self.path.as_posix() if self.path is not None else ""


def _generated_layout() -> GeneratedLayout:
    """Classify this app's committed generated tree.

    Thin wrapper over :func:`application_sdk.app._generated_tree.generated_layout`
    applied to :data:`~application_sdk.constants.CONTRACT_GENERATED_DIR`, which
    is the authority — ``handler.service``'s configmap fallback and the
    tenant-side route check read the same classifier, so the same app cannot get
    two different answers about the same tree.
    """
    return generated_layout(Path(CONTRACT_GENERATED_DIR))


def _declared_artifact_schema_keys(
    entrypoint_name: str, *, layout: GeneratedLayout
) -> _Declarations:
    """Return the contract field names declared in this entry point's schemas file.

    Which files are searched follows the *generated layout*, not the Python
    entry-point count (see :func:`_generated_layout`):

    ``multi``
        ``app/generated/<wire-name>/artifact_schemas.json``, then the flat file.
        The flat fallback is safe by construction — declaring ``artifactSchemas``
        on a bundle root is a toolkit generation error, so a root-level file can
        only ever belong to a single-entry-point app.
    ``single``
        The flat file **only**.  Searching a nested path first would let a
        leftover ``app/generated/<name>/artifact_schemas.json`` — from a bundle
        this app used to be, or a stale directory — answer in place of the flat
        file the toolkit actually maintains.
    ``unknown``
        Both, best-effort, and the caller is told not to name a specific file.

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
        layout: The generated tree's shape, from :func:`_generated_layout`.

    Returns:
        A :class:`_Declarations` whose ``path`` is the file that answered, or —
        when nothing exists — the file this app's toolkit output will actually
        write, or ``None`` when the layout is ``unknown`` and naming one would
        be a guess.  ``keys`` is empty and ``readable`` is ``False`` when a file
        exists but could not be understood.
    """
    generated = Path(CONTRACT_GENERATED_DIR)
    nested = generated / entrypoint_name / _ARTIFACT_SCHEMAS_FILENAME
    flat = generated / _ARTIFACT_SCHEMAS_FILENAME
    candidates = (flat,) if layout == "single" else (nested, flat)
    for candidate in candidates:
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
                candidate.as_posix(),
                exc_info=True,
            )
            return _Declarations(readable=False, path=candidate)
        schemas = envelope.get("schemas") if isinstance(envelope, dict) else None
        if not isinstance(schemas, dict):
            _logger.warning(
                "Artifact-schema declarations at %s have no top-level 'schemas' "
                "object; skipping the boundary check for this entry point rather "
                "than reporting every field as undeclared.",
                candidate.as_posix(),
            )
            return _Declarations(readable=False, path=candidate)
        return _Declarations(
            keys=frozenset(str(key) for key in schemas), path=candidate
        )
    # Nothing exists. Name only a path this app's toolkit output will actually
    # write — a bundle emits one file per entry point under its wire name, a
    # single-entry-point (or route/card-split) app emits one flat file. When the
    # layout is unknown, name nothing: a cited path that the toolkit will never
    # write is worse than none, because following it cannot clear the warning.
    if layout == "multi":
        return _Declarations(path=nested)
    if layout == "single":
        return _Declarations(path=flat)
    return _Declarations()


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
        # The committed generated tree decides the layout, not len(entry_points):
        # a route/card-split app has several @entrypoints and one flat file.
        layout = _generated_layout()
        for ep_name, ep in entry_points.items():
            declarations = _declared_artifact_schema_keys(ep_name, layout=layout)
            if not declarations.readable:
                continue  # Unreadable declarations — logged above; don't guess.
            # Only ever cite a path this app's toolkit output will actually
            # write. With an ungenerated tree there is nothing to infer from, so
            # say where it lands for each shape instead of naming one and being
            # wrong — a path the toolkit never writes cannot clear the warning.
            destination = (
                f", so it lands in {declarations.display_path}"
                if declarations.display_path
                else (
                    ". It lands in app/generated/artifact_schemas.json for a "
                    "single-entry-point app, or "
                    "app/generated/<entry-point>/artifact_schemas.json for a bundle"
                )
            )
            for direction, contract in (
                ("input", ep.input_type),
                ("output", ep.output_type),
            ):
                for field_name in _boundary_artifact_fields(contract):
                    if field_name in declarations.keys:
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
                        f"{{ ... }} }} and regenerate{destination}. See "
                        f"docs/concepts/apps.md ('Declaring artifact schemas'). "
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
