import logging
import os
from datetime import datetime
from typing import List

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.docgen.exporters.mkdocs import MkDocsExporter
from application_sdk.docgen.models.export.page import Page
from application_sdk.docgen.parsers.directory import DirectoryParser
from application_sdk.docgen.parsers.manifest import ManifestParser


class AtlanDocsGenerator:
    def __init__(self, docs_directory_path: str, export_path: str) -> None:
        self.logger = AtlanLoggerAdapter(logging.getLogger(__name__))

        self.docs_directory_path = docs_directory_path
        self.export_path = export_path

        # Initialize Parsers and Validators
        self.manifest_parser = ManifestParser(docs_directory=self.docs_directory_path)
        self.directory_parser = DirectoryParser(docs_directory=self.docs_directory_path)

    def export(self):
        try:
            manifest = self.manifest_parser.parse_manifest()
        except Exception as e:
            raise e

        directory_parser_result = self.directory_parser.parse()
        for attr, value in directory_parser_result:
            self.logger.info(f"Directory validation - {attr}: {value}")

        pages: List[Page] = []

        index_page = Page(
            id="index",
            title="Home",
            content=manifest.description,
            last_updated=datetime.now().isoformat(),
            path="index.md",
        )

        pages.append(index_page)

        for page in manifest.customer.pages:
            if page.fileRef:
                with open(
                    os.path.join(self.docs_directory_path, page.fileRef), "r"
                ) as f:
                    content = f.read()

                    page = Page(
                        id=page.id,
                        title=page.name,
                        content=content,
                        last_updated=datetime.now().isoformat(),
                        path=page.fileRef,
                    )
                    pages.append(page)

        mkdocs_exporter = MkDocsExporter(
            manifest=manifest, export_path=self.export_path
        )
        mkdocs_exporter.export(pages=pages)

        self.logger.info(f"Documentation exported to {self.export_path}")
