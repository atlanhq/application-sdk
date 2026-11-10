"""SQL query extraction App.

Subclass and implement ``get_query_batches`` and ``fetch_queries``::

    class MyQueryExtractor(SqlQueryExtractor):
        @task(timeout_seconds=600)
        async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
            # Implement connector-specific batch counting
            return QueryBatchOutput(total_batches=10, batch_size=1000)

        @task(timeout_seconds=3600)
        async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
            # connector-specific query fetching
            return QueryFetchOutput(queries_fetched=100)

``run()`` orchestrates the pipeline automatically (get_query_batches → fetch_queries
per batch → aggregate). Override ``run()`` to customise parallelism or error handling.
"""

from __future__ import annotations

import warnings
from typing import Any, ClassVar

from application_sdk.app.task import task
from application_sdk.credentials.ref import _guid_credential_ref
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates._template_errors import (
    SqlQueryExtractorNotImplementedError,
)
from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput,
    QueryBatchOutput,
    QueryExtractionInput,
    QueryExtractionOutput,
    QueryFetchInput,
    QueryFetchOutput,
)

logger = get_logger(__name__)


class SqlQueryExtractor(BaseMetadataExtractor):
    """Abstract base class for SQL query extraction apps.

    .. deprecated::
        Will be removed in v4.0.0. Use
        :class:`application_sdk.templates.SqlApp` instead.

    Inherits ``client_class``, ``handler_class``, and ``transformer_class``
    from ``BaseMetadataExtractor``.

    The ``run()`` method orchestrates the full extraction:
    ``get_query_batches`` → ``fetch_queries`` (per batch) → aggregate output.

    Implement both abstract tasks in your subclass; ``run()`` handles the rest.
    """

    # Prevent auto-registration of the abstract base template.
    _app_registered: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__module__.startswith("application_sdk."):
            return
        if SqlQueryExtractor not in cls.__bases__:
            return
        warnings.warn(
            f"{cls.__name__} subclasses SqlQueryExtractor which is deprecated. "
            "Use application_sdk.templates.SqlApp instead. "
            "Will be removed in v4.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    @task(timeout_seconds=600)
    async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
        """Determine how many batches to process and their size.

        Override in your subclass to implement connector-specific batch counting.
        """
        raise SqlQueryExtractorNotImplementedError(
            message=f"{type(self).__name__} must implement get_query_batches().",
            operation="get_query_batches",
        )

    @task(timeout_seconds=3600)
    async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
        """Fetch one batch of queries from the source system.

        Override this method in your connector subclass.
        """
        raise SqlQueryExtractorNotImplementedError(
            message=f"{type(self).__name__} must implement fetch_queries().",
            operation="fetch_queries",
        )

    async def run(self, input: QueryExtractionInput) -> QueryExtractionOutput:  # type: ignore[override]
        """Orchestrate the full query extraction pipeline.

        Default orchestration:
        1. Get batch count
        2. Fetch each batch sequentially
        3. Return aggregated output

        Override to customize batch processing (e.g., parallel batches).
        """
        workflow_id = input.workflow_id
        logger.info("Starting SQL query extraction: %s", workflow_id)

        try:
            # Prefer a prebuilt credential_ref; otherwise build the GUID ref
            # from credential_guid (the direct-mode route).
            cred_ref = input.credential_ref
            if cred_ref is None and input.credential_guid:
                cred_ref = _guid_credential_ref(input.credential_guid)

            workflow_args = {
                "workflow_id": workflow_id,
                "connection": input.connection,
                "credential_guid": input.credential_guid,
                "credential_ref": cred_ref,
                "output_prefix": input.output_prefix,
                "output_path": input.output_path,
                "lookback_days": input.lookback_days,
                "batch_size": input.batch_size,
            }

            batch_result = await self.get_query_batches(
                QueryBatchInput(workflow_args=workflow_args)
            )

            total_queries = 0
            for batch_num in range(batch_result.total_batches):
                fetch_result = await self.fetch_queries(
                    QueryFetchInput(
                        workflow_args=workflow_args,
                        batch_number=batch_num,
                        batch_size=batch_result.batch_size,
                    )
                )
                total_queries += fetch_result.queries_fetched

            logger.info(
                "Query extraction completed: workflow_id=%s batches=%d queries=%d",
                workflow_id,
                batch_result.total_batches,
                total_queries,
            )

            return QueryExtractionOutput(
                workflow_id=workflow_id,
                success=True,
                total_batches=batch_result.total_batches,
                total_queries=total_queries,
            )

        # conformance: ignore[E004] top-level run() handler; re-raises as typed SqlQueryExtractionError, exception context preserved via from e
        except Exception as e:
            from application_sdk.templates._template_errors import (  # noqa: PLC0415
                SqlQueryExtractionError,
            )

            raise SqlQueryExtractionError(workflow_id=str(workflow_id), cause=e) from e


# ── Deprecated re-exports ────────────────────────────────────────────────────
#: name -> (replacement, why). This module imported ``legacy_credential_ref``
#: until FND-4691 switched it to the private GUID constructor, which left the
#: name importable from here only by accident of that import. Served once more
#: by ``__getattr__`` below (PEP 562) so the removal waits for v4.0.0.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "legacy_credential_ref": (
        "application_sdk.credentials.ref.legacy_credential_ref",
        "it was only ever re-exported here as a side effect of an import",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the removed re-exports once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated here; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    from application_sdk.credentials import ref as _ref  # noqa: PLC0415

    return getattr(_ref, name)
