"""Dapr-backed credential vault for GUID-based runtime credential resolution.

Fetches credential config from S3 via Dapr binding, then resolves secret
references via the Dapr secret store.
"""

import asyncio
import copy
import hashlib
import re
from enum import Enum
from typing import Any

import orjson

from application_sdk.errors.leaves import (
    ColdStartRaceError,
    DaprSidecarUnreachableError,
)
from application_sdk.infrastructure._dapr.client import (
    classify_secret_fetch_error,
    is_dapr_transport_unavailable,
)
from application_sdk.infrastructure._dapr.http import (
    DAPR_SECRET_STORE_COMPONENT,
    DAPR_UPSTREAM_BINDING_COMPONENT,
    AsyncDaprClient,
    retry_past_dapr_cold_start,
)
from application_sdk.infrastructure._secret_utils import process_secret_data
from application_sdk.infrastructure.bindings import BindingError
from application_sdk.infrastructure.secrets import (
    SecretNotFoundError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Allowlist: UUIDs, hex strings, and similar safe identifiers.
_SAFE_GUID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

#: Bounded fan-out for the single-key probe sweep (BLDX-1594). Duplicated rather
#: than imported from :mod:`application_sdk.credentials.agent`'s twin, which
#: lives in the ``credentials`` layer this one must not depend on. The two copies
#: can drift; centralizing them is tracked in
#: https://github.com/atlanhq/application-sdk/issues/2995.
_MAX_CONCURRENT_SINGLE_KEY_PROBES = 8


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class _CredentialSource(str, Enum):
    DIRECT = "direct"
    AGENT = "agent"


class _SecretMode(Enum):
    MULTI_KEY = "multi-key"
    SINGLE_KEY = "single-key"


def _resolve_credentials(
    credential_config: dict[str, Any], secret_data: dict[str, Any]
) -> dict[str, Any]:
    """Substitute secret references in *credential_config* with values from *secret_data*."""
    credentials = copy.deepcopy(credential_config)
    for key, value in list(credentials.items()):
        if isinstance(value, str) and value in secret_data:
            credentials[key] = secret_data[value]
    if "extra" in credentials and isinstance(credentials["extra"], dict):
        for key, value in list(credentials["extra"].items()):
            if isinstance(value, str) and value in secret_data:
                credentials["extra"][key] = secret_data[value]
    return credentials


# ---------------------------------------------------------------------------
# DaprCredentialVault
# ---------------------------------------------------------------------------


class DaprCredentialVault:
    """Dapr-backed credential vault for GUID-based runtime credential resolution.

    Fetches a credential config record from the upstream object store (S3 via
    Dapr binding), then resolves secret references within it via the Dapr
    secret store.
    """

    def __init__(
        self,
        client: AsyncDaprClient,
        *,
        upstream_binding_name: str | None = None,
        secret_store_name: str | None = None,
    ) -> None:
        # Deferred import: circular dependency with constants module
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only on credential resolution
            DEPLOYMENT_OBJECT_STORE_NAME,
            SECRET_STORE_NAME,
            UPSTREAM_OBJECT_STORE_NAME,
        )
        from application_sdk.infrastructure._dapr.client import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
            DaprBinding,
        )

        self._client = client
        if upstream_binding_name is not None:
            credential_binding = upstream_binding_name
        else:
            # Mirror App.upload()/download(): use the upstream store when it is
            # configured (component YAML present), fall back to the deployment
            # store otherwise.  Heracles writes credential configs to whichever
            # store the app is pointed at; in non-SDR / local / CI environments
            # that is the deployment store.
            import os  # noqa: PLC0415 — cold path
            from pathlib import Path  # noqa: PLC0415 — cold path

            from application_sdk.storage.binding import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                is_binding_configured,
            )

            components_dir = Path(
                os.environ.get("DAPR_COMPONENTS_PATH", "./components")
            )
            upstream_configured = is_binding_configured(
                UPSTREAM_OBJECT_STORE_NAME, components_dir=components_dir
            )
            credential_binding = (
                UPSTREAM_OBJECT_STORE_NAME
                if upstream_configured
                else DEPLOYMENT_OBJECT_STORE_NAME
            )
        self._upstream = DaprBinding(client, credential_binding)
        self._secret_store_name: str = secret_store_name or SECRET_STORE_NAME

    async def get_credentials(self, credential_guid: str) -> dict[str, Any]:
        """Resolve the full credential dict for *credential_guid*.

        1. Fetches the credential config JSON from the upstream S3 store
           (path: ``persistent-artifacts/apps/{app}/credentials/{guid}/config.json``).
        2. Determines the secret resolution mode (multi-key or single-key).
        3. Fetches secrets from the Dapr secret store.
        4. Returns the merged credential dict.

        Raises:
            CredentialVaultError: If any step fails.
        """
        # Deferred import: circular dependency
        from application_sdk.infrastructure.credential_vault import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
            CredentialVaultError,
        )

        try:
            credential_config = await self._fetch_credential_config(credential_guid)

            credential_source_str = credential_config.get(
                "credentialSource", _CredentialSource.DIRECT.value
            )
            try:
                credential_source = _CredentialSource(credential_source_str)
            except ValueError:
                logger.warning(
                    "Unknown credentialSource=%r; defaulting to DIRECT",
                    credential_source_str,
                    exc_info=True,
                )
                credential_source = _CredentialSource.DIRECT

            secret_path = credential_config.get("secret-path")

            if credential_source == _CredentialSource.DIRECT or secret_path:
                mode = _SecretMode.MULTI_KEY
            else:
                mode = _SecretMode.SINGLE_KEY

            secret_data: dict[str, Any] = {}

            if mode == _SecretMode.MULTI_KEY:
                key_to_fetch = (
                    secret_path
                    if credential_source == _CredentialSource.AGENT
                    else credential_guid
                )
                logger.debug("Fetching multi-key secret: %s", key_to_fetch)
                # No local swallow: _get_secret already treats a definitively
                # absent bundle as {} (expected — not every credential has
                # one); anything else (a cold-start race, a genuine store
                # rejection) propagates to the except Exception below and
                # becomes a typed CredentialVaultError, rather than being
                # silently downgraded to "no secrets" the way it used to be.
                secret_data = await self._get_secret(key_to_fetch)
            else:
                secret_data = await self._fetch_single_key_secrets(credential_config)

            if credential_source == _CredentialSource.DIRECT:
                credential_config.update(secret_data)
                return credential_config
            else:
                return _resolve_credentials(credential_config, secret_data)

        except CredentialVaultError:
            raise
        # conformance: ignore[E004] outer re-raise wraps all failures into CredentialVaultError; no logging needed here
        except Exception as e:
            raise CredentialVaultError(
                f"Failed to resolve credentials for {credential_guid}: {e}",
                credential_guid=credential_guid,
                cause=e,
            ) from e

    async def _fetch_credential_config(self, credential_guid: str) -> dict[str, Any]:
        """Fetch the credential config JSON from the upstream object store.

        Retries a cold-start race via :func:`retry_past_dapr_cold_start`,
        same as :meth:`_get_secret` — this is typically the *first* Dapr
        call ``get_credentials()`` makes (before the secret-store fetch), so
        it races the identical cold sidecar. A transport/5xx failure here is
        reclassified as a :class:`ColdStartRaceError` (via
        :func:`~application_sdk.infrastructure._dapr.client.is_dapr_transport_unavailable`,
        the same predicate :func:`~application_sdk.infrastructure._dapr.client.classify_secret_fetch_error`
        uses) so :meth:`get_credentials`'s caller can tell a transient outage
        apart from a genuinely-missing config — without this, a cold-start
        race here would be indistinguishable from "no config" and get
        collapsed into a non-retryable ``CredentialNotFoundError``.

        Raises:
            CredentialVaultError: If the GUID contains unsafe characters or no
                config is found in the upstream store.
        """
        import os  # noqa: PLC0415 — cold path: only on local dev path

        # Deferred imports: circular dependency with constants and storage modules
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only on credential resolution
            APPLICATION_NAME,
            STATE_STORE_PATH_TEMPLATE,
            TEMPORARY_PATH,
        )
        from application_sdk.infrastructure.bindings import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
            BindingResponse,
        )
        from application_sdk.infrastructure.credential_vault import (  # noqa: PLC0415 — circular: infrastructure/__init__.py loads sibling modules
            CredentialVaultError,
        )
        from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage.ops imports execution-related modules
            normalize_key,
        )

        # Validate before interpolation to prevent path traversal.
        if not _SAFE_GUID_RE.match(credential_guid):
            raise CredentialVaultError(
                "Invalid credential GUID — must match [a-zA-Z0-9_-]+: %r"
                % credential_guid,
                credential_guid=credential_guid,
            )

        raw_path = os.path.join(
            TEMPORARY_PATH,
            STATE_STORE_PATH_TEMPLATE.format(
                application_name=APPLICATION_NAME,
                state_type="credentials",
                id=credential_guid,
            ),
        )
        normalized_key = normalize_key(raw_path)

        data = orjson.dumps({"key": normalized_key})
        metadata = {
            "key": normalized_key,
            "fileName": normalized_key,
            "blobName": normalized_key,
        }

        async def _fetch() -> BindingResponse:
            try:
                return await self._upstream.invoke("get", data=data, metadata=metadata)
            except BindingError as exc:
                if exc.cause is not None and is_dapr_transport_unavailable(exc.cause):
                    raise ColdStartRaceError(
                        message=f"Upstream credential-config store unreachable: {exc.cause}",
                        cause=exc.cause,
                    ) from exc
                raise

        response = await retry_past_dapr_cold_start(
            _fetch,
            description=f"Credential-vault config fetch for '{credential_guid}'",
            component=DAPR_UPSTREAM_BINDING_COMPONENT,
        )

        if response.data is None:
            raise CredentialVaultError(
                "No credential config found for GUID %s in upstream store"
                % credential_guid,
                credential_guid=credential_guid,
            )

        return orjson.loads(response.data)

    async def _get_secret(
        self,
        secret_key: str,
        component_name: str | None = None,
        *,
        log_label: str | None = None,
    ) -> dict[str, Any]:
        """Fetch and process a secret from the Dapr secret store.

        Retries a cold-start race via :func:`retry_past_dapr_cold_start` and
        classifies failures via the shared
        :func:`~application_sdk.infrastructure._dapr.client.classify_secret_fetch_error`
        (transport/5xx = unreachable, retried; 4xx = definitive rejection,
        not retried) instead of collapsing every failure into a single
        non-retryable error the way this used to — that made a transient
        sidecar race here indistinguishable from "no secret", so callers
        had no way to retry it and silently proceeded with an incomplete
        credential instead.

        ``log_label`` overrides ``secret_key`` in the retry-warning log
        description — pass a hashed label when ``secret_key`` is a ref-key
        (single-key mode) so its raw value, which encodes secret-store
        topology, never lands in WARNING logs (mirrors
        :mod:`application_sdk.credentials.agent`'s single-key probe).

        Returns ``{}`` when the key is definitively absent from the store,
        or in local-environment deployments to avoid secret store
        dependency during development.
        """
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only on credential resolution
            DEPLOYMENT_NAME,
            LOCAL_ENVIRONMENT,
        )

        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            return self._get_local_secret(secret_key)

        store = component_name or self._secret_store_name

        async def _fetch() -> dict[str, str]:
            try:
                result = await self._client.get_secret(store_name=store, key=secret_key)
            # conformance: ignore[E004] re-raises as typed SecretStore(Unavailable)Error via the shared classifier; cause chain preserved
            except Exception as e:
                raise classify_secret_fetch_error(secret_key, e) from e
            if not result:
                raise SecretNotFoundError(secret_key)
            return result

        try:
            result = await retry_past_dapr_cold_start(
                _fetch,
                description=(
                    f"Credential-vault secret fetch for '{log_label or secret_key}'"
                ),
                component=DAPR_SECRET_STORE_COMPONENT,
            )
        except SecretNotFoundError:
            logger.warning(
                "Secret %s definitively absent from store; returning empty credential",
                log_label or secret_key,
                exc_info=True,
            )
            return {}
        return process_secret_data(result)

    def _get_local_secret(self, secret_key: str) -> dict[str, Any]:
        """Read secret from the local secrets file for development.

        All secrets are stored in a single ``./local/dapr/secrets/secrets.json``
        file keyed by guid. No user input in filenames.
        """
        from pathlib import Path  # noqa: PLC0415 — cold path: only on local dev path

        secrets_file = Path(".", "local", "dapr", "secrets", "secrets.json")
        if not secrets_file.exists():
            logger.debug("No local secrets file found")
            return {}
        try:
            all_secrets = orjson.loads(secrets_file.read_bytes())
            secret = all_secrets.get(secret_key, {})
            if not secret:
                logger.debug("No local secret for key %s", secret_key)
            return secret
        # conformance: ignore[E004] exc_info=True already present on the logger.debug call below
        except Exception:
            logger.debug(
                "Failed to read local secret file for key %s",
                secret_key,
                exc_info=True,
            )
            return {}

    async def _fetch_single_key_secrets(
        self, credential_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch secrets in single-key mode — one lookup per string field value.

        Probes run **concurrently**, bounded by
        :data:`_MAX_CONCURRENT_SINGLE_KEY_PROBES`. They were serial until
        BLDX-1594: a probe that misses pays a full Dapr retry ladder, so serial
        probing cost *ladder × number of non-secret fields*.

        Results are merged **in candidate order**, not completion order: each
        probe's *inner* keys merge into one dict, so a key returned by two
        probes is last-writer-wins, and serial order was the tiebreak. Keeping
        it makes the resolved credential identical to the serial behaviour.

        Raises:
            SecretStoreUnavailableError: If any probe found the store
                unreachable (cold-start outage past the retry budget).
        """
        logger.debug("Single-key mode: fetching secrets per field")

        candidates = _single_key_candidates(credential_config)
        if not candidates:
            return {}

        sem = asyncio.Semaphore(_MAX_CONCURRENT_SINGLE_KEY_PROBES)

        async def _probe(label: str, value: str) -> tuple[dict[str, Any], str | None]:
            async with sem:
                return await self._probe_single_key(label, value)

        # return_exceptions=True: cancelling siblings on the first raise unwinds
        # them mid-backoff and buries the real failure under CancelledError.
        results = await asyncio.gather(
            *(_probe(label, value) for label, value in candidates),
            return_exceptions=True,
        )

        collected: dict[str, Any] = {}
        failed_lookups: list[str] = []
        cold_start_exc: BaseException | None = None

        for (label, value), result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                # Only _probe_single_key's two cold-start branches raise (the
                # terminal SecretStoreUnreachableError and the transient
                # SecretStoreUnavailableError, both ColdStartRaceError
                # subtypes); anything else is a bug, not "this field isn't a
                # secret". First-in-candidate-order wins, which within one
                # sweep is unambiguous: every probe checks the per-component
                # readiness gate before any of them can arm it, so a single
                # gather never mixes the terminal and transient labels.
                if isinstance(result, ColdStartRaceError):
                    cold_start_exc = cold_start_exc or result
                    continue
                raise result
            single_secret, failure_type = result
            if failure_type is not None:
                value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
                failed_lookups.append(
                    "  '%s' → sha256:%s: %s" % (label, value_hash, failure_type)
                )
                continue
            for k, v in single_secret.items():
                if v is None or v == "":
                    continue
                collected[k] = v

        if cold_start_exc is not None:
            raise cold_start_exc

        if not collected and failed_lookups:
            logger.error(
                "Single-key secret resolution failed: no secrets resolved. "
                "%d attempted, all failed:\n%s",
                len(failed_lookups),
                "\n".join(failed_lookups),
            )
        elif failed_lookups:
            logger.debug(
                "Single-key mode: resolved %d secrets, skipped %d non-secret fields",
                len(collected),
                len(failed_lookups),
            )

        return collected

    async def _probe_single_key(
        self, label: str, value: str
    ) -> tuple[dict[str, Any], str | None]:
        """Probe one candidate ref-key.

        Returns ``(secret, failure_type)`` — ``failure_type`` is the exception's
        type name on a genuine store error, else ``None``. A definitively-absent
        key is not a failure here: :meth:`_get_secret` already collapses it to
        ``{}`` without raising.

        Raises:
            SecretStoreUnreachableError: If the store never answered across the
                whole cold-start budget (the terminal case), hash-labelled (see
                below).
            SecretStoreUnavailableError: If the store failed *without* the
                budget being spent (the transient case) — only reachable once
                this component has already answered once, so
                ``retry_past_dapr_cold_start`` short-circuits its wait. Also
                hash-labelled.
        """
        value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
        try:
            single_secret = await self._get_secret(
                value, log_label=f"sha256:{value_hash}"
            )
        except DaprSidecarUnreachableError as exc:
            # Terminal: budget exhausted without one usable answer — not a race a
            # later attempt warms. Keep the terminal type so a persistent outage
            # stays distinguishable from a still-cold one, with the same
            # redaction as the transient branch below (hash label, no cause) and
            # the secret-free component/attempts/elapsed diagnostics preserved.
            raise SecretStoreUnreachableError(
                f"sha256:{value_hash}",
                component=exc.component,
                attempts=exc.attempts,
                elapsed_seconds=exc.elapsed_seconds,
            ) from None
        except ColdStartRaceError:
            # A *steady-state* blip, not an exhausted budget: budget exhaustion
            # always raises DaprSidecarUnreachableError and is caught above, so
            # the only way a bare ColdStartRaceError reaches here is
            # retry_past_dapr_cold_start short-circuiting its wait — this
            # component has already answered once, so the error propagates from
            # `call()` without any retry. Either way the store did not answer,
            # which is not "this field isn't a secret" (that case is already
            # collapsed to {} by _get_secret without raising). Propagate so the
            # caller raises a typed CredentialVaultError instead of silently
            # proceeding with an incomplete credential, mirroring the
            # multi-key branch above.
            #
            # Not a bare `raise` and not `from exc`: SecretStoreUnavailableError
            # (the only ColdStartRaceError subtype this path raises) carries
            # the raw ref-key in both `.secret_name` and `__str__()`, and its
            # `cause` (the underlying httpx exception) can re-embed the same
            # ref-key via a percent-encoded request URL. Re-raise a
            # hash-labelled, cause-free equivalent instead of the original.
            raise SecretStoreUnavailableError(f"sha256:{value_hash}") from None
        # conformance: ignore[E004] exc_info=True would leak the raw ref-key (SecretStoreError.__str__ embeds `secret=<ref-key>`); hash + exception type name logged instead, mirroring retry_past_dapr_cold_start's warning log
        except Exception as e:
            logger.debug(
                "Secret resolution failed for '%s' (sha256:%s): %s",
                label,
                value_hash,
                type(e).__name__,
            )
            return {}, type(e).__name__
        return single_secret or {}, None


def _single_key_candidates(credential_config: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered, de-duplicated ``(label, value)`` pairs to probe as secret keys.

    Collected up-front because the probes run concurrently — a ``seen`` set
    mutated per-probe would race. Order matters: the caller merges in candidate
    order to keep inner-key collisions resolving as they did when serial.

    No literal-key exemption, unlike the ``credentials/agent.py`` sibling: this
    path's config keys are v2 camelCase, so ``_LITERAL_KEYS`` would not match,
    and inventing a set here risks skipping a field some deployment does use as
    a ref-key. With probes overlapping the extra lookups are cheap.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(label: str, value: Any) -> None:
        if isinstance(value, str) and value.strip() and value not in seen:
            seen.add(value)
            candidates.append((label, value))

    for field, value in credential_config.items():
        if isinstance(value, str):
            _add(field, value)
        elif field == "extra" and isinstance(value, dict):
            for extra_key, extra_value in value.items():
                _add(f"extra.{extra_key}", extra_value)

    return candidates


def create_dapr_credential_vault(
    client: AsyncDaprClient,
    *,
    upstream_binding_name: str | None = None,
    secret_store_name: str | None = None,
) -> "DaprCredentialVault":
    """Create a Dapr-backed credential vault.

    Args:
        client: Dapr client instance.
        upstream_binding_name: Dapr binding component for the credential config store.
            Defaults to ``UPSTREAM_OBJECT_STORE_NAME`` when its component YAML is
            present (SDR / production), otherwise falls back to
            ``DEPLOYMENT_OBJECT_STORE_NAME`` (local / CI / non-SDR).
        secret_store_name: Dapr secret store component name.
            Defaults to the ``SECRET_STORE_NAME`` constant.

    Returns:
        Configured DaprCredentialVault.
    """
    return DaprCredentialVault(
        client,
        upstream_binding_name=upstream_binding_name,
        secret_store_name=secret_store_name,
    )
