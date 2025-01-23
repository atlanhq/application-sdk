from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, HttpUrl


class BugReportInfo(BaseModel):
    """Information about bug reporting channels.

    Attributes:
        url: The URL where bugs can be reported.
        email: The email address where bugs can be reported.
    """

    url: Optional[HttpUrl] = None
    email: Optional[EmailStr] = None


class DocsSection(BaseModel):
    """A section within a documentation page.

    Attributes:
        id: Unique identifier for the section.
        name: Display name of the section.
    """

    id: str
    name: str
    fileRef: str


class DocsPage(BaseModel):
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
    sections: List[DocsSection] = []


class DocsManifestMetadata(BaseModel):
    """Base metadata for a manifest file.

    Attributes:
        apiVersion: Version of the API.
        name: Name of the manifest.
        description: Detailed description of the manifest.
        keywords: List of keywords associated with the manifest.
        homepage: URL of the project homepage.
        support: Support information.
        bugs: Bug reporting information.
        license: License information.
        author: Author information.
        metadata: Additional metadata as key-value pairs.
    """

    apiVersion: str
    name: str
    description: str
    keywords: List[str] = []
    homepage: Optional[HttpUrl] = None
    support: Optional[str] = None
    bugs: Optional[BugReportInfo] = None
    license: Optional[str] = None
    author: Optional[str] = None
    metadata: Dict[str, Any] = {}


class CustomerDocsManifest(DocsManifestMetadata):
    """A manifest file containing documentation pages.

    Inherits from DocsMetadata.

    Attributes:
        pages: List of documentation pages.
    """

    pages: List[DocsPage]


class InternalDocsManifest(DocsManifestMetadata):
    """An internal manifest file containing documentation pages.

    Inherits from DocsMetadata.

    Attributes:
        pages: List of documentation pages.
    """

    pages: List[DocsPage]
