import argparse
import http.server
import logging
import os
import socketserver
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

        # Generate index page content
        # TODO: move this to a separate function

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

        self.logger.info(f"Documentation exported to {self.export_path}")


def create_cli_parser():
    parser = argparse.ArgumentParser(description="Atlan Documentation Generator CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Build command
    subparsers.add_parser("build", help="Build the documentation")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Serve the documentation")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to serve on")

    return parser


def setup_docs(docs_directory_path: str, export_path: str):
    parser = create_cli_parser()
    args = parser.parse_args()

    generator = AtlanDocsGenerator(docs_directory_path, export_path)
    generator.export()

    os.chdir(export_path)
    os.system("mkdocs build")

    if args.command == "build":
        print("Docs written to", export_path)
    elif args.command == "serve":
        print("Serving docs on port", args.port)
        os.chdir("site")
        handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer(("", args.port), handler) as httpd:
            print(f"Serving at http://localhost:{args.port}")
            httpd.serve_forever()
