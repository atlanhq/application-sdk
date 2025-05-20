import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, HttpUrl

from application_sdk.docgen.models.export.page import Page
from application_sdk.docgen.models.manifest import DocsManifest
from application_sdk.observability.logger_adaptor import get_logger


class MkDocsConfig(BaseModel):
    """MkDocs configuration.

    A model representing the configuration for MkDocs static site generator.

    Attributes:
        site_name (str): Name of the documentation site.
        site_description (str): Description of the documentation site.
        site_url (str): Base URL where the site will be hosted.
        repo_url (str): URL of the source code repository.
        repo_name (str): Display name for the repository.
        site_author (str): Author of the documentation site.
        copyright (str): Copyright notice for the site.
        theme (Dict[str, Any]): Dictionary containing theme configuration.
        nav (List[Dict[str, Any]]): List of dictionaries defining the navigation structure.
        extra (Dict[str, Any]): Dictionary containing additional configuration options.
    """

    site_name: str
    site_description: str
    site_url: Optional[HttpUrl]
    repo_url: Optional[HttpUrl]
    site_author: str
    copyright: str
    theme: Dict[str, Any]
    nav: List[Dict[str, Any]]
    extra: Dict[str, Any]


class MkDocsExporter:
    """Exports documentation to MkDocs format.

    Generates mkdocs.yml configuration file with navigation structure based on manifest.

    Args:
        docs_directory (str): Base documentation directory path
        manifest (CustomerDocsManifest): Manifest containing documentation structure
    """

    def __init__(self, manifest: DocsManifest, export_path: str):
        self.logger = get_logger(__name__)

        self.manifest = manifest
        self.export_path = export_path

        os.makedirs(self.export_path, exist_ok=True)

    def generate_config(self) -> MkDocsConfig:
        config = MkDocsConfig(
            site_name=self.manifest.name,
            site_description=self.manifest.description,
            site_url=self.manifest.homepage,
            repo_url=self.manifest.repository,
            site_author=self.manifest.author,
            copyright=f"Copyright © {datetime.now().year} {self.manifest.author}",
            theme={},
            nav=[],
            extra={},
        )

        config.theme = {
            "name": "material",
            "palette": {
                "primary": "indigo",
                "accent": "pink",
            },
            "features": [
                "navigation.tabs",
                "navigation.tabs.sticky",
                "navigation.top",
                "navigation.tracking",
                "navigation.path",
            ],
        }

        return config

    def export(self, pages: List[Page]) -> None:
        """Generate mkdocs.yml in the specified export path.

        Args:
            export_path (str): Directory to write mkdocs.yml
        """
        config = self.generate_config()
        config.nav = self.generate_nav(pages=pages)

        with open(os.path.join(self.export_path, "mkdocs.yml"), "w") as f:
            yaml.dump(json.loads(config.model_dump_json()), f)

        docs_dir = os.path.join(self.export_path, "docs")
        os.makedirs(docs_dir, exist_ok=True)

        for page in pages:
            with open(os.path.join(docs_dir, f"{page.id}.md"), "w") as f:
                f.write(page.content)

        self.logger.info(f"Documentation exported to {self.export_path}")

    def generate_nav(self, pages: List[Page]) -> List[Dict[str, Any]]:
        """Generate navigation structure from manifest pages.

        Returns:
            List of nav entries for mkdocs.yml
        """
        nav: List[Dict[str, Any]] = []

        for page in pages:
            page_entry = {page.title: f"{page.id}.md"}
            nav.append(page_entry)

        return nav
