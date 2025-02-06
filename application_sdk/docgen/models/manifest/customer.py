from typing import List

from application_sdk.docgen.models.manifest.metadata import DocsManifestMetadata
from application_sdk.docgen.models.manifest.page import DocsManifestPage


class CustomerDocsManifest(DocsManifestMetadata):
    """A manifest file containing documentation pages for customer use.

    Inherits from DocsManifestMetadata.

    Attributes:
        pages (List[DocsManifestPage]): List of documentation pages.
    """

    pages: List[DocsManifestPage]
