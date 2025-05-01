import unittest
from unittest.mock import Mock, patch

from application_sdk.docgen.parsers.directory import (
    DirectoryParser,
    DirectoryParsingResult,
    DocsSubDirectory,
)


class TestDirectoryParser(unittest.TestCase):
    def setUp(self):
        self.test_docs_dir = "/fake/docs/path"
        self.parser = DirectoryParser(self.test_docs_dir)

    def test_init(self):
        """Test DirectoryParser initialization"""
        assert self.parser.docs_directory == self.test_docs_dir
        assert self.parser.valid_image_extensions == (".png", ".jpg", ".jpeg", ".gif")
        assert self.parser.valid_video_extensions == (".mp4", ".mov", ".avi", ".mkv")
        assert self.parser.valid_walkthrough_extensions == (".html",)
        assert self.parser.valid_content_extensions == (".md",)
        assert self.parser.valid_openapi_extensions == (".json",)

    @patch("glob.glob")
    def test_validate_directory_contents_empty_directory(self, mock_glob: Mock):
        """Test directory validation when directory is empty"""
        mock_glob.return_value = []
        result = self.parser.validate_directory_contents(
            DocsSubDirectory.IMAGES,
            [self.parser.validate_file_extension(self.parser.valid_image_extensions)],
        )

        assert result is True

    @patch("glob.glob")
    def test_validate_directory_contents_valid_files(self, mock_glob: Mock):
        """Test directory validation with valid files"""
        mock_glob.return_value = [
            "/fake/docs/path/images/test1.png",
            "/fake/docs/path/images/test2.jpg",
        ]
        result = self.parser.validate_directory_contents(
            DocsSubDirectory.IMAGES,
            [self.parser.validate_file_extension(self.parser.valid_image_extensions)],
        )

        assert result is True

    @patch("glob.glob")
    @patch("os.path.isfile")
    def test_validate_directory_contents_invalid_files(
        self, mock_is_file: Mock, mock_glob: Mock
    ):
        """Test directory validation with invalid files"""
        mock_glob.return_value = [
            "/fake/docs/path/images/test1.png",
            "/fake/docs/path/images/invalid.txt",
        ]
        mock_is_file.return_value = True
        result = self.parser.validate_directory_contents(
            DocsSubDirectory.IMAGES,
            [self.parser.validate_file_extension(self.parser.valid_image_extensions)],
        )

        assert result is False

    def test_validate_file_extension(self):
        """Test file extension validator"""
        validator = self.parser.validate_file_extension((".txt", ".md"))
        assert validator("test.txt") is True
        assert validator("test.md") is True
        assert validator("test.TXT") is True
        assert validator("test.pdf") is False

    @patch.object(DirectoryParser, "validate_directory_contents")
    def test_check_images_sub_directory(self, mock_validate: Mock):
        """Test images subdirectory validation"""
        mock_validate.return_value = True
        assert self.parser.check_images_sub_directory() is True
        mock_validate.assert_called_once()

    @patch.object(DirectoryParser, "validate_directory_contents")
    def test_check_videos_sub_directory(self, mock_validate: Mock):
        """Test videos subdirectory validation"""
        mock_validate.return_value = True
        assert self.parser.check_videos_sub_directory() is True
        mock_validate.assert_called_once()

    @patch.object(DirectoryParser, "validate_directory_contents")
    def test_check_content_sub_directory(self, mock_validate: Mock):
        """Test content subdirectory validation"""
        mock_validate.return_value = True
        assert self.parser.check_content_sub_directory() is True
        mock_validate.assert_called_once()

    @patch.object(DirectoryParser, "validate_directory_contents")
    def test_check_walkthroughs_sub_directory(self, mock_validate: Mock):
        """Test walkthroughs subdirectory validation"""
        mock_validate.return_value = True
        assert self.parser.check_walkthroughs_sub_directory() is True
        mock_validate.assert_called_once()

    @patch.object(DirectoryParser, "validate_directory_contents")
    def test_check_openapi_sub_directory(self, mock_validate: Mock):
        """Test OpenAPI subdirectory validation"""
        mock_validate.return_value = True
        assert self.parser.check_openapi_sub_directory() is True
        mock_validate.assert_called_once()

    @patch.object(DirectoryParser, "check_images_sub_directory")
    @patch.object(DirectoryParser, "check_videos_sub_directory")
    @patch.object(DirectoryParser, "check_content_sub_directory")
    @patch.object(DirectoryParser, "check_walkthroughs_sub_directory")
    @patch.object(DirectoryParser, "check_openapi_sub_directory")
    def test_parse(
        self,
        mock_openapi: Mock,
        mock_walkthroughs: Mock,
        mock_content: Mock,
        mock_videos: Mock,
        mock_images: Mock,
    ):
        """Test overall parsing functionality"""
        # Set all validations to True
        mock_images.return_value = True
        mock_videos.return_value = True
        mock_content.return_value = True
        mock_walkthroughs.return_value = True
        mock_openapi.return_value = True

        result = self.parser.parse()

        self.assertIsInstance(result, DirectoryParsingResult)
        self.assertTrue(result.images_valid)
        self.assertTrue(result.videos_valid)
        self.assertTrue(result.content_valid)
        self.assertTrue(result.walkthroughs_valid)
        self.assertTrue(result.openapi_valid)

        # Test with some failures
        mock_images.return_value = False
        mock_videos.return_value = False

        result = self.parser.parse()

        self.assertFalse(result.images_valid)
        self.assertFalse(result.videos_valid)
        self.assertTrue(result.content_valid)
        self.assertTrue(result.walkthroughs_valid)
        self.assertTrue(result.openapi_valid)


if __name__ == "__main__":
    unittest.main()
