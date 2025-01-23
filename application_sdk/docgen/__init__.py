import logging
from typing import Tuple

from application_sdk.docgen.exporters.customer import CustomerDocsExporter
from application_sdk.docgen.models.manifest import (
    CustomerDocsManifest,
    InternalDocsManifest,
)
from application_sdk.docgen.parsers.directories import DirectoryParser
from application_sdk.docgen.parsers.manifest import ManifestParser


class AtlanDocsGenerator:
    def __init__(self, docs_directory_path: str, export_path: str = "docs/out") -> None:
        self.docs_directory_path = docs_directory_path
        self.export_path = export_path
        self.logger = logging.getLogger(__name__)

    def parse(self) -> Tuple[CustomerDocsManifest, InternalDocsManifest]:
        directory_parser = DirectoryParser(directory_path=self.docs_directory_path)
        result = directory_parser.parse()
        if not result:
            self.logger.error("Directory does not have all the valid files")
            raise Exception("Directory does not have all the valid files")

        manifest_parser = ManifestParser(docs_directory=self.docs_directory_path)
        customer_manifest, internal_manifest = manifest_parser.parse()

        return customer_manifest, internal_manifest

    def export(self):
        customer_manifest, _ = self.parse()

        customer_exporter = CustomerDocsExporter(
            docs_directory=self.docs_directory_path, manifest=customer_manifest
        )

        customer_exporter.export(self.export_path)
