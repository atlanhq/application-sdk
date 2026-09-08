"""FND-1790: SqlApp.run() keeps the transform declaration and checks it.

``run()`` used to throw away the four ``TransformOutput.transformed_file``
refs its transform tasks returned and hand publish a string-joined prefix
instead.  Publish walks that prefix and cannot tell a short tree from a small
one, so a run whose transformed tree was genuinely incomplete published a
subset and archived the rest (``ATLAS-404-00-00A`` on the rejected children).

The prefix stays exactly as it was — it is publish's contract.  What changes is
that ``run()`` now holds the expected set and asserts against it first.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.contracts.storage import VerifyRefsInput, VerifyRefsOutput
from application_sdk.contracts.types import FileReference
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskOutput,
    PrimeAuthOutput,
    TransformOutput,
)
from application_sdk.templates.sql_app import SqlApp
from application_sdk.templates.sql_app_errors import TransformedFileMissingError

OUTPUT_PATH = "./local/tmp/artifacts/apps/test/workflows/wf-1/run-1"
PREFIX = "artifacts/apps/test/workflows/wf-1/run-1/transformed"

ENTITIES = ("database", "schema", "table", "column")
TASK_BY_ENTITY = {
    "database": ("extract_databases", "transform_databases"),
    "schema": ("extract_schemas", "transform_schemas"),
    "table": ("extract_tables", "transform_tables"),
    "column": ("extract_columns", "transform_columns"),
}


def _transformed_ref(entity: str) -> FileReference:
    return FileReference(
        local_path=f"{OUTPUT_PATH}/transformed/{entity}/entities.json",
        storage_path=f"{PREFIX}/{entity}/entities.json",
        is_durable=True,
    )


def _app() -> SqlApp:
    app = SqlApp.__new__(SqlApp)
    app._app_name = "test-app"
    return app


def _patches(transform_outputs: dict[str, TransformOutput], verify: AsyncMock):
    """Patch prime + the four extract/transform pairs + verify_refs."""
    out = [
        patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        patch.object(
            SqlApp,
            "prime_sql_auth",
            new=AsyncMock(return_value=PrimeAuthOutput(duration_ms=1.0)),
        ),
        patch.object(SqlApp, "verify_refs", new=verify),
    ]
    for entity in ENTITIES:
        extract_name, transform_name = TASK_BY_ENTITY[entity]
        out.append(
            patch.object(
                SqlApp,
                extract_name,
                new=AsyncMock(
                    return_value=ExtractionTaskOutput(
                        typename=entity,
                        total_record_count=1,
                        raw_file=FileReference(
                            local_path=f"{OUTPUT_PATH}/raw/{entity}/records.json",
                            storage_path=f"raw/{entity}/records.json",
                            is_durable=True,
                        ),
                    )
                ),
            )
        )
        out.append(
            patch.object(
                SqlApp,
                transform_name,
                new=AsyncMock(return_value=transform_outputs[entity]),
            )
        )
    return out


async def _run(transform_outputs: dict[str, TransformOutput], verify: AsyncMock):
    app = _app()
    patches = _patches(transform_outputs, verify)
    for p in patches:
        p.start()
    try:
        return await app.run(ExtractionInput(output_path=OUTPUT_PATH))
    finally:
        for p in patches:
            p.stop()


def _all_produced() -> dict[str, TransformOutput]:
    return {
        e: TransformOutput(
            typename=e, total_record_count=3, transformed_file=_transformed_ref(e)
        )
        for e in ENTITIES
    }


class TestRunVerifiesTransformedDeclaration:
    async def test_the_refs_reach_the_output_and_the_check(self) -> None:
        verify = AsyncMock(return_value=VerifyRefsOutput(verified_count=4))

        result = await _run(_all_produced(), verify)

        # 1. The declaration survives onto the output, so a connector's own
        #    Atlan bridge can upload by ref instead of scanning a directory.
        assert [r.storage_path for r in result.transformed_files] == [
            f"{PREFIX}/{e}/entities.json" for e in ENTITIES
        ]
        # 2. And the same set was asserted before the prefix was handed on.
        verify.assert_awaited_once()
        sent: VerifyRefsInput = verify.await_args.args[0]
        assert sent.prefix == PREFIX
        assert [r.storage_path for r in sent.refs] == [
            f"{PREFIX}/{e}/entities.json" for e in ENTITIES
        ]

    async def test_the_prefix_contract_is_unchanged(self) -> None:
        """Publish reads ``transformed_data_prefix``; this must not move."""
        verify = AsyncMock(return_value=VerifyRefsOutput(verified_count=4))

        result = await _run(_all_produced(), verify)

        assert result.transformed_data_prefix == PREFIX
        assert result.output_path == OUTPUT_PATH

    async def test_verification_does_not_download_the_transformed_files(self) -> None:
        """The check is a HEAD. Left on, the interceptor would materialise
        every transformed file onto whichever pod runs the check."""
        verify = AsyncMock(return_value=VerifyRefsOutput(verified_count=4))

        await _run(_all_produced(), verify)

        sent: VerifyRefsInput = verify.await_args.args[0]
        assert all(r.auto_materialize is False for r in sent.refs)

    async def test_a_zero_row_entity_contributes_nothing_and_is_not_an_error(
        self,
    ) -> None:
        """The genuine zero-row signal publish already relies on: no records,
        no file, no complaint."""
        outputs = _all_produced()
        outputs["column"] = TransformOutput(typename="column", total_record_count=0)
        verify = AsyncMock(return_value=VerifyRefsOutput(verified_count=3))

        result = await _run(outputs, verify)

        assert [r.storage_path for r in result.transformed_files] == [
            f"{PREFIX}/{e}/entities.json" for e in ("database", "schema", "table")
        ]

    async def test_records_without_a_file_is_a_hole_and_fails_loudly(self) -> None:
        """A transform that mapped records but declared no file has produced
        asset data nothing can point at — it will simply be absent from the
        tree publish walks, where absence reads as removed-from-source."""
        outputs = _all_produced()
        outputs["table"] = TransformOutput(typename="table", total_record_count=7)
        verify = AsyncMock()

        with pytest.raises(TransformedFileMissingError) as exc:
            await _run(outputs, verify)

        assert exc.value.typename == "table"
        assert exc.value.record_count == 7
        verify.assert_not_awaited()

    async def test_no_entity_produced_output_skips_the_check_and_warns(self) -> None:
        outputs = {
            e: TransformOutput(typename=e, total_record_count=0) for e in ENTITIES
        }
        verify = AsyncMock()

        with patch("application_sdk.templates.sql_app.logger") as logger:
            result = await _run(outputs, verify)

        assert result.transformed_files == []
        verify.assert_not_awaited()
        assert logger.warning.called

    async def test_a_failed_check_propagates_instead_of_returning_the_prefix(
        self,
    ) -> None:
        """The whole point: the run stops at the producer rather than handing
        publish a prefix it cannot vouch for."""
        from application_sdk.storage.errors import StorageHandoffIncompleteError

        verify = AsyncMock(
            side_effect=StorageHandoffIncompleteError(
                "short", missing_keys=[f"{PREFIX}/table/entities.json"]
            )
        )

        with pytest.raises(StorageHandoffIncompleteError):
            await _run(_all_produced(), verify)


class TestCollectTransformedFiles:
    """Unit-level cover for the collector the run() path delegates to."""

    def test_order_follows_the_declaration(self) -> None:
        outs = [
            TransformOutput(
                typename=e, total_record_count=1, transformed_file=_transformed_ref(e)
            )
            for e in ENTITIES
        ]
        refs = SqlApp.collect_transformed_files(outs)
        assert [r.storage_path for r in refs] == [
            f"{PREFIX}/{e}/entities.json" for e in ENTITIES
        ]

    def test_unnamed_entity_still_names_the_count(self) -> None:
        with pytest.raises(TransformedFileMissingError) as exc:
            SqlApp.collect_transformed_files([TransformOutput(total_record_count=5)])
        assert "<unknown>" in exc.value.message
        assert exc.value.record_count == 5

    def test_empty_input_declares_nothing(self) -> None:
        assert SqlApp.collect_transformed_files([]) == []


class TestCollectTransformedFilesIsPublicForRunOverrides:
    """The reason it is public: a ``run()`` override adding a fifth entity.

    ``SqlApp.run()`` builds the declaration for the four entities it drives.
    A connector that adds one — procedures, say — holds a ``TransformOutput``
    that never passed through ``run()``, so its ref is absent from
    ``ExtractionOutput.transformed_files``. Without a public way to apply the
    same "records but no ref = raise, zero rows = skip" rule, every connector
    restates it by hand, which is the drift FND-1790 exists to remove.
    """

    def test_reachable_off_the_class_with_no_instance(self) -> None:
        """A ``run()`` override calls it before it has anything else to hand."""
        assert SqlApp.collect_transformed_files([]) == []

    def test_the_documented_concatenation_yields_the_whole_declaration(self) -> None:
        base = ExtractionOutput(
            transformed_data_prefix=PREFIX,
            transformed_files=[_transformed_ref(e) for e in ENTITIES],
        )
        procedures = TransformOutput(
            typename="extras-procedure",
            total_record_count=9,
            transformed_file=_transformed_ref("extras-procedure"),
        )

        declaration = [
            *base.transformed_files,
            *SqlApp.collect_transformed_files([procedures]),
        ]

        assert [r.storage_path for r in declaration] == [
            f"{PREFIX}/{e}/entities.json" for e in (*ENTITIES, "extras-procedure")
        ]

    def test_the_added_entity_gets_the_same_hole_check(self) -> None:
        """The whole point of sharing the helper rather than the rule."""
        with pytest.raises(TransformedFileMissingError) as exc:
            SqlApp.collect_transformed_files(
                [TransformOutput(typename="extras-procedure", total_record_count=9)]
            )

        assert exc.value.typename == "extras-procedure"


class TestRunOverrideSurfaceIsPublic:
    """Every helper a documented ``run()`` override needs is public.

    ``build_task_input`` was already public and documented as the API for
    overrides, but it takes a ``cred_ref`` that only a private method produced —
    so the documented path could not be walked as documented. The three helpers
    below close that, and the private names stay as deprecated shims because a
    `gh search code` over atlanhq finds a dozen connector repos on them.
    """

    def test_the_whole_wiring_path_is_public(self) -> None:
        for name in (
            "resolve_credential_ref",
            "build_task_input",
            "build_transform_input",
            "collect_transformed_files",
            "extract_procedures",
            "transform_procedures",
        ):
            assert hasattr(SqlApp, name), f"SqlApp.{name} is missing"
            assert not name.startswith("_")

    def test_build_transform_input_threads_the_ref(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionTaskInput

        ref = _transformed_ref("database")
        out = SqlApp.build_transform_input(ExtractionTaskInput(workflow_id="w"), ref)

        assert out.workflow_id == "w"
        assert out.raw_file is ref

    def test_resolve_credential_ref_is_reachable_on_an_instance(self) -> None:
        app = _app()
        assert app.resolve_credential_ref(ExtractionInput()) is None


class TestDeprecatedPrivateAliases:
    """The old names keep working for one major version.

    They were private and carried no compatibility promise, but a dozen
    connector repos call them today. Renaming with no shim would break every
    one on its next SDK bump — a fleet-wide outage traded for a tidier diff.
    """

    def test_build_transform_input_alias_delegates_and_warns(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionTaskInput

        ref = _transformed_ref("database")
        with pytest.warns(DeprecationWarning, match="build_transform_input"):
            out = SqlApp._build_transform_input(ExtractionTaskInput(), ref)

        assert out.raw_file is ref

    def test_resolve_credential_ref_alias_delegates_and_warns(self) -> None:
        app = _app()
        with pytest.warns(DeprecationWarning, match="resolve_credential_ref"):
            assert app._resolve_credential_ref(ExtractionInput()) is None

    def test_each_alias_names_its_removal_version(self) -> None:
        """A deprecation with no removal version never gets removed."""
        import inspect

        for fn in (SqlApp._build_transform_input, SqlApp._resolve_credential_ref):
            assert "v4.0.0" in inspect.getsource(fn)
