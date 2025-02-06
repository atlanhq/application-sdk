from typing import List

from application_sdk.docgen.models.manifest.metadata import DocsManifestMetadata
from application_sdk.docgen.models.manifest.page import DocsManifestPage


class InternalDocsManifest(DocsManifestMetadata):
    """An internal manifest file containing documentation pages.

    Inherits from DocsMetadata.

    Attributes:
        pages: List of documentation pages.
    """

    pages: List[DocsManifestPage]
