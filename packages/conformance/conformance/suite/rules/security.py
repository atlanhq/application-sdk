"""Security / secret-hygiene rule definitions (S-series).

A deterministic baseline for handling sensitive data consistently (BLDX-1419):
apps must never hardcode credentials and must resolve secrets through the SDK's
supported mechanism (``application_sdk.credentials`` /
``application_sdk.infrastructure.secrets``) rather than reading them straight
from the process environment.

The issue's third clause — *no credentials in logs* — is already enforced by
``L010 CredentialInLogOutput`` (BLOCK, scope=both, category=security); per the
rule-id non-duplication policy the S-series does **not** restate it.

Rule-id stability (non-migration policy)
----------------------------------------
S-ids are a permanent public contract: each is exposed in SARIF ``help_uri`` and
referenced by inline ``# conformance: ignore[Sxxx]`` suppressions across the
fleet.  An S-id therefore **never migrates and never changes**.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="S001",
        scope=RuleScope.BOTH,
        name="HardcodedCredential",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="credential-storage",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "A credential baked into source is committed to version control in plaintext, "
            "shared with everyone who can read the repo, survives rotation, and cannot be "
            "scoped or revoked per-deployment. Secrets must be resolved at runtime through "
            "the SDK secret store, never embedded in the code that ships them."
        ),
        short_description=(
            "String literal assigned to a credential-named variable/argument — a hardcoded secret"
        ),
        full_description=(
            "A non-empty string literal is assigned to (or passed as) a target whose\n"
            "name marks it a credential value (``password``, ``api_key``, ``secret``,\n"
            "``access_key``, ``client_secret``, ``token``, …).  Hardcoded credentials\n"
            "are committed in plaintext and cannot be rotated or scoped per deployment.\n"
            "\n"
            "Resolve the secret at runtime instead — via\n"
            "``context.resolve_credential(ref)`` / a ``CredentialRef``\n"
            "(``application_sdk.credentials``) or the ``SecretStore`` protocol\n"
            "(``application_sdk.infrastructure.secrets``).\n"
            "\n"
            "The check is deliberately conservative: empty strings, ``Field(default=…)``\n"
            'declarations, format/URL templates (``"...{password}..."``), values that are\n'
            "themselves SCREAMING_SNAKE env-var-name references, and ``Enum`` members are\n"
            "not flagged.  A reviewed exception is suppressed inline with a justification:\n"
            "``# conformance: ignore[S001] <reason>`` (BLDX-1419).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
            "conformance/docs/rules/security.md#s001"
        ),
    ),
    RuleDefinition(
        id="S002",
        scope=RuleScope.APP,
        name="RawEnvCredentialAccess",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="credential-resolution",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "Reading a credential straight from os.environ bypasses the SDK's secret-store "
            "seam: there is no central audit of which secrets an app consumes, no typed "
            "CredentialRef contract, and no Dapr-backed resolution. Apps must resolve secrets "
            "through the SDK mechanism so credential handling stays uniform and auditable."
        ),
        short_description=(
            "Credential-named environment variable read directly via os.getenv/os.environ"
        ),
        full_description=(
            "Application code reads a credential-named environment variable directly\n"
            '(``os.getenv("...SECRET")``, ``os.environ["...TOKEN"]``,\n'
            '``os.environ.get("...API_KEY")``) instead of resolving it through the SDK\n'
            "secret store.  Raw env reads bypass the typed ``CredentialRef`` contract and\n"
            "the Dapr-backed ``SecretStore`` (``application_sdk.infrastructure.secrets``),\n"
            "so there is no central record of which secrets an app consumes.\n"
            "\n"
            "This rule is **app-scoped**: the SDK itself is the *provider* of the seam —\n"
            "``EnvironmentSecretStore`` legitimately reads ``os.environ`` — so it never\n"
            "fires on the SDK.  Environment *writes* (``os.environ[x] = v``), endpoint URLs\n"
            "(``..._TOKEN_URL``), public identifiers (``..._ACCESS_KEY_ID``), store-name\n"
            "reads (``SECRET_STORE_NAME``), and dev harnesses (``run_dev*.py``, ``scripts/``)\n"
            "are not flagged.  A reviewed exception — e.g. platform self-auth with no SDK\n"
            "seam — is suppressed inline: ``# conformance: ignore[S002] <reason>`` (BLDX-1419).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
            "conformance/docs/rules/security.md#s002"
        ),
    ),
)
