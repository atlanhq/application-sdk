"""CERTIFICATE protocol - certificate-based mutual authentication.

Covers mTLS, Client Certificates, SSH Keys (~40+ services).
Pattern: Use certificates for mutual authentication.
"""

import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from application_sdk.credentials.aliases import get_field_value
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import ApplyResult, MaterializeResult
from application_sdk.credentials.types import FieldSpec, FieldType


class CertificateProtocol(BaseProtocol):
    """Protocol for mTLS, Client Certificates.

    Pattern: Use certificates for mutual authentication.
    Certificates can be provided as PEM content or file paths.

    Configuration options (via config or config_override):
        - cert_format: "pem" or "der" (default: "pem")
        - verify_ssl: Whether to verify server certificate (default: True)
        - write_temp_files: Write certs to temp files for SDK compatibility (default: True)

    Examples:
        >>> # mTLS with PEM certificates
        >>> protocol = CertificateProtocol()
        >>> result = protocol.materialize({
        ...     "client_cert": "-----BEGIN CERTIFICATE-----...",
        ...     "client_key": "-----BEGIN PRIVATE KEY-----...",
        ...     "ca_cert": "-----BEGIN CERTIFICATE-----..."
        ... })

    Note:
        For SDK compatibility, certificates may be written to temporary files.
        These are cleaned up when the CredentialHandle is cleaned up.
    """

    default_fields: List[FieldSpec] = [
        FieldSpec(
            name="client_cert",
            display_name="Client Certificate",
            field_type=FieldType.TEXTAREA,
            help_text="PEM-formatted client certificate",
            required=True,
        ),
        FieldSpec(
            name="client_key",
            display_name="Client Private Key",
            field_type=FieldType.TEXTAREA,
            sensitive=True,
            help_text="PEM-formatted private key",
            required=True,
        ),
        FieldSpec(
            name="ca_cert",
            display_name="CA Certificate",
            field_type=FieldType.TEXTAREA,
            help_text="PEM-formatted CA certificate (optional)",
            required=False,
        ),
        FieldSpec(
            name="key_password",
            display_name="Key Password",
            field_type=FieldType.PASSWORD,
            sensitive=True,
            help_text="Password for encrypted private key (optional)",
            required=False,
        ),
    ]

    default_config: Dict[str, Any] = {
        "cert_format": "pem",  # pem, der
        "verify_ssl": True,
        "write_temp_files": True,  # Write to temp files for SDK compatibility
    }

    # Track temp files for cleanup
    _temp_files: List[str] = []

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Provide certificate configuration for HTTP client.

        Note: Certificates are typically handled at the client level,
        not per-request. This returns the cert config for the HTTP client.
        """
        cert_tuple = self._get_cert_tuple(credentials)
        ca_cert = self._get_ca_cert(credentials)

        # For httpx, we return auth config
        auth_config: Dict[str, Any] = {}
        if cert_tuple:
            auth_config["cert"] = cert_tuple
        if ca_cert:
            auth_config["verify"] = ca_cert

        return ApplyResult(auth=auth_config if auth_config else None)

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Return certificate paths/content for SDK usage.

        If write_temp_files is enabled, certificates are written to
        temporary files and paths are returned. Otherwise, raw content
        is returned.
        """
        client_cert = get_field_value(credentials, "client_cert")
        client_key = get_field_value(credentials, "client_key")
        ca_cert = get_field_value(credentials, "ca_cert")
        key_password = get_field_value(credentials, "key_password")

        if self.config.get("write_temp_files", True):
            # Write to temp files for SDK compatibility
            result: Dict[str, Any] = {}

            if client_cert:
                cert_path = self._write_temp_file(client_cert, ".pem")
                result["client_cert_path"] = cert_path

            if client_key:
                key_path = self._write_temp_file(client_key, ".key")
                result["client_key_path"] = key_path

            if ca_cert:
                ca_path = self._write_temp_file(ca_cert, ".pem")
                result["ca_cert_path"] = ca_path

            if key_password:
                result["key_password"] = key_password

            return MaterializeResult(credentials=result)
        else:
            # Return raw content
            return MaterializeResult(
                credentials={
                    "client_cert": client_cert,
                    "client_key": client_key,
                    "ca_cert": ca_cert,
                    "key_password": key_password,
                }
            )

    def _get_cert_tuple(self, credentials: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Get client certificate tuple for httpx.

        Returns tuple of (cert_path, key_path) if both are available.
        """
        client_cert = get_field_value(credentials, "client_cert")
        client_key = get_field_value(credentials, "client_key")

        if not client_cert or not client_key:
            return None

        if self.config.get("write_temp_files", True):
            cert_path = self._write_temp_file(client_cert, ".pem")
            key_path = self._write_temp_file(client_key, ".key")
            return (cert_path, key_path)
        else:
            # Return content directly (may not work with all HTTP clients)
            return (client_cert, client_key)

    def _get_ca_cert(self, credentials: Dict[str, Any]) -> Any:
        """Get CA certificate for verification."""
        ca_cert = get_field_value(credentials, "ca_cert")

        if ca_cert:
            if self.config.get("write_temp_files", True):
                return self._write_temp_file(ca_cert, ".pem")
            return ca_cert

        return self.config.get("verify_ssl", True)

    def _write_temp_file(self, content: str, suffix: str) -> str:
        """Write content to a temporary file.

        Files are tracked for later cleanup.
        """
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            self._temp_files.append(path)
            return path
        except Exception:
            os.close(fd)
            raise

    def cleanup_temp_files(self) -> None:
        """Clean up temporary certificate files.

        Called by CredentialHandle.cleanup().
        """
        for path in self._temp_files:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        self._temp_files.clear()

    def get_temp_files(self) -> List[str]:
        """Get list of temporary files for external cleanup."""
        return list(self._temp_files)
