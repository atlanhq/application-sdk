from pydantic import BaseModel


class DocsManifestPageSection(BaseModel):
    """A section within a documentation page.

    Attributes:
        id: Unique identifier for the section.
        name: Display name of the section.
    """

    id: str
    name: str
    fileRef: str
