import os
import unittest
from unittest.mock import mock_open, patch

import yaml

from application_sdk.docgen.parsers.manifest import MANIFEST_FILE_NAMES, ManifestParser


class TestManifestParser(unittest.TestCase):
    def setUp(self):
        self.parser = ManifestParser("/tmp/docs/path")

    def test_manifest_parser_init(self):
        assert self.parser.docs_directory == "/tmp/docs/path"

    def test_find_manifest_path_success(self):
        for manifest_name in MANIFEST_FILE_NAMES:
            with self.subTest(manifest_name=manifest_name):
                expected_path = os.path.join("/tmp/docs/path", manifest_name)

                def mock_exists_side_effect(x: str) -> bool:
                    return x == expected_path

                with patch("os.path.exists") as mock_exists:
                    mock_exists.side_effect = mock_exists_side_effect
                    result = self.parser.find_manifest_path()
                    assert result == expected_path

    def test_find_manifest_path_not_found(self):
        with patch("os.path.exists", return_value=False):
            with self.assertRaises(FileNotFoundError) as context:
                self.parser.find_manifest_path()

            assert "Could not find manifest file" in str(context.exception)

    def test_read_manifest_file_success(self):
        test_yaml = """
        title: Test Docs
        sections:
          - name: Section 1
        """
        expected_dict = {"title": "Test Docs", "sections": [{"name": "Section 1"}]}

        with patch("builtins.open", mock_open(read_data=test_yaml)):
            result = self.parser.read_manifest_file("dummy/path")
            assert result == expected_dict

    def test_read_manifest_file_yaml_error(self):
        invalid_yaml = "invalid: yaml: content: [}"

        with patch("builtins.open", mock_open(read_data=invalid_yaml)):
            with self.assertRaises(yaml.YAMLError) as context:
                self.parser.read_manifest_file("dummy/path")

            assert "Failed to parse YAML manifest" in str(context.exception)

    def test_read_manifest_file_io_error(self):
        with patch("builtins.open", mock_open()) as mock_file:
            mock_file.side_effect = IOError("Test IO Error")
            with self.assertRaises(IOError) as _:
                self.parser.read_manifest_file("dummy/path")

    def test_parse_manifest_file_not_found(self):
        with patch.object(
            self.parser,
            "find_manifest_path",
            side_effect=FileNotFoundError("Test error"),
        ):
            with self.assertRaises(FileNotFoundError) as _:
                self.parser.parse_manifest()

    def test_parse_manifest_invalid_yaml(self):
        invalid_yaml = "invalid: yaml: content: [}"

        with patch.object(
            self.parser,
            "find_manifest_path",
            return_value="/test/path/docs.manifest.yaml",
        ):
            with patch("builtins.open", mock_open(read_data=invalid_yaml)):
                with self.assertRaises(yaml.YAMLError) as _:
                    self.parser.parse_manifest()


if __name__ == "__main__":
    unittest.main()
