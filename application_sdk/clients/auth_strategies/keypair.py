"""Keypair (certificate/private-key) auth strategy."""

from __future__ import annotations

from typing import Any

from application_sdk.credentials.types import CertificateCredential, Credential


class KeypairAuthStrategy:
    """Handles CertificateCredential for SQL clients.

    Decrypts the PEM private key and passes the DER-encoded bytes
    via ``connect_args`` (e.g. Snowflake keypair auth).
    """

    credential_type = CertificateCredential

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        assert isinstance(credential, CertificateCredential)
        from cryptography.hazmat.primitives import serialization

        key_bytes = credential.key_data.encode()
        passphrase = credential.passphrase.encode() if credential.passphrase else None
        private_key = serialization.load_pem_private_key(key_bytes, passphrase)
        der_bytes = private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return {"private_key": der_bytes}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        return {}
