from typing import List

from pydantic import BaseModel

from application_sdk.docgen.models.manifest.page import DocsManifestPage


class InternalDocsManifest(BaseModel):
    """An internal manifest file containing documentation pages for internal use.

    Attributes:
        pages (List[DocsManifestPage]): List of documentation pages.
    """

    pages: List[DocsManifestPage]
