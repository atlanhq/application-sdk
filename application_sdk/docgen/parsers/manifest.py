"""Parser module for handling documentation manifest files.

This module provides functionality to parse both customer-facing and internal
documentation manifest files in YAML format. It includes utilities to locate
manifest files within a specified directory and convert them into strongly-typed
manifest objects.
"""

import os
from typing import Any, Dict

import yaml

from application_sdk.docgen.models.manifest import (
    CustomerDocsManifest,
    InternalDocsManifest,
)
from application_sdk.docgen.parsers.constants import (
    CUSTOMER_MANIFEST_FILE_NAMES,
    INTERNAL_MANIFEST_FILE_NAMES,
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

    def _find_customer_manifest_path(self) -> str:
        """Locate the customer manifest file in the docs directory.

        Searches through predefined customer manifest file names in the specified
        docs directory.

        Returns:
            str: The full path to the found customer manifest file.

        Raises:
            FileNotFoundError: If no customer manifest file is found in the directory.
        """
        for manifest_name in CUSTOMER_MANIFEST_FILE_NAMES:
            path = os.path.join(self.docs_directory, manifest_name)
            if os.path.exists(path):
                return path
        paths_tried = [
            os.path.join(self.docs_directory, name)
            for name in CUSTOMER_MANIFEST_FILE_NAMES
        ]
        raise FileNotFoundError(
            f"Could not find customer manifest file. Tried paths: {', '.join(paths_tried)}"
        )

    def _find_internal_manifest_path(self) -> str:
        """Locate the internal manifest file in the docs directory.

        Searches through predefined internal manifest file names in the specified
        docs directory.

        Returns:
            str: The full path to the found internal manifest file.

        Raises:
            FileNotFoundError: If no internal manifest file is found in the directory.
        """
        for manifest_name in INTERNAL_MANIFEST_FILE_NAMES:
            path = os.path.join(self.docs_directory, manifest_name)
            if os.path.exists(path):
                return path
        paths_tried = [
            os.path.join(self.docs_directory, name)
            for name in INTERNAL_MANIFEST_FILE_NAMES
        ]
        raise FileNotFoundError(
            f"Could not find internal manifest file. Tried paths: {', '.join(paths_tried)}"
        )

    def read_manifest_file(self, file_path: str) -> Dict[str, Any]:
        """Read and parse a YAML manifest file.

        Args:
            file_path: Path to the YAML manifest file.

        Returns:
            Dict[str, Any]: The parsed manifest file as a dictionary.

        Raises:
            FileNotFoundError: If the specified file cannot be opened.
            yaml.YAMLError: If the file contains invalid YAML syntax.
        """
        with open(file_path, "r") as f:
            manifest = yaml.safe_load(f)
            return manifest

    def parse_customer_manifest(self) -> CustomerDocsManifest:
        """Parse the customer documentation manifest file.

        Locates and parses the customer manifest file, converting it into a
        strongly-typed CustomerDocsManifest object.

        Returns:
            CustomerDocsManifest: A typed representation of the customer manifest.

        Raises:
            FileNotFoundError: If the customer manifest file cannot be found or read.
            ValueError: If the manifest file contains invalid YAML syntax.
        """
        try:
            customer_manifest_dict = self.read_manifest_file(
                self._find_customer_manifest_path()
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Failed to read customer manifest file: {str(e)}")
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in customer manifest file: {str(e)}")

        return CustomerDocsManifest(**customer_manifest_dict)

    def parse_internal_manifest(self) -> InternalDocsManifest:
        """Parse the internal documentation manifest file.

        Locates and parses the internal manifest file, converting it into a
        strongly-typed InternalDocsManifest object.

        Returns:
            InternalDocsManifest: A typed representation of the internal manifest.

        Raises:
            FileNotFoundError: If the internal manifest file cannot be found or read.
            ValueError: If the manifest file contains invalid YAML syntax.
        """
        try:
            internal_manifest_dict = self.read_manifest_file(
                self._find_internal_manifest_path()
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Failed to read internal manifest file: {str(e)}")
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in internal manifest file: {str(e)}")

        return InternalDocsManifest(**internal_manifest_dict)
