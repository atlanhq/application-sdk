"""SSL utilities for HTTP clients."""

import os
import ssl
from typing import List, Optional, Union

from application_sdk.constants import SSL_CERT_DIR
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Supported certificate file extensions
CERT_FILE_EXTENSIONS = (".pem", ".crt", ".cer", ".ca-bundle")


def get_ssl_cert_dir() -> Optional[str]:
    """
    Get the SSL certificate directory from configuration.

    Returns:
        Optional[str]: The SSL certificate directory path if SSL_CERT_DIR is set and the directory exists,
            None otherwise.
    """
    if SSL_CERT_DIR and os.path.isdir(SSL_CERT_DIR):
        logger.debug(f"Using SSL certificates from directory: {SSL_CERT_DIR}")
        return SSL_CERT_DIR
    return None


def get_certificate_files(cert_dir: str) -> List[str]:
    """
    Get all certificate files from a directory.

    Args:
        cert_dir: Directory to search for certificate files

    Returns:
        List[str]: List of full paths to certificate files
    """
    cert_files: List[str] = []
    for filename in os.listdir(cert_dir):
        if filename.lower().endswith(CERT_FILE_EXTENSIONS):
            cert_files.append(os.path.join(cert_dir, filename))
    return cert_files


def create_ssl_context_with_custom_certs(cert_dir: str) -> ssl.SSLContext:
    """
    Create an SSL context that includes both default system certificates and custom certificates.

    This ensures that connections to public services (using well-known CAs) continue to work,
    while also allowing connections to private services using custom/internal certificates.

    The function loads all .pem, .crt, .cer, and .ca-bundle files from the specified directory.

    Args:
        cert_dir: Directory containing additional certificate files (.pem, .crt, .cer, .ca-bundle)

    Returns:
        ssl.SSLContext: SSL context with both default and custom certificates loaded
    """
    # Create context with default certificates
    ssl_context = ssl.create_default_context()

    # Find and load all certificate files from the directory
    cert_files = get_certificate_files(cert_dir)

    for cert_file in cert_files:
        try:
            ssl_context.load_verify_locations(cafile=cert_file)
            logger.debug(f"Loaded certificate from: {cert_file}")
        except ssl.SSLError as e:
            logger.warning(f"Failed to load certificate from {cert_file}: {e}")

    if cert_files:
        logger.debug(
            f"Created SSL context with default certificates and {len(cert_files)} custom certificate(s) from: {cert_dir}"
        )
    else:
        logger.debug(
            f"Created SSL context with default certificates (no custom certificates found in {cert_dir})"
        )

    return ssl_context


def get_ssl_context() -> Union[bool, ssl.SSLContext]:
    """
    Get the SSL verification context for HTTP clients (httpx, aiohttp, etc.).

    If SSL_CERT_DIR is set and points to a valid directory, returns an SSLContext
    that includes both default system certificates AND custom certificates from that directory.
    This allows connections to both public services and private services with custom CAs.

    Returns:
        Union[bool, ssl.SSLContext]: An SSLContext with combined certificates if SSL_CERT_DIR is set,
            True otherwise for default SSL verification.

    Example:
        >>> import httpx
        >>> ssl_context = get_ssl_context()
        >>> async with httpx.AsyncClient(verify=ssl_context) as client:
        ...     response = await client.get("https://example.com")

        >>> import aiohttp
        >>> ssl_context = get_ssl_context()
        >>> async with aiohttp.ClientSession() as session:
        ...     async with session.get("https://example.com", ssl=ssl_context) as response:
        ...         pass
    """
    ssl_cert_dir = get_ssl_cert_dir()
    if ssl_cert_dir:
        return create_ssl_context_with_custom_certs(ssl_cert_dir)
    return True

