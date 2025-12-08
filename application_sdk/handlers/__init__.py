from abc import ABC, abstractmethod
from typing import Any, Dict


class HandlerInterface(ABC):
    """
    Abstract base class for workflow handlers
    """

    @abstractmethod
    async def load(self, *args: Any, **kwargs: Any) -> None:
        """
        Method to load the handler
        """
        pass

    @abstractmethod
    async def test_auth(self, *args: Any, **kwargs: Any) -> bool:
        """
        Abstract method to test the authentication credentials
        To be implemented by the subclass
        """
        raise NotImplementedError("test_auth method not implemented")

    @abstractmethod
    async def preflight_check(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to perform preflight checks
        To be implemented by the subclass
        """
        raise NotImplementedError("preflight_check method not implemented")

    @abstractmethod
    async def fetch_metadata(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to fetch metadata
        To be implemented by the subclass
        """
        raise NotImplementedError("fetch_metadata method not implemented")

    async def upload_file(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Upload file to object store and return metadata matching heracles format.

        This method matches the heracles CreateFile logic for force=false case:
        - Creates file model similar to heracles NewFileModel (force=false path)
        - fileName = UUID + extension
        - rawName = original filename
        - Key includes prefix if provided: prefix + "/" + fileName (matching heracles)
        - Uploads to object store
        - Returns metadata matching heracles File model structure

        Handlers can override this method if they need custom behavior.

        Args:
            **kwargs: Request data from FileUploadRequest.model_dump() containing:
                - file_content: bytes
                - filename: str
                - content_type: str
                - size: int (note: field name is 'size', not 'file_size')
                - prefix: Optional[str] (defaults to 'workflow_file_upload')

        Returns:
            Dict with file metadata matching FileUploadResponse structure
        """
        import mimetypes
        import time
        import uuid
        from datetime import datetime
        from pathlib import Path

        from application_sdk.services.objectstore import ObjectStore

        # Extract request data from kwargs (matching preflight_check pattern)
        # All fields from FileUploadRequest.model_dump() are passed as kwargs
        # Model uses snake_case field names (file_content, content_type, size)
        file_content = kwargs.get("file_content", b"")
        filename = kwargs.get("filename", "unknown")
        content_type = kwargs.get("content_type", "application/octet-stream")
        file_size = kwargs.get("size", 0)  # Model field is 'size', not 'file_size'
        prefix = kwargs.get("prefix", "workflow_file_upload")

        # Generate UUID for file ID (matching heracles)
        file_id = str(uuid.uuid4())

        # Determine extension from filename or content type (matching heracles NewFileModel)
        # Heracles uses filepath.Ext(rawname) first, then mime.ExtensionsByType(contentType)
        extension = Path(filename).suffix
        if not extension:
            extension_list = mimetypes.guess_all_extensions(content_type)
            if extension_list:
                extension = extension_list[0]
            else:
                extension = ""

        # Generate file name and raw name (matching heracles NewFileModel with force=false)
        # When force=false: fileName = id + extension, rawName = original filename
        file_name = f"{file_id}{extension}"
        raw_name = filename  # Keep original filename as rawName

        # Generate key with prefix (matching heracles NewFileModel: lines 109-112)
        # Heracles: key = fileName if prefix is empty, else prefix + "/" + fileName
        key = file_name
        if prefix:
            key = f"{prefix}/{file_name}"

        # Upload directly from bytes to object store (matching heracles PutObject)
        await ObjectStore.upload_file_from_bytes(
            file_content=file_content,
            destination=key,
        )

        # Generate metadata matching heracles File model format
        now_ms = int(time.time() * 1000)
        # Simple version generation - can be overridden by handlers if needed
        version = file_id[:8]  # Use first 8 chars of UUID as simple version

        return {
            "id": file_id,
            "version": version,
            "isActive": True,
            "createdAt": now_ms,
            "updatedAt": now_ms,
            "fileName": file_name,
            "rawName": raw_name,
            "key": key,
            "extension": extension,
            "contentType": content_type,
            "fileSize": file_size,
            "isEncrypted": False,
            "redirectUrl": "",
            "isUploaded": True,
            "uploadedAt": datetime.utcnow().isoformat() + "Z",
            "isArchived": False,
        }

    @staticmethod
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        """
        Static method to get the configmap
        """
        return {}
