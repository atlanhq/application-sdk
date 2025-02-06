from typing import Optional

from application_sdk.docgen.models.manifest.customer import CustomerDocsManifest
from application_sdk.docgen.models.manifest.internal import InternalDocsManifest
from application_sdk.docgen.models.manifest.metadata import DocsManifestMetadata


class DocsManifest(DocsManifestMetadata):
    customer: CustomerDocsManifest
    internal: Optional[InternalDocsManifest] = None
