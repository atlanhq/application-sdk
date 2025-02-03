import logging

from application_sdk.docgen.exporters.customer import CustomerDocsExporter
from application_sdk.docgen.exporters.internal import InternalDocsExporter
from application_sdk.docgen.parsers.directories import DirectoryParser
from application_sdk.docgen.parsers.manifest import ManifestParser


class AtlanDocsGenerator:
    def __init__(self, docs_directory_path: str, export_path: str = "docs/out") -> None:
        self.logger = logging.getLogger(__name__)

        self.docs_directory_path = docs_directory_path
        self.export_path = export_path

        self.manifest_parser = ManifestParser(docs_directory=self.docs_directory_path)
        self.directory_parser = DirectoryParser(docs_directory=self.docs_directory_path)

    def export(self):
        try:
            customer_manifest = self.manifest_parser.parse_customer_manifest()
        except Exception as e:
            self.logger.error(f"Error parsing manifests: {e}")
            raise e

        internal_manifest = None
        try:
            internal_manifest = self.manifest_parser.parse_internal_manifest()
        except Exception as e:
            if isinstance(e, FileNotFoundError):
                self.logger.warning(f"Internal manifest file not found: {e}")
            else:
                self.logger.error(f"Error parsing manifests: {e}")
                raise e

        directory_parser_result = self.directory_parser.parse()
        for attr, value in directory_parser_result:
            self.logger.info(f"Directory validation - {attr}: {value}")

        customer_exporter = CustomerDocsExporter(
            docs_directory=self.docs_directory_path, manifest=customer_manifest
        )
        customer_exporter.export(self.export_path)

        if internal_manifest:
            internal_exporter = InternalDocsExporter(
                docs_directory=self.docs_directory_path, manifest=internal_manifest
            )
            internal_exporter.export(self.export_path)

        self.logger.info(f"Documentation exported to {self.export_path}")
