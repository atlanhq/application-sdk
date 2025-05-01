from typing import Optional

from application_sdk.docgen.models.manifest.customer import CustomerDocsManifest
from application_sdk.docgen.models.manifest.internal import InternalDocsManifest
from application_sdk.docgen.models.manifest.metadata import DocsManifestMetadata


class DocsManifest(DocsManifestMetadata):
    """Model for the docs manifest file that contains details for both customer and internal docs.

    Inherits from DocsManifestMetadata.

    Attributes:
        customer (CustomerDocsManifest): Customer documentation manifest.
        internal (Optional[InternalDocsManifest]): Optional internal documentation manifest.
    """

    customer: CustomerDocsManifest
    internal: Optional[InternalDocsManifest] = None
