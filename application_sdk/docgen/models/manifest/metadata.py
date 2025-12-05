from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, HttpUrl


class DocsManifestMetadata(BaseModel):
    """Base metadata for the docs manifest file.

    Attributes:
        version (str): Manifest compatibility version.
        name (str): Name of the manifest.
        description (str): Detailed description of the manifest.
        keywords (List[str]): List of keywords associated with the manifest.
        homepage (Optional[HttpUrl]): URL of the project homepage.
        supportEmail (Optional[EmailStr]): Email address for support.
        license (Optional[str]): License information.
        repository (Optional[HttpUrl]): URL of the project repository.
        author (Optional[str]): Author information.
        metadata (Dict[str, Any]): Additional metadata as key-value pairs.
    """

    version: str
    name: str
    description: str
    author: str
    keywords: List[str] = []
    homepage: Optional[HttpUrl] = None
    supportEmail: Optional[EmailStr] = None
    license: Optional[str] = None
    repository: Optional[HttpUrl] = None
    metadata: Dict[str, Any] = {}
