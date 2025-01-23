import os
from typing import Any, Dict, List

import yaml

from application_sdk.docgen.models.manifest import CustomerDocsManifest


class MkDocsExporter:
    """Exports documentation to MkDocs format.

    Generates mkdocs.yml configuration file with navigation structure based on manifest.

    Args:
        docs_directory (str): Base documentation directory path
        manifest (CustomerDocsManifest): Manifest containing documentation structure
    """

    def __init__(self, docs_directory: str, manifest: CustomerDocsManifest):
        self.docs_directory = docs_directory
        self.manifest = manifest

    def export(self, export_path: str) -> None:
        """Generate mkdocs.yml in the specified export path.

        Args:
            export_path (str): Directory to write mkdocs.yml
        """
        mkdocs_config = self._generate_config()

        # Write mkdocs.yml
        output_file = os.path.join(export_path, "mkdocs.yml")
        with open(output_file, "w") as f:
            yaml.dump(mkdocs_config, f, default_flow_style=False)

    def _generate_config(self) -> Dict[str, Any]:
        """Generate MkDocs configuration dictionary.

        Returns:
            Dict containing MkDocs configuration
        """
        config = {
            "site_name": self.manifest.name,
            "site_description": self.manifest.description,
            # "nav": self._generate_nav()
        }

        # Add optional metadata if present
        if self.manifest.homepage:
            config["site_url"] = str(self.manifest.homepage)

        if self.manifest.author:
            config["site_author"] = self.manifest.author

        return config

    def _generate_nav(self) -> List[Dict[str, Any]]:
        """Generate navigation structure from manifest pages.

        Returns:
            List of nav entries for mkdocs.yml
        """
        nav: List[Dict[str, Any]] = []

        for page in self.manifest.pages:
            page_entry = {page.name: f"{page.id}.md"}

            nav.append(page_entry)

        return nav
