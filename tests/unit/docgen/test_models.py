import unittest

from application_sdk.docgen.models import Page, Section


class TestDocGenModels(unittest.TestCase):
    def test_section_field_count(self):
        assert len(Section.model_fields) >= 4

    def test_page_field_count(self):
        assert len(Page.model_fields) >= 4
