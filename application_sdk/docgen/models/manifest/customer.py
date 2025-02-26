from typing import List

from pydantic import BaseModel

from application_sdk.docgen.models.manifest.page import DocsManifestPage


class FeatureDetails(BaseModel):
    name: str
    supported: bool = False
    notes: str = ""


class CustomerDocsManifest(BaseModel):
    """A manifest file containing documentation pages for customer use.

    Attributes:
        pages (List[DocsManifestPage]): List of documentation pages.
    """

    pages: List[DocsManifestPage]
    supported_features: List[FeatureDetails]
