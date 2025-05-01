"""Parser module for handling documentation manifest files.

This module provides functionality to parse both customer-facing and internal
documentation manifest files in YAML format. It includes utilities to locate
manifest files within a specified directory and convert them into strongly-typed
manifest objects.
"""

import os
from typing import Any, Dict, Tuple

import yaml

from application_sdk.docgen.models.manifest import DocsManifest

# List of possible manifest file names
MANIFEST_FILE_NAMES: Tuple[str, ...] = (
    "docs.manifest.yaml",
    "docs.manifest.yml",
)


class ManifestParser:
    """A parser class for handling documentation manifest files.

    This class provides methods to locate and parse both customer-facing and
    internal documentation manifest files from a specified directory.

    Args:
        docs_directory: The base directory path where manifest files are located.
    """

    def __init__(self, docs_directory: str) -> None:
        self.docs_directory = docs_directory

    def find_manifest_path(self) -> str:
        """Locate the customer manifest file in the docs directory.

        Searches through predefined customer manifest file names in the specified
        docs directory.

        Returns:
            str: The full path to the found customer manifest file.

        Raises:
            FileNotFoundError: If no customer manifest file is found in the directory.
        """
        for manifest_name in MANIFEST_FILE_NAMES:
            path = os.path.join(self.docs_directory, manifest_name)
            if os.path.exists(path):
                return path

        paths_tried = [
            os.path.join(self.docs_directory, name) for name in MANIFEST_FILE_NAMES
        ]

        raise FileNotFoundError(
            f"Could not find manifest file. Tried paths: {', '.join(paths_tried)}"
        )

    @staticmethod
    def read_manifest_file(file_path: str) -> Dict[str, Any]:
        """Read and parse a YAML manifest file.

        Args:
            file_path: Path to the YAML manifest file.

        Returns:
            Dict[str, Any]: The parsed manifest file as a dictionary.

        Raises:
            IOError: If the specified file cannot be opened and read.
            yaml.YAMLError: If the file contains invalid YAML syntax.
        """
        with open(file_path, "r") as f:
            try:
                manifest = yaml.safe_load(f)
                return manifest
            except yaml.YAMLError as e:
                raise yaml.YAMLError(f"Failed to parse YAML manifest: {str(e)}") from e
            except Exception as e:
                raise IOError(f"Error reading manifest file: {str(e)}") from e

    def parse_manifest(self) -> DocsManifest:
        """Parse the customer documentation manifest file.

        Locates and parses the customer manifest file, converting it into a
        strongly-typed CustomerDocsManifest object.

        Returns:
            CustomerDocsManifest: A typed representation of the customer manifest.

        Raises:
            Exception: If the manifest file cannot be found or read.
        """
        try:
            manifest_dict = self.read_manifest_file(self.find_manifest_path())
        except Exception as e:
            raise e

        return DocsManifest(**manifest_dict)
