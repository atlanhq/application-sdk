"""SDR agent credential resolution against a real, *scoped* Dapr secret store.

A production SDR secret-store identity can usually read only its own secrets.
Asked for any other name, the store refuses: AWS / GCP access denied, Azure
Key Vault or Kubernetes rejecting the name. Dapr secret scoping
(``defaultAccess: deny``) reproduces that refusal locally, as a 403
``ERR_PERMISSION_DENIED``.

The default ``local.file`` test store instead answers "not found" for any
unknown name. Against it, a resolver that sends plain values to the store
looks fine, which is how the SDK 3.50 single-key regression (FND-4407) reached
the fleet unseen. Every test here runs against the refusing store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from application_sdk.credentials.agent import resolve_agent_credential
from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors.categories import Audience
from application_sdk.infrastructure._dapr.client import DaprSecretStore
from application_sdk.infrastructure._dapr.http import AsyncDaprClient
from application_sdk.infrastructure.secrets import (
    AgentSecretReadError,
    SecretStoreError,
)

pytestmark = [pytest.mark.integration]

#: The only secret the scoped identity may read.
ALLOWED_SECRET = "SDR_DB_PASSWORD"
ALLOWED_SECRET_VALUE = "integration-db-password"
#: Allowed by scope, but absent from the store: a genuine "not found".
ALLOWED_BUT_ABSENT = "SDR_DB_MISSING"
#: A plain value, as SDR configs carry for usernames, databases, ARNs.
PLAIN_USERNAME = "svc_reader"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def scoped_dapr(tmp_path_factory):
    """A real daprd whose secret store refuses every name but one."""
    from application_sdk.dev import embedded_dapr

    root = tmp_path_factory.mktemp("scoped-dapr")
    secrets = root / "secrets.json"
    secrets.write_text(json.dumps({ALLOWED_SECRET: ALLOWED_SECRET_VALUE}))
    components = root / "components"
    components.mkdir()
    (components / "secretstore.yaml").write_text(
        "apiVersion: dapr.io/v1alpha1\n"
        "kind: Component\n"
        "metadata:\n"
        "  name: secretstore\n"
        "spec:\n"
        "  type: secretstores.local.file\n"
        "  version: v1\n"
        "  metadata:\n"
        "    - name: secretsFile\n"
        f"      value: {secrets}\n"
    )
    config = root / "config.yaml"
    config.write_text(
        "apiVersion: dapr.io/v1alpha1\n"
        "kind: Configuration\n"
        "metadata:\n"
        "  name: scoped-secrets\n"
        "spec:\n"
        "  secrets:\n"
        "    scopes:\n"
        "      - storeName: secretstore\n"
        "        defaultAccess: deny\n"
        f"        allowedSecrets: [{ALLOWED_SECRET}, {ALLOWED_BUT_ABSENT}]\n"
    )
    async with embedded_dapr(
        components_dir=str(components), config_file=str(config)
    ) as dapr:
        yield dapr


@pytest.fixture
async def scoped_secret_store(scoped_dapr):
    """The real SDK store over the sidecar, on the test's own event loop."""
    client = AsyncDaprClient(base_url=f"http://127.0.0.1:{scoped_dapr.http_port}")
    yield DaprSecretStore(client, store_name="secretstore")
    await client.close()


def _ref(name: str) -> dict[str, object]:
    return {"valueFrom": {"secretKeyRef": {"name": name, "key": name}}}


def _declared(**fields: object) -> AgentCredentialSpec:
    return AgentCredentialSpec.model_validate(
        {
            "format": 2,
            "agent-name": "sdr-agent",
            "secret-manager": "custom",
            "auth-type": "basic",
            "host": {"value": "db.example.com"},
            "port": {"value": 3306},
            **fields,
        }
    )


class TestStoreRefusesUnknownNames:
    """Pins the harness: the store must refuse, not answer "not found"."""

    async def test_a_plain_value_is_refused_not_absent(
        self, scoped_secret_store: DaprSecretStore
    ) -> None:
        with pytest.raises(SecretStoreError) as exc_info:
            await scoped_secret_store.get(PLAIN_USERNAME)
        assert not isinstance(exc_info.value, CredentialNotFoundError)


class TestDeclaredSources:
    async def test_plain_values_never_reach_the_store(
        self, scoped_secret_store: DaprSecretStore
    ) -> None:
        """The census payload in format 2: a plain username the store would
        refuse, and one real secret."""
        spec = _declared(
            **{
                "basic.username": {"value": PLAIN_USERNAME},
                "basic.password": _ref(ALLOWED_SECRET),
                "extra.database": {"value": "sales_db.prod"},
                "role.extra.aws_role_arn": {
                    "value": "arn:aws:iam::123456789012:role/atlan"
                },
            }
        )
        resolved = await resolve_agent_credential(spec, scoped_secret_store)
        assert resolved["username"] == PLAIN_USERNAME
        assert resolved["password"] == ALLOWED_SECRET_VALUE
        assert resolved["port"] == 3306

    async def test_a_refused_declared_ref_fails_the_run(
        self, scoped_secret_store: DaprSecretStore
    ) -> None:
        """No fallback: the field is known to be a secret, so its name must
        never be sent to the source as the value."""
        spec = _declared(**{"basic.password": _ref("SDR_NOT_ALLOWED")})
        with pytest.raises(AgentSecretReadError) as exc_info:
            await resolve_agent_credential(spec, scoped_secret_store)
        err = exc_info.value
        # The customer's scoping refused it: routed to the user, not retried
        # (Dapr's 403 is a definitive rejection), and never names the secret.
        assert err.audience is Audience.USER
        assert err.effective_retryable is False
        assert "SDR_NOT_ALLOWED" not in str(err)

    async def test_an_absent_declared_ref_is_not_found(
        self, scoped_secret_store: DaprSecretStore
    ) -> None:
        spec = _declared(**{"basic.password": _ref(ALLOWED_BUT_ABSENT)})
        with pytest.raises(CredentialNotFoundError):
            await resolve_agent_credential(spec, scoped_secret_store)


class TestLegacySingleKeyBridge:
    """The FND-4407 regression, against a real refusing store.

    The census payload is a legacy single-key config with a plain username
    and one real secret ref. SDK 3.50-3.52 failed it with ``Secret store failed
    while looking up single-key ref-key ...``. The deprecated bridge keeps the
    plain value while still resolving the real ref.
    """

    async def test_census_payload_resolves(
        self, scoped_secret_store: DaprSecretStore
    ) -> None:
        spec = AgentCredentialSpec.model_validate(
            {
                "agent-name": "sdr-agent",
                "secret-manager": "custom",
                "key-type": "single-key",
                "auth-type": "basic",
                "host": "db.example.com",
                "port": 3306,
                "basic.username": PLAIN_USERNAME,
                "basic.password": ALLOWED_SECRET,
                "extra.databaseTerm": "SCHEMA",
                "role.extra.aws_role_arn": "arn:aws:iam::123456789012:role/atlan",
            }
        )
        resolved = await resolve_agent_credential(spec, scoped_secret_store)
        assert resolved["username"] == PLAIN_USERNAME
        assert resolved["password"] == ALLOWED_SECRET_VALUE
        assert resolved["extra"]["databaseTerm"] == "SCHEMA"


def test_fixture_files_are_self_contained() -> None:
    """The harness writes its own store and config; nothing leaks into the
    shared ``tests/integration/secrets.json``."""
    shared = json.loads(
        (Path(__file__).parent / "secrets.json").read_text(encoding="utf-8")
    )
    assert ALLOWED_SECRET not in shared
