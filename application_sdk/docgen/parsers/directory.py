import enum
import glob
import os
from typing import Callable, List, Tuple

import pydantic
from loguru import logger


class DocsSubDirectory(enum.Enum):
    """Enumeration of subdirectories in the documentation directory.

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


class DirectoryParsingResult(pydantic.BaseModel):
    """Model representing the results of directory parsing validation.

    Each attribute corresponds to a subdirectory's validation status.

    Attributes:
        images_valid: Whether the images subdirectory passes validation.
        videos_valid: Whether the videos subdirectory passes validation.
        walkthroughs_valid: Whether the walkthroughs subdirectory passes validation.
        content_valid: Whether the content subdirectory passes validation.
        openapi_valid: Whether the OpenAPI subdirectory passes validation.
    """

    images_valid: bool = False
    videos_valid: bool = False
    walkthroughs_valid: bool = False
    content_valid: bool = False
    openapi_valid: bool = False


class DirectoryParser:
    """Parser for documentation directory structure and content validation.

    This class enforces guidelines for documentation organization including required
    manifest files, proper file types in each subdirectory, and directory structure
    compliance.

    Args:
        docs_directory(str): Base path to the docs directory.

    Attributes:
        docs_directory: Base path to the documentation directory.
        valid_image_extensions: Tuple of allowed image file extensions.
        valid_video_extensions: Tuple of allowed video file extensions.
        valid_walkthrough_extensions: Tuple of allowed walkthrough file extensions.
        valid_content_extensions: Tuple of allowed content file extensions.
    """

    def __init__(self, docs_directory: str):
        self.logger = logger

        self.docs_directory = docs_directory

        self.valid_image_extensions = (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
        )
        self.valid_video_extensions = (
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
        )
        self.valid_walkthrough_extensions = (".html",)
        self.valid_content_extensions = (".md",)
        self.valid_openapi_extensions = (".json",)

    def parse(self) -> DirectoryParsingResult:
        """Parse and validate the entire documentation directory structure.

        Performs all validation checks including manifest files and subdirectory contents.

        Returns:
            DirectoryParsingResult: A model containing the validation results for each subdirectory.
        """
        images_valid = self.check_images_sub_directory()
        videos_valid = self.check_videos_sub_directory()
        content_valid = self.check_content_sub_directory()
        walkthroughs_valid = self.check_walkthroughs_sub_directory()
        openapi_valid = self.check_openapi_sub_directory()

        return DirectoryParsingResult(
            images_valid=images_valid,
            videos_valid=videos_valid,
            content_valid=content_valid,
            walkthroughs_valid=walkthroughs_valid,
            openapi_valid=openapi_valid,
        )

    # Directory content checks
    def check_images_sub_directory(self) -> bool:
        """Validate all files in the images directory.

        Ensures all files have valid image extensions as defined in VALID_IMAGE_EXTENSIONS.

        Returns:
            bool: True if all files are valid images, False otherwise.
        """
        return self.validate_directory_contents(
            subdir=DocsSubDirectory.IMAGES,
            validators=[self.validate_file_extension(self.valid_image_extensions)],
        )

    def check_videos_sub_directory(self) -> bool:
        """Validate all files in the videos directory.

        Ensures all files have valid video extensions as defined in VALID_VIDEO_EXTENSIONS.

        Returns:
            bool: True if all files are valid videos, False otherwise.
        """
        return self.validate_directory_contents(
            subdir=DocsSubDirectory.VIDEOS,
            validators=[self.validate_file_extension(self.valid_video_extensions)],
        )

    def check_content_sub_directory(self) -> bool:
        """Validate all files in the content directory.

        Ensures all files have valid content extensions as defined in VALID_CONTENT_EXTENSIONS.

        Returns:
            bool: True if all files are valid content files, False otherwise.
        """
        return self.validate_directory_contents(
            subdir=DocsSubDirectory.CONTENT,
            validators=[self.validate_file_extension(self.valid_content_extensions)],
        )

    def check_walkthroughs_sub_directory(self) -> bool:
        """Validate all files in the walkthroughs directory.

        Ensures all files have valid walkthrough extensions as defined in self.valid_walkthrough_extensions.

        Returns:
            bool: True if all files pass validation, False otherwise.
        """
        return self.validate_directory_contents(
            subdir=DocsSubDirectory.WALKTHROUGHS,
            validators=[
                self.validate_file_extension(self.valid_walkthrough_extensions)
            ],
        )

    def check_openapi_sub_directory(self) -> bool:
        """Validate all files in the OpenAPI directory.

        Ensures all files have valid OpenAPI extensions as defined in VALID_OPENAPI_EXTENSIONS.

        Returns:
            bool: True if all files are valid OpenAPI files, False otherwise.
        """
        return self.validate_directory_contents(
            subdir=DocsSubDirectory.OPENAPI,
            validators=[self.validate_file_extension(self.valid_openapi_extensions)],
        )

    def validate_directory_contents(
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
                os.path.join(self.docs_directory, subdir.value, "**", "*"),
                recursive=True,
            )
            if os.path.isfile(f)
        ]

        if not files:
            self.logger.debug(
                f"No files found in {subdir.value} directory at {os.path.join(self.docs_directory, subdir.value)}"
            )
            return True

        results: List[bool] = []

        for validator in validators:
            results.append(all([validator(file) for file in files]))

        return all(results)

    @staticmethod
    def validate_file_extension(
        valid_extensions: Tuple[str, ...],
    ) -> Callable[[str], bool]:
        """Create a validator function for checking file extensions.

        Args:
            valid_extensions: Tuple of allowed file extensions.

        Returns:
            Callable[[str], bool]: Function that validates if a file has an allowed extension.
        """
        return lambda file_path: file_path.lower().endswith(valid_extensions)
