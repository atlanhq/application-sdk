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

from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest
import trustme
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from dateutil.tz import tzutc
from obstore.auth.azure import AzureCredentialProvider
from obstore.auth.boto3 import StsCredentialProvider

from application_sdk.storage._credential_providers import (
    _RegionFallbackStsCredentialProvider,
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


class BotocoreTzStsClient(FakeStsClient):
    """FakeStsClient whose ``Expiration`` carries botocore's real tzinfo.

    botocore deserialises STS timestamps with ``dateutil``, so a live
    AssumeRole response has ``tzinfo=dateutil.tz.tzutc()`` — equal to, but not
    identical with, ``datetime.timezone.utc``. ``FakeStsClient`` above uses the
    stdlib singleton, which is why it never reproduced the obstore rejection.
    """

    def assume_role(self, **kwargs: object) -> dict[str, object]:
        response = super().assume_role(**kwargs)
        credentials = response["Credentials"]
        assert isinstance(credentials, dict)
        credentials["Expiration"] = datetime(2026, 1, 1, tzinfo=tzutc())
        return response


class BotocoreTzSession(FakeSession):
    """FakeSession that hands out a :class:`BotocoreTzStsClient`."""

    def __init__(self) -> None:
        super().__init__()
        self.sts = BotocoreTzStsClient()


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

    @pytest.mark.parametrize(
        ("external_id", "expected"),
        [("  ext-123  ", "ext-123"), ("", None), ("   ", None)],
    )
    def test_external_id_normalised(
        self, external_id: str, expected: str | None
    ) -> None:
        """Padded IDs are trimmed; blank ones are omitted (STS rejects empty)."""
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            external_id=external_id,
        )
        assert provider.kwargs.get("ExternalId") == expected

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

    def test_expiry_is_normalised_to_stdlib_utc(self) -> None:
        """The expiry handed to obstore must carry ``datetime.timezone.utc``.

        obstore's Rust binding requires that exact tzinfo and raises
        ``ValueError: expected datetime.timezone.utc`` otherwise, surfaced as
        ``UnauthenticatedError ... path: "External AWS credential provider"``
        on the first object-store call. botocore supplies
        ``dateutil.tz.tzutc()``, which obstore's Python-side check lets through
        because it only tests ``tzinfo is not None`` — so the failure only
        appears at the Rust boundary, deterministically, exhausting the retry
        budget without progress.

        Asserted on the ``tzinfo`` with ``is``, because that is the condition
        obstore actually enforces. Note what an assertion on the *datetime*
        would not catch: ``datetime(..., tzinfo=tzutc())`` and
        ``datetime(..., tzinfo=timezone.utc)`` compare equal — same instant —
        so the ``==`` check below pins that the value was not shifted, and
        cannot on its own tell a normalised expiry from an unnormalised one.
        (Between the tzinfo objects themselves ``==`` is False, so a tzinfo
        equality check would also fail the unnormalised case; identity is used
        because it states obstore's requirement exactly.)
        """
        provider = make_s3_assume_role_provider(role_arn=ROLE_ARN)
        provider.session = BotocoreTzSession()

        credential = provider()

        expires_at = credential["expires_at"]
        assert expires_at.tzinfo is timezone.utc
        # The instant must be preserved, not shifted.
        assert expires_at == datetime(2026, 1, 1, tzinfo=UTC)

    def test_stdlib_utc_expiry_passes_through_unchanged(self) -> None:
        """Normalisation is idempotent for an already-stdlib-UTC expiry."""
        provider = make_s3_assume_role_provider(role_arn=ROLE_ARN)
        provider.session = FakeSession()

        expires_at = provider()["expires_at"]

        assert expires_at.tzinfo is timezone.utc
        assert expires_at == datetime(2026, 1, 1, tzinfo=UTC)

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


class RegionalFakeSession(FakeSession):
    """FakeSession for one STS endpoint that can reject the call with *error_code*."""

    def __init__(self, region_name: str | None, error_code: str | None = None) -> None:
        super().__init__()
        self.region_name = region_name
        self.error_code = error_code

    def client(self, service_name: str) -> FakeStsClient:
        if self.error_code:
            self.client_requests.append(service_name)
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "rejected"}},
                "AssumeRole",
            )
        return super().client(service_name)


@pytest.fixture
def no_ambient_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No AWS_REGION / AWS_DEFAULT_REGION / profile region: the pod case."""
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


class TestS3AssumeRoleStsEndpointOrder:
    """Which STS endpoint the provider calls, and when it moves to the next."""

    def test_no_fallback_region_keeps_single_endpoint_provider(
        self, no_ambient_region: None
    ) -> None:
        provider = make_s3_assume_role_provider(role_arn=ROLE_ARN)
        assert not isinstance(provider, _RegionFallbackStsCredentialProvider)
        assert provider.session.region_name is None  # global endpoint, as before

    def test_no_ambient_region_tries_bucket_region_before_global(
        self, no_ambient_region: None
    ) -> None:
        # Pod with no AWS_REGION, bucket in an opt-in region.
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN, fallback_region="eu-south-2"
        )
        assert isinstance(provider, _RegionFallbackStsCredentialProvider)
        assert [s.region_name for s in provider._sessions] == ["eu-south-2", None]
        assert provider.session.client("sts").meta.endpoint_url == (
            "https://sts.eu-south-2.amazonaws.com"
        )
        # Store region stays the bucket's region.
        assert provider.config == {"region": "eu-south-2"}

    def test_ambient_region_goes_first(
        self, no_ambient_region: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN, fallback_region="me-central-1"
        )
        assert [s.region_name for s in provider._sessions] == [
            "eu-west-1",
            "me-central-1",
        ]

    def test_fallback_equal_to_ambient_region_is_single_endpoint(
        self, no_ambient_region: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-south-2")
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN, fallback_region="eu-south-2"
        )
        assert not isinstance(provider, _RegionFallbackStsCredentialProvider)
        assert provider.session.region_name == "eu-south-2"

    def test_base_credentials_reach_every_endpoint_session(
        self, no_ambient_region: None
    ) -> None:
        provider = make_s3_assume_role_provider(
            role_arn=ROLE_ARN,
            base_access_key="AK",
            base_secret_key="SK",
            external_id="ext-1",
            fallback_region="eu-south-2",
        )
        for session in provider._sessions:
            frozen = session.get_credentials().get_frozen_credentials()
            assert (frozen.access_key, frozen.secret_key) == ("AK", "SK")
        assert provider.kwargs["ExternalId"] == "ext-1"

    def test_bucket_region_success_uses_regional_endpoint_only(self) -> None:
        # Pod and bucket in the same opt-in region: the regional call works
        # and the global endpoint is never called.
        regional = RegionalFakeSession("eu-south-2")
        global_ = RegionalFakeSession(None)
        provider = _RegionFallbackStsCredentialProvider(
            [regional, global_], RoleArn=ROLE_ARN, RoleSessionName="s"
        )

        credential = provider()

        assert credential["token"] == "temp-token"
        assert len(regional.sts.assume_role_calls) == 1
        assert global_.client_requests == []

    @pytest.mark.parametrize(
        "code", ["InvalidClientTokenId", "RegionDisabledException"]
    )
    def test_region_rejection_falls_back_and_is_remembered(self, code: str) -> None:
        # Bucket in an opt-in region the caller's account has not enabled:
        # STS there rejects the call, the global endpoint serves it, and the
        # next refresh goes straight to the global endpoint.
        regional = RegionalFakeSession("me-central-1", error_code=code)
        global_ = RegionalFakeSession(None)
        provider = _RegionFallbackStsCredentialProvider(
            [regional, global_], RoleArn=ROLE_ARN, RoleSessionName="s"
        )

        assert provider()["access_key_id"] == "ASIA-TEMP"
        assert provider()["access_key_id"] == "ASIA-TEMP"

        assert regional.client_requests == ["sts"]  # tried once, then skipped
        assert len(global_.sts.assume_role_calls) == 2

    def test_other_errors_are_raised_without_fallback(self) -> None:
        # AccessDenied (trust policy, ExternalId) is the same on every
        # endpoint, so it is raised at once.
        regional = RegionalFakeSession("eu-south-2", error_code="AccessDenied")
        global_ = RegionalFakeSession(None)
        provider = _RegionFallbackStsCredentialProvider(
            [regional, global_], RoleArn=ROLE_ARN, RoleSessionName="s"
        )

        with pytest.raises(ClientError, match="AccessDenied"):
            provider()
        assert global_.client_requests == []

    def test_last_endpoint_error_is_raised(self) -> None:
        regional = RegionalFakeSession("eu-south-2", error_code="InvalidClientTokenId")
        global_ = RegionalFakeSession(None, error_code="InvalidClientTokenId")
        provider = _RegionFallbackStsCredentialProvider(
            [regional, global_], RoleArn=ROLE_ARN, RoleSessionName="s"
        )

        with pytest.raises(ClientError, match="InvalidClientTokenId"):
            provider()

    def test_fallback_credential_expiry_is_stdlib_utc(self) -> None:
        regional = RegionalFakeSession("eu-south-2")
        regional.sts = BotocoreTzStsClient()
        provider = _RegionFallbackStsCredentialProvider(
            [regional], RoleArn=ROLE_ARN, RoleSessionName="s"
        )
        assert provider()["expires_at"].tzinfo is timezone.utc


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
