"""Unit tests for BaseMetadataExtractor template."""

from __future__ import annotations

from dataclasses import dataclass

from application_sdk.app.base import App
from application_sdk.contracts.base import Input, Output
from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor


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

    def test_concrete_subclass_instantiates(self) -> None:
        # The base class is now a thin App subclass — verify it still
        # composes correctly with a concrete run() implementation.
        ext = _ConcreteExtractor()
        assert isinstance(ext, App)
