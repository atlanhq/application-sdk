"""``SqlApp``'s ``run()``-override helpers: fold the declaration and verify it.

Split out of ``templates/sql_app.py`` (FND-4267):
:class:`~application_sdk.templates.sql_app.SqlApp` inherits these, so every
name here is reached as ``SqlApp.<name>`` and stays part of the documented
``run()``-override surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from application_sdk.app.base import App
from application_sdk.contracts.storage import VerifyRefsInput
from application_sdk.contracts.types import FileReference

# ``AppTimeoutError`` is re-exported: importable from here before FND-4687 moved
# the wire-envelope rebuild into ``errors.wire`` (Symbol Removal Check).
from application_sdk.errors.leaves import AppTimeoutError as AppTimeoutError
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionOutput as _ExtractionOutput,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    TransformInput,
    TransformOutput,
)
from application_sdk.templates.sql_app_errors import (
    TransformedFileMissingError,
)

if TYPE_CHECKING:
    pass


if TYPE_CHECKING:
    # A type checker sees the mixin as an ``App``, so ``self`` has the App
    # API; at runtime it is a plain mixin, and only ``SqlApp`` is an ``App``.
    _AppBase = App
else:
    _AppBase = object

logger = get_logger(__name__)


class _SqlFinalizeMixin(_AppBase):
    """The declaration helpers a ``run()`` override ends in, mixed into ``SqlApp``."""

    async def finalize_extraction(
        self,
        base: _ExtractionOutput,
        extra: Sequence[TransformOutput] = (),
    ) -> _ExtractionOutput:
        """Fold extra transforms into ``base``'s declaration and verify it all.

        Call this from a ``run()`` override that adds entities the default
        ``run()`` does not drive — procedures, say. It is the whole tail of
        such an override: concatenate, assert, return.

        **Why a helper and not a documented recipe.** ``super().run()`` calls
        ``App.verify_refs`` on the four refs it drove and then returns, so an
        override that concatenates a fifth ref and returns a ``model_copy``
        ships it unverified while looking, at the call site, exactly like it
        verified everything — the same silent shortfall FND-1790 is about, one
        layer up. Documenting the extra ``verify_refs`` call would leave a step
        that is invisible when omitted; this method removes the chance to omit
        it.

        **Using ``App.upload_refs`` is not a substitute.** It gives partial
        cover — it fails if a declared ref cannot be materialised, and it
        verifies the copies it delivered — but it checks the *destination*
        after the fact. It does not assert the declaration against the
        deployment store before delivery starts, and it does not check
        containment in ``transformed_data_prefix``. So a missing ref surfaces
        as a materialisation failure part-way through a delivery rather than as
        a ``StorageHandoffIncompleteError`` naming the key, and a ref that
        landed outside the prefix is not caught at all. Call this first and
        hand ``upload_refs`` the finalised declaration.

        Verifies the **whole** declaration, not just *extra*. The four default
        refs were already checked inside ``super().run()``, so those HEADs are
        redundant — but the thing being handed downstream is the concatenated
        list, and asserting exactly what is handed on is cheaper to reason
        about than a split proof, at four metadata lookups.

        **Verification is skipped when there is no app context**, i.e. when
        ``run()`` was called directly rather than by a worker. Without a worker
        there is no activity interceptor, so nothing was persisted and there is
        nothing in the store to check. This is what lets a connector's existing
        ``run()`` unit tests keep passing unchanged: the declaration is still
        assembled and returned, only the store assertion is skipped. It logs a
        warning, so if it ever happens under a worker it is visible.

        Args:
            base: The ``ExtractionOutput`` returned by ``super().run()``.
            extra: ``TransformOutput`` values for the entities this override
                drove itself. Empty is valid and means "verify what base
                declared", which is a no-op assertion rather than an error.

        Returns:
            ``base`` with ``transformed_files`` extended by *extra*'s refs.

        Raises:
            TransformedFileMissingError: If a result in *extra* reports records
                but carries no ``transformed_file``.
            StorageHandoffIncompleteError: If any declared ref is missing from
                the store or resolves outside ``base.transformed_data_prefix``.
        """
        declaration = [
            *base.transformed_files,
            *self.collect_transformed_files(extra),
        ]

        # No app context means ``run()`` was invoked directly rather than by a
        # worker: the workflow wrapper binds ``_context`` before ``run()`` and
        # clears it after, so its absence is structural, not incidental.
        #
        # Skipping the check there is correct rather than a concession. Without
        # a worker there is no activity interceptor, so no ``transformed_file``
        # ref was ever persisted — there is nothing in the object store to
        # verify, and asserting against a store nothing wrote to would fail on
        # the absence of data that was never supposed to be there yet.
        #
        # It is also what keeps this change backwards compatible. Every SQL
        # connector has ``run()`` unit tests that drive the real ``run()`` with
        # the tasks mocked out and no infrastructure bound; before FND-1790
        # ``run()`` performed no I/O, so those tests needed none. Failing them
        # would force a test-only edit on every connector in the fleet to buy
        # an assertion that cannot mean anything in that context.
        #
        # The declaration is still assembled and still returned on
        # ``transformed_files``, so a connector's own ``upload_refs`` /
        # ``verify_refs`` call is unaffected — only the pre-delivery assertion
        # is skipped, and only where nothing was delivered.
        if self._context is None:
            logger.warning(
                "Skipping transformed-declaration verification: no app context, "
                "so run() is not executing under a worker and nothing was "
                "persisted to verify (%d ref(s) declared)",
                len(declaration),
            )
            return base.model_copy(update={"transformed_files": declaration})

        if declaration:
            await self.verify_refs(
                VerifyRefsInput(
                    # auto_materialize=False: this is a HEAD per ref, not a
                    # reason to pull every transformed file onto this pod.
                    refs=[
                        ref.model_copy(update={"auto_materialize": False})
                        for ref in declaration
                    ],
                    prefix=base.transformed_data_prefix,
                )
            )
        else:
            logger.warning(
                "No entity produced transformed output; transformed_data_prefix "
                "%s names an empty tree",
                base.transformed_data_prefix,
            )
        return base.model_copy(update={"transformed_files": declaration})

    @staticmethod
    def collect_transformed_files(
        results: Sequence[TransformOutput],
    ) -> list[FileReference]:
        """Return the ``transformed_file`` refs a set of transforms declared.

        This is the producer's declaration of what the transform step wrote —
        the expected set ``transformed_data_prefix`` gets checked against
        before it is handed downstream (FND-1790).

        **Public because a ``run()`` override needs it.** The class docstring
        already directs connectors that need their own sequencing to override
        ``run()`` and drive ``extract_*`` / ``transform_*`` themselves, and
        those methods are public for exactly that. A connector adding an entity
        the default ``run()`` does not know about — procedures, say — holds a
        ``TransformOutput`` that never passed through ``run()``, so its ref is
        absent from :attr:`ExtractionOutput.transformed_files`. Its refs have
        to be folded into that list, or every asset of that entity is dropped
        from the upload silently, because the ref-based path has no directory
        listing to fall back on.

        **Most overrides should call :meth:`finalize_extraction` instead**,
        which uses this and then verifies the combined declaration.
        Concatenating this return value by hand and returning it leaves the
        extra entity unasserted: ``super().run()`` verified the four refs it
        drove and then returned, so nothing checks the fifth. Reach for this
        method directly only when you are assembling a declaration you will
        verify yourself — ``App.upload_refs`` verifies its own delivery, so
        handing it the concatenated list also covers you.

        A transform that mapped zero records legitimately contributes no ref;
        that is the "genuine zero-row entity" signal publish already relies on
        and it is skipped silently. A transform that mapped records and still
        returned no ref is a hole, not a quiet day: its assets exist but
        nothing can point at them, so they will be missing from the tree
        publish walks and archived as removed-from-source. That case raises.

        Args:
            results: The ``TransformOutput`` values returned by the
                ``transform_*`` tasks, in any order.

        Returns:
            One ``FileReference`` per entity that produced output.

        Raises:
            TransformedFileMissingError: If any result reports records but
                carries no ``transformed_file``.
        """
        refs: list[FileReference] = []
        for result in results:
            if result.transformed_file is not None:
                refs.append(result.transformed_file)
            elif result.total_record_count > 0:
                raise TransformedFileMissingError(
                    message=(
                        f"transform_{result.typename or '<unknown>'} reported "
                        f"{result.total_record_count} records but returned no "
                        "transformed_file reference"
                    ),
                    typename=result.typename or None,
                    record_count=result.total_record_count,
                    expectation="a FileReference for the transformed output",
                    observed="transformed_file=None",
                )
        return refs

    @staticmethod
    def build_transform_input(
        base: ExtractionTaskInput,
        raw_file: FileReference | None,
    ) -> TransformInput:
        """Build a ``TransformInput`` from ``base`` carrying ``raw_file``.

        Part of the supported ``run()``-override surface (see the class
        docstring). ``transform_*`` is public, and this is the only thing that
        builds its input correctly — an override that adds an entity the
        default ``run()`` does not drive needs both.

        Called by ``run()`` to thread the durable ``FileReference``
        returned by each ``extract_*`` activity (as
        ``ExtractionTaskOutput.raw_file``) into the matching
        ``transform_*`` activity's input. The resulting ``TransformInput``
        inherits every field from ``base`` plus the new ``raw_file``
        ref; the activity interceptor will materialise the ref on
        whichever worker pod ends up running the transform, with
        SHA-256 sidecar verification — see ``storage.file_ref_sync``.
        """
        return TransformInput(
            **base.model_dump(exclude_none=False),
            raw_file=raw_file,
        )
