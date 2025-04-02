"""
Docusaurus exporter for Atlan documentation.

This module provides functionality to export documentation to the Atlan Docusaurus documentation site.
It handles cloning the docs repository, installing dependencies, copying documentation, and building the site.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.docgen.models.export.page import Page
from application_sdk.docgen.models.manifest import DocsManifest


class DocusaurusExportError(Exception):
    """Base exception for Docusaurus export errors."""

    pass


class DocusaurusExporter:
    """
    Exports documentation to Atlan's Docusaurus site.

    This class handles:
    - Cloning the Atlan docs repository
    - Installing NPM dependencies
    - Copying documentation to the correct location
    - Building the Docusaurus site

    Args:
        manifest (DocsManifest): Manifest containing documentation structure
        export_path (str): Directory to export documentation to
        docs_repo_url (str): URL of the docs repository
        target_docs_dir (str): Target directory for documentation within the repo
        branch (str): Branch to clone and work with
    """

    def __init__(
        self,
        manifest: DocsManifest,
        export_path: str,
        docs_repo_url: str = "git@github.com:atlanhq/atlan-docs.git",
        target_docs_dir: str = "docs/apps/connectors",
        branch: str = "main",
    ):
        self.logger = AtlanLoggerAdapter(__name__)
        self.manifest = manifest
        self.export_path = Path(export_path)
        self.docs_repo_url = docs_repo_url
        self.target_docs_dir = target_docs_dir
        self.branch = branch
        self.repo_path: Path = Path("/tmp/atlan-docs")

        self.export_path.mkdir(parents=True, exist_ok=True)

    def _run_command(
        self,
        command: List[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        error_message: str = "Command failed",
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell command with error handling."""
        try:
            self.logger.debug(f"Running command: {' '.join(command)}")
            result = subprocess.run(
                command,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                check=True,
                capture_output=True,
                text=True,
            )
            self.logger.debug(f"Command output: {result.stdout}")
            return result
        except subprocess.CalledProcessError as e:
            error_detail = f"{error_message}: {e.stderr}"
            self.logger.error(error_detail)
            raise DocusaurusExportError(error_detail) from e
        except Exception as e:
            error_detail = f"{error_message}: {str(e)}"
            self.logger.error(error_detail)
            raise DocusaurusExportError(error_detail) from e

    def _clone_docs_repo(self) -> None:
        """Clone the documentation repository."""
        try:
            # Check if directory already exists
            if os.path.exists(self.repo_path):
                self.logger.info(f"Repository already exists at: {self.repo_path}")
                return

            self.logger.info(f"Cloning docs repository to: {self.repo_path}")

            self._run_command(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "-b",
                    self.branch,
                    self.docs_repo_url,
                    str(self.repo_path),
                ],
                error_message="Failed to clone docs repository",
            )

            self.logger.info("Successfully cloned docs repository")
        except Exception as e:
            self.cleanup()
            raise DocusaurusExportError(f"Failed to clone repository: {str(e)}") from e

    def _install_dependencies(self) -> None:
        """Install NPM dependencies."""
        if not self.repo_path:
            raise DocusaurusExportError("Repository not cloned")

        try:
            self.logger.info("Installing NPM dependencies")
            self._run_command(
                ["npm", "install"],
                cwd=str(self.repo_path),
                error_message="Failed to install NPM dependencies",
            )
            self.logger.info("Successfully installed NPM dependencies")
        except Exception as e:
            raise DocusaurusExportError(
                f"Failed to install dependencies: {str(e)}"
            ) from e

    def _copy_pages(self, pages: List[Page]) -> None:
        """Copy documentation pages to the target directory."""
        if not os.path.exists(self.repo_path):
            raise DocusaurusExportError("Repository not cloned")

        try:
            target_path = os.path.join(
                self.repo_path, self.target_docs_dir, self.manifest.name
            )
            os.makedirs(target_path, exist_ok=True)

            self.logger.info(f"Copying documentation pages to {target_path}")

            # Write each page to the target directory
            for page in pages:
                page_path = os.path.join(target_path, f"{page.id}.md")
                os.makedirs(os.path.dirname(page_path), exist_ok=True)

                with open(page_path, "w") as f:
                    self.logger.debug(f"Writing page content to {page_path}")
                    f.write(page.content)

            self.logger.info("Successfully copied documentation pages")
        except Exception as e:
            raise DocusaurusExportError(
                f"Failed to copy documentation: {str(e)}"
            ) from e

    def _build_docs(self) -> None:
        """Build the Docusaurus site."""
        if not os.path.exists(self.repo_path):
            raise DocusaurusExportError("Repository not cloned")

        try:
            self.logger.info("Building Docusaurus site")
            self._run_command(
                ["npm", "run", "build"],
                cwd=str(self.repo_path),
                error_message="Failed to build Docusaurus site",
            )
            self.logger.info("Successfully built Docusaurus site")
        except Exception as e:
            raise DocusaurusExportError(
                f"Failed to build documentation: {str(e)}"
            ) from e

    def cleanup(self) -> None:
        """Clean up temporary directory and resources."""
        pass

    def export(self, pages: List[Page]) -> None:
        """
        Export documentation pages to Docusaurus.

        This method orchestrates the entire export process:
        1. Clone the docs repository
        2. Install dependencies
        3. Copy documentation pages
        4. Build the site

        Args:
            pages: List of documentation pages to export

        Raises:
            DocusaurusExportError: If any step of the export process fails
        """
        try:
            self._clone_docs_repo()
            self._install_dependencies()
            self._copy_pages(pages)
            self._build_docs()

            # Copy the built site to the export path
            if not os.path.exists(self.repo_path):
                raise DocusaurusExportError("Repository not cloned")

            build_dir = self.repo_path / "build"
            if not os.path.exists(build_dir):
                raise DocusaurusExportError("Build directory not found")

            # Clean up existing export path before copying
            if self.export_path.exists():
                self.logger.debug(
                    f"Cleaning up existing export path: {self.export_path}"
                )
                shutil.rmtree(self.export_path)
            self.export_path.mkdir(parents=True)
            shutil.copytree(build_dir, self.export_path, dirs_exist_ok=True)
            self.logger.info(f"Documentation exported to {self.export_path}")
        except Exception as e:
            self.logger.error(f"Documentation export failed: {str(e)}")
            raise DocusaurusExportError(str(e)) from e
        finally:
            self.cleanup()
