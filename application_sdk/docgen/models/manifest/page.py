from typing import List

from pydantic import BaseModel

from application_sdk.docgen.models.manifest.section import DocsManifestPageSection


class DocsManifestPage(BaseModel):
    """A documentation page containing multiple sections.

    Attributes:
        id: Unique identifier for the page.
        name: Display name of the page.
        description: Detailed description of the page content.
        sections: List of sections contained within the page.
    """

    id: str
    name: str
    description: str
    fileRef: str
    sections: List[DocsManifestPageSection] = []
