from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, HttpUrl


class DocsManifestMetadata(BaseModel):
    """Base metadata for a manifest file.

    Attributes:
        version: Manifest compatibility version.
        name: Name of the manifest.
        description: Detailed description of the manifest.
        keywords: List of keywords associated with the manifest.
        homepage: URL of the project homepage.
        supportEmail: Email address for support.
        bugs: Bug reporting information.
        license: License information.
        author: Author information.
        metadata: Additional metadata as key-value pairs.
    """

    version: str
    name: str
    description: str
    keywords: List[str] = []
    homepage: Optional[HttpUrl] = None
    supportEmail: Optional[EmailStr] = None
    license: Optional[str] = None
    author: Optional[str] = None
    metadata: Dict[str, Any] = {}
