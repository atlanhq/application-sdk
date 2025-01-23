import os
import shutil

from application_sdk.docgen.exporters.mkdocs import MkDocsExporter
from application_sdk.docgen.models.manifest import CustomerDocsManifest


class CustomerDocsExporter:
    def __init__(self, docs_directory: str, manifest: CustomerDocsManifest):
        self.docs_directory = docs_directory
        self.manifest = manifest

    def export(self, export_path: str):
        # Create export directory if it doesn't exist

        os.makedirs(export_path, exist_ok=True)

        mkdocs_exporter = MkDocsExporter(
            docs_directory=self.docs_directory, manifest=self.manifest
        )
        mkdocs_exporter.export(export_path)

        export_path = os.path.join(export_path, "docs")

        # Create export directory if it doesn't exist
        os.makedirs(export_path, exist_ok=True)

        # Loop through pages with enumeration for ordering
        for page_num, page in enumerate(self.manifest.pages, start=1):
            # Read page content from fileRef
            page_file_path = os.path.join(self.docs_directory, page.fileRef)
            page_output_path = os.path.join(export_path, f"{page_num:02d}_{page.id}.md")

            # Copy page file to output with ordered name
            shutil.copy2(page_file_path, page_output_path)

            # Loop through sections
            for section_num, section in enumerate(page.sections, start=1):
                # Read section content from fileRef
                section_file_path = os.path.join(self.docs_directory, section.fileRef)

                # Read section content
                with open(section_file_path, "r") as section_file:
                    section_content = section_file.read()

                # Append section content to page file
                with open(page_output_path, "a") as page_file:
                    # Add section header
                    page_file.write(f"\n## {section.name}\n\n")
                    # Add section content
                    page_file.write(section_content)
                    page_file.write("\n")
