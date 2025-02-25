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
    """Docs Generator for Atlan Apps.

    This class handles parsing documentation manifests, validating content,
    and exporting the documentation to MkDocs format.

    Args:
        docs_directory_path (str): Path to the directory containing documentation files.
        export_path (str): Path where the generated documentation will be exported.
    """

    def __init__(self, docs_directory_path: str, export_path: str) -> None:
        self.logger = AtlanLoggerAdapter(str(logging.getLogger(__name__)))

        self.docs_directory_path = docs_directory_path
        self.export_path = export_path

        # Initialize Parsers and Validators
        self.manifest_parser = ManifestParser(docs_directory=self.docs_directory_path)
        self.directory_parser = DirectoryParser(docs_directory=self.docs_directory_path)

    def verify(self):
        """Verify the manifest content meets minimum requirements.

        Raises:
            ValueError: If manifest doesn't contain at least one page or supported feature.
        """
        manifest = self.manifest_parser.parse_manifest()

        if len(manifest.customer.pages) == 0:
            raise ValueError("Manifest must contain at least one page")

        if len(manifest.customer.supported_features) == 0:
            raise ValueError("Manifest must contain at least one supported feature")

    def export(self):
        """Export the documentation to MkDocs format.

        This method:
        1. Parses the manifest file
        2. Validates the directory structure
        3. Generates an index page with supported features
        4. Processes all additional pages
        5. Exports to MkDocs format
        6. Builds the final MkDocs site

        Raises:
            Exception: Any exception that occurs during manifest parsing or export process.
        """

        try:
            from mkdocs import config
            from mkdocs.commands import build
        except ImportError:
            self.logger.warning(
                "mkdocs is not installed. Please install it using 'pip install mkdocs'"
            )
            return

        try:
            manifest = self.manifest_parser.parse_manifest()
        except Exception as e:
            raise e

        directory_parser_result = self.directory_parser.parse()
        for attr, value in directory_parser_result:
            self.logger.info(f"Directory validation - {attr}: {value}")

        pages: List[Page] = []

        index_page_content = ""

        index_page_content += f"# {manifest.name}\n\n"
        index_page_content += f"{manifest.description}\n\n"

        index_page_content += "## Supported Features\n\n"
        index_page_content += "| Name | Supported | Notes |\n"
        index_page_content += "|------|-----------|-------|\n"
        for feature in manifest.customer.supported_features:
            index_page_content += f"| {feature.name} | {'✅' if feature.supported else '❌'} | {feature.notes.strip()} |\n"

        index_page = Page(
            id="index",
            title=manifest.name,
            content=index_page_content,
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

        # Build the MkDocs site
        cfg = config.load_config(  # type: ignore
            config_file=os.path.join(self.export_path, "mkdocs.yml")
        )

        cfg.plugins.on_startup(command="build", dirty=False)

        try:
            build.build(cfg, dirty=False)
        finally:
            cfg.plugins.on_shutdown()

        self.logger.info(f"Documentation exported to {self.export_path}")
