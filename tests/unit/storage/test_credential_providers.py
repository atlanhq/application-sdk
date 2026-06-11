"""Unit tests for _credential_providers.

Per ADR-0005 ("mock the Protocols, not the SDKs") these tests do NOT patch
boto3 / azure-identity / obstore constructors.  They exercise the factories
through their real SDK boundary objects and assert on documented surfaces:

* S3: the returned ``StsCredentialProvider`` exposes ``session`` (a real
  ``boto3.Session`` — inspected via the public ``get_credentials()`` /
  ``get_frozen_credentials()`` API), ``kwargs`` (the AssumeRole request), and
  ``config`` (region passed to the store).  The STS call itself is exercised
  by swapping in a fake session that implements the documented seam
  (``.client("sts")`` → client with ``.assume_role(**kwargs)``).
* Azure: real certificates (generated with ``trustme`` / ``cryptography``)
  are fed through the factory so that azure-identity's own construction-time
  validation proves each kwarg was forwarded — bad certificate data, missing
  passwords, conflicting path+data, and non-HTTPS authorities all fail loudly
  at the factory call site.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import trustme
from cryptography.hazmat.primitives import serialization
from obstore.auth.azure import AzureCredentialProvider
from obstore.auth.boto3 import StsCredentialProvider

from application_sdk.storage._credential_providers import (
    make_azure_certificate_provider,
    make_s3_assume_role_provider,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/MyRole"

# ---------------------------------------------------------------------------
# Fake boto3-session seam (documented surface: .client("sts") → .assume_role)
# ---------------------------------------------------------------------------


class FakeStsClient:
    """Fake STS client implementing the documented ``assume_role`` response."""

    def __init__(self) -> None:
        self.assume_role_calls: list[dict[str, object]] = []

    def assume_role(self, **kwargs: object) -> dict[str, object]:
        self.assume_role_calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIA-TEMP",
                "SecretAccessKey": "temp-secret",
                "SessionToken": "temp-token",
                "Expiration": datetime(2026, 1, 1, tzinfo=UTC),
            }
        }


class FakeSession:
    """Fake boto3-session boundary object: only the seam the provider uses."""

    def __init__(self) -> None:
        self.sts = FakeStsClient()
        self.client_requests: list[str] = []

    def client(self, service_name: str) -> FakeStsClient:
        self.client_requests.append(service_name)
        return self.sts


# ---------------------------------------------------------------------------
# Certificate helpers (real PEM material, no network)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_cert() -> trustme.LeafCert:
    """Real RSA certificate — azure-identity requires RSA keys for RS256."""
    ca = trustme.CA(key_type=trustme.KeyType.RSA)
    return ca.issue_cert("app.example.test", key_type=trustme.KeyType.RSA)


@pytest.fixture(scope="module")
def cert_pem(rsa_cert: trustme.LeafCert) -> bytes:
    """PEM bundle with the private key and certificate chain."""
    return rsa_cert.private_key_and_cert_chain_pem.bytes()


@pytest.fixture(scope="module")
def encrypted_cert_pem(rsa_cert: trustme.LeafCert) -> bytes:
    """PEM bundle whose private key is encrypted with passphrase ``pass123``."""
    key = serialization.load_pem_private_key(
        rsa_cert.private_key_pem.bytes(), password=None
    )
    encrypted_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"pass123"),
    )
    return encrypted_key + rsa_cert.cert_chain_pems[0].bytes()


class TestMakeS3AssumeRoleProvider:
    def test_returns_sts_provider_with_role_and_region_config(self) -> None:
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            session_name="my-session",
            region="us-east-1",
        )

        assert isinstance(provider, StsCredentialProvider)
        # AssumeRole request the provider will issue on refresh.
        assert provider.kwargs == {
            "RoleArn": ROLE_ARN,
            "RoleSessionName": "my-session",
        }
        # Region propagates to the store config and the underlying session.
        assert provider.config == {"region": "us-east-1"}
        assert provider.session.region_name == "us-east-1"

    def test_base_credentials_become_the_sts_session_credentials(self) -> None:
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            base_access_key="AK",
            base_secret_key="SK",
            base_session_token="TOK",
            region="eu-west-1",
        )

        frozen = provider.session.get_credentials().get_frozen_credentials()
        assert frozen.access_key == "AK"
        assert frozen.secret_key == "SK"
        assert frozen.token == "TOK"
        assert provider.session.region_name == "eu-west-1"

    def test_base_session_token_ignored_without_key_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token without its key pair is meaningless — default chain wins."""
        # Pin the default credential chain to known env values so the test is
        # hermetic on machines with real AWS config.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENV-AK")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENV-SK")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)

        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            base_session_token="TOK",  # no base_access_key / base_secret_key
        )

        frozen = provider.session.get_credentials().get_frozen_credentials()
        assert frozen.access_key == "ENV-AK"
        assert frozen.secret_key == "ENV-SK"
        # The orphan token was NOT injected into the session.
        assert frozen.token is None

    def test_external_id_included_in_assume_role_request(self) -> None:
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            external_id="ext-123",
        )
        assert provider.kwargs["ExternalId"] == "ext-123"

    def test_external_id_omitted_by_default(self) -> None:
        provider = make_s3_assume_role_provider(role_arn=ROLE_ARN)
        assert "ExternalId" not in provider.kwargs

    def test_no_region_leaves_store_config_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Clear ambient region config (env vars AND ~/.aws/config) so the
        # assertion is hermetic on developer machines with real AWS profiles.
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
        monkeypatch.setenv(
            "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials")
        )

        provider = make_s3_assume_role_provider(role_arn=ROLE_ARN)
        assert provider.config == {}

    def test_provider_call_exchanges_sts_response_for_s3_credential(self) -> None:
        """End-to-end refresh through the fake session seam.

        Verifies the full provider behavior the factory wires up: an STS
        AssumeRole call carrying our configured request, translated into the
        obstore ``S3Credential`` mapping.
        """
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            session_name="seam-session",
            external_id="ext-9",
        )
        fake_session = FakeSession()
        provider.session = fake_session

        credential = provider()

        assert fake_session.client_requests == ["sts"]
        assert fake_session.sts.assume_role_calls == [
            {
                "RoleArn": ROLE_ARN,
                "RoleSessionName": "seam-session",
                "ExternalId": "ext-9",
            }
        ]
        assert credential == {
            "access_key_id": "ASIA-TEMP",
            "secret_access_key": "temp-secret",
            "token": "temp-token",
            "expires_at": datetime(2026, 1, 1, tzinfo=UTC),
        }


class TestMakeAzureCertificateProvider:
    def test_certificate_data_builds_provider(self, cert_pem: bytes) -> None:
        provider = make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_data=cert_pem,
        )
        assert isinstance(provider, AzureCredentialProvider)

    def test_certificate_path_builds_provider(
        self, cert_pem: bytes, tmp_path: Path
    ) -> None:
        cert_file = tmp_path / "app.pem"
        cert_file.write_bytes(cert_pem)

        provider = make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_path=str(cert_file),
        )
        assert isinstance(provider, AzureCredentialProvider)

    def test_invalid_certificate_data_fails_at_construction(self) -> None:
        """Bad PEM fails at the factory call, not on the first storage request."""
        with pytest.raises(ValueError, match="certificate"):
            make_azure_certificate_provider(
                tenant_id="tid",
                client_id="cid",
                certificate_data=b"not a certificate",
            )

    def test_path_and_data_together_are_rejected(
        self, cert_pem: bytes, tmp_path: Path
    ) -> None:
        """The factory docstring says exactly one of path/data — pinned here."""
        cert_file = tmp_path / "app.pem"
        cert_file.write_bytes(cert_pem)

        with pytest.raises(ValueError, match="not both"):
            make_azure_certificate_provider(
                tenant_id="tid",
                client_id="cid",
                certificate_path=str(cert_file),
                certificate_data=cert_pem,
            )

    def test_certificate_password_unlocks_encrypted_key(
        self, encrypted_cert_pem: bytes
    ) -> None:
        """certificate_password is forwarded — proven by decrypting a real key."""
        provider = make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_data=encrypted_cert_pem,
            certificate_password="pass123",
        )
        assert isinstance(provider, AzureCredentialProvider)

    def test_encrypted_key_without_password_fails(
        self, encrypted_cert_pem: bytes
    ) -> None:
        # cryptography raises TypeError ("Password was not given but private
        # key is encrypted"); older versions raised ValueError.
        with pytest.raises((TypeError, ValueError), match="[Pp]assword"):
            make_azure_certificate_provider(
                tenant_id="tid",
                client_id="cid",
                certificate_data=encrypted_cert_pem,
            )

    def test_authority_host_forwarded_to_credential(self, cert_pem: bytes) -> None:
        """authority_host reaches azure-identity — proven by its own validation.

        azure-identity rejects non-HTTPS authorities at construction time, so
        an http:// authority raising here is positive proof the kwarg is wired
        through (a dropped kwarg would construct successfully).
        """
        with pytest.raises(ValueError, match="https"):
            make_azure_certificate_provider(
                tenant_id="tid",
                client_id="cid",
                certificate_data=cert_pem,
                authority_host="http://insecure.example",
            )

    def test_valid_sovereign_authority_accepted(self, cert_pem: bytes) -> None:
        provider = make_azure_certificate_provider(
            tenant_id="tid",
            client_id="cid",
            certificate_data=cert_pem,
            authority_host="https://login.chinacloudapi.cn",
        )
        assert isinstance(provider, AzureCredentialProvider)
