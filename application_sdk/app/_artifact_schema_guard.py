"""Registration-time guard: every entrypoint ``FileReference`` needs a declared schema.

Data crosses app boundaries as files, and at every hand-off the producer's idea
of the artifact's shape and the consumer's idea of it are independent beliefs
that nothing checks (ADR-0020).  ``artifactSchemas`` in the app's pkl contract is
where that shape is written down, keyed by the name of the ``FileReference``
field it describes.  The toolkit renders it per entry point: a bundle emits
``app/generated/<wire-name>/artifact_schemas.json`` for each, a
single-entry-point app emits one flat ``app/generated/artifact_schemas.json``.
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


@dataclass(frozen=True)
class _Declarations:
    """What the generated tree says about one entry point's artifact schemas."""

    keys: frozenset[str] = frozenset()
    """Declared contract field names.  Empty when nothing is declared."""

    path: Path | None = None
    """The file that answered, or — when none exists — the file where this entry
    point's declarations belong, so a warning can name a location the author can
    actually act on.  Render it with :meth:`display_path`, never with ``str()``."""

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


def _declared_artifact_schema_keys(
    entrypoint_name: str, *, is_bundle: bool
) -> _Declarations:
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
        is_bundle: Whether the app registers more than one entry point.  Only
            affects which path is *reported* when no file exists — a bundle's
            declarations belong in its own nested file, a single-entry-point
            app's in the flat one.

    Returns:
        A :class:`_Declarations` whose ``path`` is the file that answered, or —
        when nothing exists — the file where this entry point's declarations
        belong, chosen by ``is_bundle`` rather than by search order.  ``keys``
        is empty and ``readable`` is ``False`` when a file exists but could not
        be understood.
    """
    generated = Path(CONTRACT_GENERATED_DIR)
    candidates = (
        generated / entrypoint_name / _ARTIFACT_SCHEMAS_FILENAME,
        generated / _ARTIFACT_SCHEMAS_FILENAME,
    )
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
    # Nothing exists. Name where the declaration *would* land, which depends on
    # the app's shape, not on the search order: a bundle emits one file per
    # entry point under its wire name, a single-entry-point app emits one flat
    # file. Naming the flat path to a bundle author sends them to a file the
    # toolkit will never write — and that a bundle root cannot legally declare.
    return _Declarations(path=candidates[0] if is_bundle else candidates[1])


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
    is_bundle = len(entry_points) > 1
    try:
        for ep_name, ep in entry_points.items():
            declarations = _declared_artifact_schema_keys(ep_name, is_bundle=is_bundle)
            if not declarations.readable:
                continue  # Unreadable declarations — logged above; don't guess.
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
                        f"{{ ... }} }} and regenerate, so it lands in "
                        f"{declarations.display_path}. See docs/concepts/apps.md "
                        f"('Declaring artifact schemas'). Internal @task "
                        f"contracts are exempt; entry-point contracts are not. "
                        f"This is a warning today and will be an error in "
                        f"v{ARTIFACT_SCHEMA_REMOVAL_VERSION}."
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
