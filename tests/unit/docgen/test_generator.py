import unittest

import pydantic

from application_sdk.docgen.generator import DocumentationGenerator, docgen
from application_sdk.docgen.models import Page, Section


class TestDocumentationGenerator(unittest.TestCase):
    def setUp(self):
        self.g = DocumentationGenerator()

        s = Section(id="section 1", title="A", markdown_content="", metadata={})

        p = Page(id="a", title="A", sections={}, metadata={})

        self.g.add_page(page=p)
        self.g.add_section(page_id="a", section=s)

        @docgen(page_id="a", section_id="section 1")
        class DummyModel(pydantic.BaseModel):
            name: str

    def test_generation(self):
        assert len(self.g.pages) == 1
