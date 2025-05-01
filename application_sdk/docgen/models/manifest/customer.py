from typing import List

from pydantic import BaseModel

from application_sdk.docgen.models.manifest.page import DocsManifestPage


class FeatureDetails(BaseModel):
    """Model for feature details in documentation manifests.

    This class represents a feature's details in the documentation manifest,
    including its name, support status, and any additional notes.

    Attributes:
        name (str): The name of the feature.
        supported (bool): Whether the feature is supported. Defaults to False.
        notes (str): Additional notes about the feature. Defaults to empty string.
    """

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
