"""Tests for cloud detection."""

import os
import unittest

from application_sdk.lakehouse._polaris import cloud as cloud_mod


class TestDetectCloud(unittest.TestCase):
    def setUp(self):
        os.environ.pop("CLOUD", None)
        os.environ.pop("AWS_REGION", None)

    def tearDown(self):
        os.environ.pop("CLOUD", None)
        os.environ.pop("AWS_REGION", None)

    def test_default_is_aws_when_unset(self):
        self.assertEqual(cloud_mod.detect_cloud(), "aws")

    def test_default_is_aws_when_empty(self):
        os.environ["CLOUD"] = ""
        self.assertEqual(cloud_mod.detect_cloud(), "aws")

    def test_default_is_aws_when_unknown(self):
        os.environ["CLOUD"] = "alibaba"
        self.assertEqual(cloud_mod.detect_cloud(), "aws")

    def test_aws(self):
        os.environ["CLOUD"] = "aws"
        self.assertEqual(cloud_mod.detect_cloud(), "aws")

    def test_gcp(self):
        os.environ["CLOUD"] = "gcp"
        self.assertEqual(cloud_mod.detect_cloud(), "gcp")

    def test_azure(self):
        os.environ["CLOUD"] = "azure"
        self.assertEqual(cloud_mod.detect_cloud(), "azure")

    def test_case_insensitive(self):
        os.environ["CLOUD"] = "AZURE"
        self.assertEqual(cloud_mod.detect_cloud(), "azure")

    def test_strips_whitespace(self):
        os.environ["CLOUD"] = "  gcp  "
        self.assertEqual(cloud_mod.detect_cloud(), "gcp")


class TestAwsRegion(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AWS_REGION", None)

    def tearDown(self):
        os.environ.pop("AWS_REGION", None)

    def test_default_empty(self):
        self.assertEqual(cloud_mod.aws_region(), "")

    def test_returns_set(self):
        os.environ["AWS_REGION"] = "ap-south-1"
        self.assertEqual(cloud_mod.aws_region(), "ap-south-1")


if __name__ == "__main__":
    unittest.main()
