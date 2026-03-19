"""Base metadata extraction App — v3 implementation.

Provides the upload_to_atlan task and client/handler/transformer class
attributes for non-SQL (REST/API-based) metadata extraction connectors.

Subclasses must define their own run() with their own Input/Output contracts::

    from dataclasses import dataclass
    from application_sdk.app import task
    from application_sdk.contracts.base import Input, Output
    from application_sdk.templates import BaseMetadataExtractor
    from application_sdk.templates.contracts.base_metadata_extraction import UploadInput

    @dataclass
    class MyInput(Input):
        output_path: str = ""
        # add connector-specific fields here

    @dataclass
    class MyOutput(Output):
        pass

    class MyConnectorExtractor(BaseMetadataExtractor):
        async def run(self, input: MyInput) -> MyOutput:
            # fetch data, transform, then upload
            await self.upload_to_atlan(UploadInput(output_path=input.output_path))
            return MyOutput()

Client, handler, and transformer are cached in self.app_state after the first
task that initialises them — use self.app_state.get/set("client", ...) in your
@task methods rather than re-initialising on every call.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, ClassVar, Optional, Type

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.clients.base import BaseClient
from application_sdk.constants import (
    DEPLOYMENT_OBJECT_STORE_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.storage import (
    create_store_from_binding,
    download_file,
    list_keys,
    upload_file,
)
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadInput,
    UploadOutput,
)

if TYPE_CHECKING:
    from application_sdk.handlers.base import BaseHandler
    from application_sdk.transformers import TransformerInterface


class BaseMetadataExtractor(App):
    """Base App for non-SQL metadata extraction connectors.

    Provides:
    - Class attributes for plugging in a client, handler, and transformer.
    - A concrete upload_to_atlan task that migrates output files from the
      deployment object store to the upstream (Atlan) object store.

    Subclasses must implement run() with their own Input/Output contracts.
    See module docstring for a usage example.
    """

    client_class: ClassVar[Type[BaseClient]] = BaseClient
    handler_class: ClassVar[Type["BaseHandler"]]
    transformer_class: ClassVar[Optional[Type["TransformerInterface"]]] = None

    @task(timeout_seconds=1800)
    async def upload_to_atlan(self, input: UploadInput) -> UploadOutput:
        """Migrate output files from the deployment store to the upstream store.

        Lists all objects under input.output_path in the deployment store and
        copies each one to the same key in the upstream store. Raises on any
        migration failures.

        Args:
            input: UploadInput specifying the output_path prefix to migrate.

        Returns:
            UploadOutput with migrated_files and total_files counts.
        """
        deployment_store = create_store_from_binding(DEPLOYMENT_OBJECT_STORE_NAME)
        upstream_store = create_store_from_binding(UPSTREAM_OBJECT_STORE_NAME)

        files_to_migrate = await list_keys(input.output_path, store=deployment_store)
        total_files = len(files_to_migrate)
        migrated_files = 0
        failures = []

        for file_path in files_to_migrate:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            try:
                await download_file(file_path, tmp_path, store=deployment_store)
                await upload_file(file_path, tmp_path, store=upstream_store)
                migrated_files += 1
            except Exception as e:
                failures.append({"file": file_path, "error": str(e)})
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        if failures:
            from application_sdk.common.error_codes import ActivityError

            failed = len(failures)
            raise ActivityError(
                f"{ActivityError.ATLAN_UPLOAD_ERROR}: Atlan upload failed with "
                f"{failed} errors. Failed migrations: {failed}, "
                f"Total files: {total_files}"
            )

        return UploadOutput(migrated_files=migrated_files, total_files=total_files)
