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


def _get_default_ca_bundle_path() -> Optional[str]:
    """
    Get the path to the default CA certificate bundle.

    Tries multiple sources in order:
    1. certifi package (if available) - provides Mozilla CA bundle
    2. SSL default verify paths from the system

    Returns:
        Optional[str]: Path to the default CA bundle file, or None if not found.
    """
    # Try certifi first (commonly available, provides Mozilla CA bundle)
    try:
        import certifi

        certifi_path = certifi.where()
        if certifi_path and os.path.isfile(certifi_path):
            logger.debug(f"Using certifi CA bundle: {certifi_path}")
            return certifi_path
    except ImportError:
        logger.debug("certifi not available, falling back to system CA paths")

    # Fall back to system SSL default paths
    default_paths = ssl.get_default_verify_paths()

    # Try cafile first (single file with all certs)
    if default_paths.cafile and os.path.isfile(default_paths.cafile):
        logger.debug(f"Using system CA file: {default_paths.cafile}")
        return default_paths.cafile

    # Try openssl_cafile
    if default_paths.openssl_cafile and os.path.isfile(default_paths.openssl_cafile):
        logger.debug(f"Using OpenSSL CA file: {default_paths.openssl_cafile}")
        return default_paths.openssl_cafile

    logger.debug("No default CA bundle found")
    return None


def _read_default_ca_certs() -> Optional[bytes]:
    """
    Read the default system CA certificates as bytes.

    Returns:
        Optional[bytes]: The default CA certificates as bytes, or None if not available.
    """
    ca_bundle_path = _get_default_ca_bundle_path()
    if not ca_bundle_path:
        return None

    try:
        with open(ca_bundle_path, "rb") as f:
            ca_bytes = f.read()
            if ca_bytes:
                logger.debug(
                    f"Read default CA certificates ({len(ca_bytes)} bytes) from: {ca_bundle_path}"
                )
                return ca_bytes
    except OSError as e:
        logger.warning(f"Failed to read default CA bundle from {ca_bundle_path}: {e}")

    return None


def get_custom_ca_cert_bytes() -> Optional[bytes]:
    """
    Get CA certificate bytes combining default system certificates AND custom certificates.

    This is useful for clients like Temporal that use their own TLS implementation
    and require certificate data as bytes rather than an ssl.SSLContext.

    If SSL_CERT_DIR is set and points to a valid directory containing certificate files,
    this function reads all certificate files and concatenates them with the default
    system CA certificates. This ensures that both public services (using well-known CAs)
    and private services (using custom CAs) are trusted.

    Returns:
        Optional[bytes]: Combined certificate bytes (default + custom) if SSL_CERT_DIR is set
            and contains valid certificate files, None otherwise.

    Example:
        >>> from temporalio.service import TLSConfig
        >>> ca_cert_bytes = get_custom_ca_cert_bytes()
        >>> if ca_cert_bytes:
        ...     tls_config = TLSConfig(server_root_ca_cert=ca_cert_bytes)
    """
    ssl_cert_dir = get_ssl_cert_dir()
    if not ssl_cert_dir:
        return None

    cert_files = get_certificate_files(ssl_cert_dir)
    if not cert_files:
        logger.debug(f"No certificate files found in {ssl_cert_dir}")
        return None

    cert_bytes_list: List[bytes] = []

    # First, add the default system CA certificates
    default_ca_bytes = _read_default_ca_certs()
    if default_ca_bytes:
        cert_bytes_list.append(default_ca_bytes)
        logger.debug("Added default system CA certificates to trust store")

    # Then, add custom certificates from SSL_CERT_DIR
    custom_cert_count = 0
    for cert_file in cert_files:
        try:
            with open(cert_file, "rb") as f:
                cert_data = f.read()
                if cert_data:
                    cert_bytes_list.append(cert_data)
                    custom_cert_count += 1
                    logger.debug(f"Read custom certificate from: {cert_file}")
        except OSError as e:
            logger.warning(f"Failed to read certificate from {cert_file}: {e}")

    if not cert_bytes_list:
        return None

    # Concatenate all certificates with newlines between them
    combined_certs = b"\n".join(cert_bytes_list)
    logger.debug(
        f"Combined default CA certificates and {custom_cert_count} custom certificate(s) "
        f"from {ssl_cert_dir} ({len(combined_certs)} bytes total)"
    )
    return combined_certs

