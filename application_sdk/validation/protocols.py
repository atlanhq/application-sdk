"""The two plug-in seams of artifact validation (ADR-0020).

The wrapper is thin on purpose: it takes an app-owned declaration, dispatches on
format, and owns only the shared outcome (:mod:`application_sdk.validation.artifacts`).
Everything that varies sits behind one of exactly two protocols, and **keeping them
orthogonal is the load-bearing choice** — conflating "where does the declaration
come from" with "how is this format checked" forces either a dataframe dependency
or a hand-authored field list for the 500-type asset case:

===================  ============================================  ==========================
Seam                 Question                                      Plug-ins
===================  ============================================  ==========================
:class:`SchemaSource`   where does the declaration come from?      ``ModelSource``, ``ContractSource``
:class:`FormatValidator` how is it checked for this format?        NDJSON (streaming), parquet (footer)
===================  ============================================  ==========================

The cross product is not uniformly implementable, and that is stated rather than
guessed at: parquet x model is genuinely ``unsupported`` because a model carries no
column mapping. :meth:`FormatValidator.supports` is where a cell says so, and the
wrapper turns a ``False`` into an ``unsupported`` outcome — never into silence.

``Protocol`` rather than an ABC so a plug-in is structurally typed: an app can
supply its own source or validator without importing an SDK base class, and the
SDK never has to enumerate implementations. Both are ``runtime_checkable`` so the
wrapper can reject a mis-shaped plug-in at registration with a clear error instead
of an ``AttributeError`` mid-scan; note that ``isinstance`` against a runtime
protocol checks member *presence*, not signatures, so it is a guardrail rather than
a type check.

Everything here is synchronous. Validators are plain scans, so the one caller that
must stay off the event loop — the activity interceptor — owns the offload decision
(``run_in_thread``, or an isolated child process for the model path) rather than
every validator re-deciding it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from application_sdk.validation.artifacts import (
    ArtifactDeclaration,
    ArtifactValidationReport,
)

__all__ = ["FormatValidator", "SchemaSource"]


@runtime_checkable
class SchemaSource(Protocol):
    """Where an artifact's declaration comes from.

    Two implementations ship in :mod:`application_sdk.validation.sources`:
    :class:`~application_sdk.validation.sources.ContractSource` loads the app's
    generated ``app/generated/artifact_schemas.json`` — versioned, pinned,
    statically diffable, readable by non-Python consumers — and
    :class:`~application_sdk.validation.sources.ModelSource` resolves to an
    executable typed model, so nothing is authored at all.

    **There is no inline source**, and there will not be one: no literal field map,
    no dict escape hatch, not even for a three-field artifact. Every declaration is
    version-controlled. The contracts found in the field were prose comments spread
    across each app's source — load-bearing, well written, and exactly the trap,
    because a comment can faithfully document a workaround, so drift gets recorded
    *as the spec*. A "just this once" inline map is how that state is reached.

    An app whose storage facade bypasses the framework still calls the public
    ``validate_artifact(...)`` entry point — it just passes a ``ContractSource``.
    The escape hatch is about *where the call happens*, never about *where the
    declaration lives*.
    """

    @property
    def kind(self) -> str:
        """Short stable identifier for telemetry: ``contract`` or ``model``.

        Lands on the outcome event as ``artifact_schema_source``, so it is a
        queryable value and must not be reworded once shipped.
        """
        ...

    def resolve(self) -> ArtifactDeclaration | None:
        """Load the declaration, or ``None`` when this artifact has none.

        ``None`` is a first-class answer, not an error: it becomes the
        ``not_declared`` outcome, a finding on an entrypoint's public boundary and
        informational on an internal ``@task``. Either way it emits.

        A *malformed or absent* declaration artifact is a **different** answer and
        keeps its own channel: raise
        :class:`~application_sdk.validation.sources.ArtifactDeclarationError`, which
        :func:`~application_sdk.validation.wrapper.validate_artifact` degrades to a
        warning and an ``absent`` outcome. Nothing reaches the app either way — the
        validation scaffold is defense in depth and may never break a real hand-off
        — but collapsing the two into ``None`` would report a *loader* failure as
        ``not_declared``, blaming the app on its own public boundary for a file it
        wrote correctly.

        Any other exception is caught by the wrapper too, on the "our validator
        broke" axis, which always fails open.
        """
        ...


@runtime_checkable
class FormatValidator(Protocol):
    """How one artifact format is checked against a resolved declaration.

    Each format brings its own dependency floor, and the wrapper's whole point is
    that the expensive mechanism is never on the cheap path:

    * **NDJSON** streams line by line — one pass, constant memory, no dataframe,
      stdlib and ``orjson`` only, so **zero new dependencies**.
    * **parquet** reads the schema from the file footer and diffs it — *metadata
      only, no rows read*. ``pyarrow`` is an extra, so it is imported lazily and
      degrades to skip-with-warning when absent.

    Answering "is ``START_TIME`` a timestamp?" by loading rows into a dataframe
    pays a dataframe to do a metadata lookup and drags pandas into the runtime path
    of callers that only ever see JSON. That is why this seam exists.
    """

    @property
    def artifact_format(self) -> str:
        """Short stable identifier for telemetry: ``ndjson``, ``parquet``.

        Lands on the outcome event as ``artifact_format``.
        """
        ...

    @property
    def unit(self) -> str:
        """What this validator counts — :data:`~application_sdk.validation.artifacts.UNIT_RECORD`
        or :data:`~application_sdk.validation.artifacts.UNIT_COLUMN`.

        Reported alongside the scalar counts so a consumer never has to infer the
        unit from the format.
        """
        ...

    def supports(self, declaration: ArtifactDeclaration) -> bool:
        """Whether this format can check *this kind* of declaration.

        The one cell that answers ``False`` today is parquet x model: a model
        carries no column mapping, so a footer diff has nothing to diff against.
        The wrapper turns ``False`` into an ``unsupported`` outcome — the cell says
        so out loud rather than guessing or going quiet.
        """
        ...

    def validate(
        self, path: Path, declaration: ArtifactDeclaration
    ) -> ArtifactValidationReport:
        """Check the artifact at ``path`` and return the shared report shape.

        Called only when :meth:`supports` returned True. **Every unit is examined**
        — bounding applies to the report's two output surfaces, never to the scan,
        so the scalar counts always describe the whole artifact.

        A missing or unreadable artifact is an ``absent`` report
        (:meth:`~application_sdk.validation.artifacts.ArtifactValidationReport.absent`),
        not an exception: a validator that raises into a hand-off has broken the
        very thing it was added to protect. A raise here is the wrapper's
        "our validator broke" axis, which always fails open regardless of posture.
        """
        ...
