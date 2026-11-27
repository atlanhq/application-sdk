"""Unit tests for BaseMetadataExtractor — deprecated upload_to_atlan wrapper."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from application_sdk.app.base import App
from application_sdk.app.task import is_task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import UploadInput as StorageUploadInput
from application_sdk.contracts.storage import UploadOutput as StorageUploadOutput
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadToAtlanInput,
    UploadToAtlanOutput,
)


@dataclass
class _TestInput(Input):
    output_path: str = ""


@dataclass
class _TestOutput(Output):
    pass


class _ConcreteExtractor(BaseMetadataExtractor):
    async def run(self, input: _TestInput) -> _TestOutput:
        return _TestOutput()


class TestBaseMetadataExtractorStructure:
    def test_is_app_subclass(self) -> None:
        assert issubclass(BaseMetadataExtractor, App)

    def test_upload_to_atlan_is_registered_as_task(self) -> None:
        assert is_task(BaseMetadataExtractor.upload_to_atlan)

    def test_upload_to_atlan_signature(self) -> None:
        from typing import get_type_hints

        hints = get_type_hints(BaseMetadataExtractor.upload_to_atlan)
        assert hints.get("input") is UploadToAtlanInput
        assert hints.get("return") is UploadToAtlanOutput


class TestUploadToAtlanRedirect:
    """upload_to_atlan must redirect to App.upload and emit a DeprecationWarning."""

    @pytest.fixture
    def extractor(self):
        return _ConcreteExtractor()

    async def test_redirects_to_app_upload(self, extractor) -> None:
        extractor.upload = AsyncMock(
            return_value=StorageUploadOutput(
                ref=FileReference(file_count=10), synced=True, reason="uploaded"
            )
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = await extractor.upload_to_atlan(
                UploadToAtlanInput(output_path="/tmp/test/output")
            )

        extractor.upload.assert_awaited_once()
        forwarded = extractor.upload.await_args.args[0]
        assert isinstance(forwarded, StorageUploadInput)
        assert forwarded.local_path == "/tmp/test/output"
        assert forwarded.tier is StorageTier.RETAINED

        assert isinstance(result, UploadToAtlanOutput)
        assert result.migrated_files == 10
        assert result.total_files == 10

    async def test_emits_deprecation_warning(self, extractor) -> None:
        extractor.upload = AsyncMock(
            return_value=StorageUploadOutput(
                ref=FileReference(file_count=0), synced=False, reason="skipped"
            )
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            await extractor.upload_to_atlan(UploadToAtlanInput(output_path="/tmp/x"))

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "expected upload_to_atlan to emit a DeprecationWarning"
        assert "App.upload" in str(deprecations[0].message)

    async def test_propagates_upload_exception(self, extractor) -> None:
        extractor.upload = AsyncMock(side_effect=RuntimeError("boom"))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with pytest.raises(RuntimeError, match="boom"):
                await extractor.upload_to_atlan(
                    UploadToAtlanInput(output_path="/tmp/x")
                )


class TestSubclassDeprecationNotice:
    def test_notice_names_app_and_sql_app(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)

            class _NoticeExtractor(BaseMetadataExtractor):
                async def run(self, input: _TestInput) -> _TestOutput:
                    return _TestOutput()

        messages = [
            str(w.message)
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "_NoticeExtractor subclasses BaseMetadataExtractor" in str(w.message)
        ]
        assert len(messages) == 1
        assert "application_sdk.app.App" in messages[0]
        assert "application_sdk.templates.SqlApp" in messages[0]
        assert "v4.0.0" in messages[0]


class TestDeprecatedContractNames:
    """The old names share a name with the storage contracts; they warn and alias."""

    @pytest.mark.parametrize(
        ("module", "old", "new"),
        [
            (
                "application_sdk.templates.contracts.base_metadata_extraction",
                "UploadInput",
                UploadToAtlanInput,
            ),
            (
                "application_sdk.templates.contracts.base_metadata_extraction",
                "UploadOutput",
                UploadToAtlanOutput,
            ),
            ("application_sdk.templates.contracts", "UploadInput", UploadToAtlanInput),
            (
                "application_sdk.templates.contracts",
                "UploadOutput",
                UploadToAtlanOutput,
            ),
            (
                "application_sdk.templates.base_metadata_extractor",
                "UploadInput",
                UploadToAtlanInput,
            ),
            (
                "application_sdk.templates.base_metadata_extractor",
                "UploadOutput",
                UploadToAtlanOutput,
            ),
        ],
    )
    def test_old_name_warns_and_is_the_renamed_class(
        self, module: str, old: str, new: type
    ) -> None:
        import importlib

        mod = importlib.import_module(module)
        with pytest.warns(DeprecationWarning, match=f"{old} is deprecated"):
            assert getattr(mod, old) is new
