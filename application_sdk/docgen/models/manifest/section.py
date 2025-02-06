from typing import Optional

from pydantic import BaseModel


class DocsManifestPageSection(BaseModel):
    """A section within a documentation page.

    Attributes:
        id (str): Unique identifier for the section.
        name (str): Display name of the section.
        fileRef (Optional[str]): Reference to the file containing the section content.
    """

    id: str
    name: str
    fileRef: Optional[str] = None
