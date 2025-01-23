import enum
import glob
import logging
import os
from typing import Callable, List, Tuple


class DocsSubDirectory(enum.Enum):
    """Enumeration of documentation subdirectories.

    Attributes:
        IMAGES: Directory containing image assets.
        VIDEOS: Directory containing video assets.
        WALKTHROUGHS: Directory containing walkthrough documentation.
        CONTENT: Directory containing main content files.
        OPENAPI: Directory containing OpenAPI specifications.
    """

    IMAGES = "images"
    VIDEOS = "videos"
    WALKTHROUGHS = "walkthroughs"
    CONTENT = "content"
    OPENAPI = "openapi"


class DirectoryParser:
    """Parser for documentation directory structure and content validation.

    This class enforces guidelines for documentation organization including required
    manifest files, proper file types in each subdirectory, and directory structure
    compliance.

    Args:
        directory_path(str): Base path to the documentation directory.

    Attributes:
        directory_path: Base path to the documentation directory.
        docs_manifest_file_name: Name of the main manifest file.
        docs_internal_manifest_file_name: Name of the internal manifest file.
        VALID_IMAGE_EXTENSIONS: Tuple of allowed image file extensions.
        VALID_VIDEO_EXTENSIONS: Tuple of allowed video file extensions.
        VALID_WALKTHROUGH_EXTENSIONS: Tuple of allowed walkthrough file extensions.
        VALID_CONTENT_EXTENSIONS: Tuple of allowed content file extensions.
    """

    def __init__(self, directory_path: str):
        self.directory_path = directory_path

        self.docs_manifest_file_name: str = "docs.customer.manifest.yaml"
        self.docs_internal_manifest_file_name: str = "docs.internal.manifest.yaml"

        self.VALID_IMAGE_EXTENSIONS = (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
        )
        self.VALID_VIDEO_EXTENSIONS = (
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
        )
        self.VALID_WALKTHROUGH_EXTENSIONS = (".html",)
        self.VALID_CONTENT_EXTENSIONS = (".md",)
        self.VALID_OPENAPI_EXTENSIONS = (".json",)

        self.logger = logging.getLogger(__name__)

    def parse(self) -> bool:
        """Parse and validate the entire documentation directory structure.

        Performs all validation checks including manifest files and subdirectory contents.

        Returns:
            bool: True if all validation checks pass, False if any check fails.
        """
        # Check manifest files
        manifest_valid = self.check_manifest_file()
        internal_manifest_valid = self.check_internal_manifest_file()

        # Check subdirectory contents
        images_valid = self.check_images_sub_directory()
        videos_valid = self.check_videos_sub_directory()
        content_valid = self.check_content_sub_directory()
        # TODO: add checks for walkthroughs and openapi

        # Return True only if all checks pass

        return all(
            [
                manifest_valid,
                internal_manifest_valid,
                images_valid,
                videos_valid,
                content_valid,
            ]
        )

    # Manifest file checks
    def check_manifest_file(self) -> bool:
        """Check if the main manifest file exists.

        Returns:
            bool: True if the manifest file exists, False otherwise.
        """
        return os.path.exists(
            os.path.join(self.directory_path, self.docs_manifest_file_name)
        )

    def check_internal_manifest_file(self) -> bool:
        """Check if the internal manifest file exists.

        Returns:
            bool: True if the internal manifest file exists, False otherwise.
        """
        return os.path.exists(
            os.path.join(self.directory_path, self.docs_internal_manifest_file_name)
        )

    # Directory content checks
    def check_images_sub_directory(self) -> bool:
        """Validate all files in the images directory.

        Ensures all files have valid image extensions as defined in VALID_IMAGE_EXTENSIONS.

        Returns:
            bool: True if all files are valid images, False otherwise.
        """
        return self._validate_directory_contents(
            subdir=DocsSubDirectory.IMAGES,
            validators=[self._validate_file_extension(self.VALID_IMAGE_EXTENSIONS)],
        )

    def check_videos_sub_directory(self) -> bool:
        """Validate all files in the videos directory.

        Ensures all files have valid video extensions as defined in VALID_VIDEO_EXTENSIONS.

        Returns:
            bool: True if all files are valid videos, False otherwise.
        """
        return self._validate_directory_contents(
            subdir=DocsSubDirectory.VIDEOS,
            validators=[self._validate_file_extension(self.VALID_VIDEO_EXTENSIONS)],
        )

    def check_content_sub_directory(self) -> bool:
        """Validate all files in the content directory.

        Ensures all files have valid content extensions as defined in VALID_CONTENT_EXTENSIONS.

        Returns:
            bool: True if all files are valid content files, False otherwise.
        """
        return self._validate_directory_contents(
            subdir=DocsSubDirectory.CONTENT,
            validators=[self._validate_file_extension(self.VALID_CONTENT_EXTENSIONS)],
        )

    def check_walkthroughs_sub_directory(self) -> bool:
        """Validate all files in the walkthroughs directory.

        Currently accepts all files as valid until walkthrough validation is implemented.

        Returns:
            bool: True if all files pass validation, False otherwise.
        """
        return self._validate_directory_contents(
            subdir=DocsSubDirectory.WALKTHROUGHS,
            validators=[
                self._validate_file_extension(self.VALID_WALKTHROUGH_EXTENSIONS)
            ],
        )

    def check_openapi_sub_directory(self) -> bool:
        """Validate all files in the OpenAPI directory.

        TODO: Implement OpenAPI validation.

        Returns:
            None: Currently not implemented.
        """
        # TODO: Implement OpenAPI validation
        return self._validate_directory_contents(
            subdir=DocsSubDirectory.OPENAPI,
            validators=[self._validate_file_extension(self.VALID_OPENAPI_EXTENSIONS)],
        )

    # Helper methods
    def _validate_directory_contents(
        self, subdir: DocsSubDirectory, validators: List[Callable[[str], bool]]
    ) -> bool:
        """Check all files in a subdirectory against validation functions.

        Args:
            subdir: Name of the subdirectory to validate.
            validators: List of callable validators to check files against.

        Returns:
            bool: True if all validators pass for all files, False otherwise.
        """
        files = [
            f
            for f in glob.glob(
                os.path.join(self.directory_path, subdir.value, "**", "*"),
                recursive=True,
            )
            if os.path.isfile(f)
        ]

        if not files:
            return True

        results: List[bool] = []

        for validator in validators:
            results.append(all([validator(file) for file in files]))

        return all(results)

    @staticmethod
    def _validate_file_extension(
        valid_extensions: Tuple[str, ...],
    ) -> Callable[[str], bool]:
        """Create a validator function for checking file extensions.

        Args:
            valid_extensions: Tuple of allowed file extensions.

        Returns:
            Callable[[str], bool]: Function that validates if a file has an allowed extension.
        """
        return lambda file_path: file_path.lower().endswith(valid_extensions)
