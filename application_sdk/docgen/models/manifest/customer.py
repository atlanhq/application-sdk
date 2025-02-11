from typing import List

from pydantic import BaseModel

from application_sdk.docgen.models.manifest.page import DocsManifestPage


class CustomerDocsManifest(BaseModel):
    """A manifest file containing documentation pages for customer use.

    Attributes:
        pages (List[DocsManifestPage]): List of documentation pages.
    """

    pages: List[DocsManifestPage]
