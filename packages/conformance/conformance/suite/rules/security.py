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
        canonical_reference=(
            "atlan-metabase-app app/credentials.py — credentials arrive as a "
            "`CredentialRef` built by `build_credential_ref`, or as an inline dict, and "
            'the typed `MetabaseCredential` defaults `password` to "". No string literal '
            "is assigned to a credential-named variable in any shipped app/ module of "
            "the three reference apps."
        ),
        scope=RuleScope.BOTH,
        name="HardcodedCredential",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="credential-storage",
        autofixable=True,
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
            "themselves SCREAMING_SNAKE env-var-name references, message tables (a dict of\n"
            "two or more SCREAMING_SNAKE code keys whose every value is a help-text sentence:\n"
            "six or more words ending in ``.``/``!``/``?`` with at least two common English\n"
            "stopwords, and no PEM block, auth-scheme value or token-shaped word such as\n"
            "``ghp_…``/``sk_…``/``AKIA…``), field-name alias maps (a dict\n"
            "or ``dict(...)`` whose every value is a known provider credential field name,\n"
            'such as ``{"password": "aws_secret_access_key", "username": "aws_access_key_id"}``),\n'
            "and ``Enum`` members are not flagged.  Outside those two dict shapes, a sentence\n"
            "or a field-name-shaped value is still flagged.  A reviewed exception is\n"
            "suppressed inline with a justification:\n"
            "``# conformance: ignore[S001] <reason>`` (BLDX-1419).\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
            "conformance/docs/rules/security.md#s001"
        ),
    ),
    RuleDefinition(
        id="S002",
        canonical_reference=(
            "atlan-metabase-app app/credentials.py — `build_credential_ref` routes the "
            "input to a `CredentialRef` through the SDK's `CredentialRef.resolve` "
            "(`credential_guid` or agent `agent_json`), and app/connector.py "
            "`_build_client` fetches the secret with `self.context.resolve_credential_raw` "
            "and parses it into the typed MetabaseCredential the API client consumes. No "
            "credential-named environment variable is read in either module — resolution "
            "through the seam is what correct looks like, not a justified read."
        ),
        terminal_state=(
            "Zero findings, reached by resolving the secret through CredentialRef / the "
            "SecretStore protocol rather than reading it from the environment. S002 "
            "flags reads only — a credential-named `os.getenv` / `os.environ[...]` / "
            "`.get` / `.pop` — so only a read can be licensed; a directive over an "
            "`os.environ[x] = v` write is inert, because the detector never emits there. "
            "A justified inline `# conformance: ignore[S002] <reason>` IS the correct "
            "end state for one kind of read: platform / transport self-auth the SDK "
            "exposes no secret-store seam for — an `ATLAN_*` token the app uses to call "
            "Atlan itself at process startup, injected into the pod environment before "
            "any credential context exists. Naming 'platform self-auth' is not "
            "sufficient on its own: the reason must name the specific value and the "
            "specific SDK function or seam that cannot supply it, so the suppression "
            "can be retired when that seam ships (BLDX-1419). A read that could go "
            "through credential resolution is never terminal — route it through the "
            "seam instead."
        ),
        scope=RuleScope.APP,
        name="RawEnvCredentialAccess",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="credential-resolution",
        autofixable=True,
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
