import os
import shutil

from application_sdk.docgen.models.manifest import InternalDocsManifest


class InternalDocsExporter:
    def __init__(self, docs_directory: str, manifest: InternalDocsManifest):
        self.docs_directory = docs_directory
        self.manifest = manifest

    def export(self, export_path: str):
        # Create export directory if it doesn't exist
        os.makedirs(export_path, exist_ok=True)

        # Loop through pages with enumeration for ordering
        for page_num, page in enumerate(self.manifest.pages, start=1):
            # Read page content from fileRef
            page_file_path = os.path.join(self.docs_directory, page.fileRef)
            page_output_path = os.path.join(export_path, f"{page_num:02d}_{page.id}.md")

            # Copy page file to output with ordered name
            shutil.copy2(page_file_path, page_output_path)

            # Create sections directory for this page
            page_sections_dir = os.path.join(export_path, f"{page_num:02d}_{page.id}")
            os.makedirs(page_sections_dir, exist_ok=True)

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
