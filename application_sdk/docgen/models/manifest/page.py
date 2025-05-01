from typing import List, Optional

from pydantic import BaseModel

from application_sdk.docgen.models.manifest.section import DocsManifestPageSection


class DocsManifestPage(BaseModel):
    """A documentation page containing multiple sections.

    Attributes:
        id (str): Unique identifier for the page.
        name (str): Display name of the page.
        description (str): Detailed description of the page content.
        fileRef (Optional[str]): Reference to the file containing the page content.
        sections (List[DocsManifestPageSection]): List of sections contained within the page.
    """

    id: str
    name: str
    description: str
    fileRef: Optional[str] = None
    sections: List[DocsManifestPageSection] = []
