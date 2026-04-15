"""Tests for :mod:`application_sdk.credentials.agent`.

Covers the agent-shape JSON credential resolution path: substitution of
ref-keys against an external secret bundle, dotted-key expansion into
nested dicts, v2-compatible behaviour for nested ``extra`` payloads,
and the error contract for malformed input.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from application_sdk.credentials.agent import (
    _expand_dotted,
    _substitute,
    resolve_agent_json,
)
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.infrastructure.secrets import InMemorySecretStore, SecretStoreError

# ---------------------------------------------------------------------------
# Fixtures — based on the real cloudsql-postgres agent payload from dbbi-331
# ---------------------------------------------------------------------------

#: The literal agent_json string carried on the v3 workflow payload for
#: a cloudsql-postgres agent-extraction run. Mirrors the real shape
#: emitted by the Argo marketplace template as of dbbi-331.
CLOUDSQL_POSTGRES_AGENT_JSON = json.dumps(
    {
        "connectBy": "host",
        "agent-name": "cloudsql-postgres-agent",
        "secret-manager": "awssecretmanager",
        "secret-path": "atlan/dev/cloudsql",
        "aws-region": "ap-south-1",
        "aws-auth-method": "access-key",
        "azure-auth-method": "managed_identity",
        "extra.compiled_url": "postgresql+asyncpg://34.122.182.89:5432/database",
        "host": "34.122.182.89",
        "port": 5432,
        "auth-type": "basic",
        "basic.username": "username",
        "basic.password": "password",
        "extra.database": "database",
    }
)


def _bundle(**values: str) -> str:
    """Helper: serialize a dict as the JSON string a SecretStore returns."""
    return json.dumps(values)


def _store_with(**bundles: str) -> InMemorySecretStore:
    """Helper: build an InMemorySecretStore preloaded with ``{path: json}``."""
    store = InMemorySecretStore()
    for path, raw in bundles.items():
        store.set(path, raw)
    return store


# ---------------------------------------------------------------------------
# resolve_agent_json — happy path
# ---------------------------------------------------------------------------


class TestResolveAgentJsonHappyPath:
    @pytest.mark.asyncio
    async def test_resolves_cloudsql_postgres_payload_to_nested_extra(self) -> None:
        """The exact dbbi-331 payload resolves to a dict that drops
        straight into the v3 SQL client's ``load(credentials)``.
        """
        store = _store_with(
            **{
                "atlan/dev/cloudsql": _bundle(
                    username="real_pg_user",
                    password="real_pg_password",
                    database="real_pg_db",
                )
            }
        )

        resolved = await resolve_agent_json(CLOUDSQL_POSTGRES_AGENT_JSON, store)

        # Literal fields are preserved as-is.
        assert resolved["host"] == "34.122.182.89"
        assert resolved["port"] == 5432
        assert resolved["auth-type"] == "basic"
        assert resolved["aws-region"] == "ap-south-1"

        # Ref-keys substituted and collapsed into nested dicts.
        assert resolved["basic"] == {
            "username": "real_pg_user",
            "password": "real_pg_password",
        }
        assert resolved["extra"] == {
            "database": "real_pg_db",
            # compiled_url was a literal value (not in bundle), copied through.
            "compiled_url": "postgresql+asyncpg://34.122.182.89:5432/database",
        }

        # No dotted keys remain after expansion.
        assert not any("." in k for k in resolved)

    @pytest.mark.asyncio
    async def test_literal_keys_never_substituted_even_if_they_match_bundle(
        self,
    ) -> None:
        """``host``/``port``/``aws-region`` are literals. If a bundle
        happens to carry a key of the same string, the literal wins.
        """
        agent_json = json.dumps(
            {
                "secret-path": "bundle",
                "host": "literal.example.com",
                "port": 5432,
                "aws-region": "ap-south-1",
                "basic.username": "username",
            }
        )
        # Bundle contains a key `literal.example.com` — but ``host`` is
        # in _LITERAL_KEYS so it should never be treated as a ref.
        store = _store_with(
            **{
                "bundle": _bundle(
                    **{
                        "literal.example.com": "SHOULD_NOT_WIN",
                        "ap-south-1": "SHOULD_NOT_WIN",
                        "username": "real_user",
                    }
                )
            }
        )

        resolved = await resolve_agent_json(agent_json, store)

        assert resolved["host"] == "literal.example.com"
        assert resolved["aws-region"] == "ap-south-1"
        assert resolved["basic"] == {"username": "real_user"}

    @pytest.mark.asyncio
    async def test_missing_ref_key_stays_as_ref(self) -> None:
        """v2 parity: if the bundle doesn't have a ref-key, leave the
        placeholder in place rather than raising. Downstream SQL connect
        will surface a meaningful error.
        """
        agent_json = json.dumps(
            {
                "secret-path": "bundle",
                "host": "h",
                "basic.username": "username",  # present in bundle
                "basic.password": "password",  # MISSING from bundle
            }
        )
        store = _store_with(**{"bundle": _bundle(username="real_user")})

        resolved = await resolve_agent_json(agent_json, store)

        assert resolved["basic"]["username"] == "real_user"
        # Ref-key preserved verbatim when no bundle entry matches.
        assert resolved["basic"]["password"] == "password"

    @pytest.mark.asyncio
    async def test_v2_style_nested_extra_also_resolved(self) -> None:
        """Backward compat: if the upstream emits v2-style nested
        ``extra: {k: ref}``, those refs are also substituted.
        """
        agent_json = json.dumps(
            {
                "secret-path": "bundle",
                "host": "h",
                "auth-type": "basic",
                "username": "user_ref",
                "password": "pass_ref",
                "extra": {
                    "database": "db_ref",
                    "compiled_url": "postgresql+asyncpg://...",
                },
            }
        )
        store = _store_with(
            **{
                "bundle": _bundle(
                    user_ref="real_user", pass_ref="real_pw", db_ref="real_db"
                )
            }
        )

        resolved = await resolve_agent_json(agent_json, store)

        assert resolved["username"] == "real_user"
        assert resolved["password"] == "real_pw"
        assert resolved["extra"]["database"] == "real_db"
        # Literal inside nested extra is preserved.
        assert resolved["extra"]["compiled_url"] == "postgresql+asyncpg://..."

    @pytest.mark.asyncio
    async def test_bundle_returned_as_dict_directly(self) -> None:
        """Some SecretStore backends may return a pre-parsed dict rather
        than a JSON string. Accept both.
        """

        class DictReturningStore:
            async def get(self, name: str) -> Any:
                return {"user_ref": "real_user"}

        agent_json = json.dumps({"secret-path": "bundle", "basic.username": "user_ref"})
        resolved = await resolve_agent_json(agent_json, DictReturningStore())  # type: ignore[arg-type]
        assert resolved["basic"] == {"username": "real_user"}


# ---------------------------------------------------------------------------
# resolve_agent_json — error paths
# ---------------------------------------------------------------------------


class TestResolveAgentJsonErrorPaths:
    @pytest.mark.asyncio
    async def test_invalid_agent_json_raises_parse_error(self) -> None:
        store = _store_with()
        with pytest.raises(CredentialParseError, match="not valid JSON"):
            await resolve_agent_json("{not-json", store)

    @pytest.mark.asyncio
    async def test_agent_json_not_a_dict_raises_parse_error(self) -> None:
        store = _store_with()
        with pytest.raises(CredentialParseError, match="must be a JSON object"):
            await resolve_agent_json(json.dumps(["a", "b"]), store)

    @pytest.mark.asyncio
    async def test_missing_secret_path_raises_parse_error(self) -> None:
        store = _store_with()
        agent_json = json.dumps({"host": "h", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="secret-path"):
            await resolve_agent_json(agent_json, store)

    @pytest.mark.asyncio
    async def test_empty_secret_path_raises_parse_error(self) -> None:
        store = _store_with()
        agent_json = json.dumps({"secret-path": "", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="secret-path"):
            await resolve_agent_json(agent_json, store)

    @pytest.mark.asyncio
    async def test_secret_not_found_raises_credential_not_found(self) -> None:
        store = _store_with()  # empty — no bundle at the path
        agent_json = json.dumps({"secret-path": "missing/path", "basic.username": "u"})
        with pytest.raises(CredentialNotFoundError):
            await resolve_agent_json(agent_json, store)

    @pytest.mark.asyncio
    async def test_generic_store_failure_wrapped_in_credential_error(self) -> None:
        class FlakyStore:
            async def get(self, name: str) -> str:
                raise SecretStoreError("upstream dapr outage")

        agent_json = json.dumps({"secret-path": "p", "basic.username": "u"})
        with pytest.raises(CredentialError, match="dapr outage"):
            await resolve_agent_json(agent_json, FlakyStore())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_bundle_not_valid_json_raises_parse_error(self) -> None:
        store = _store_with(**{"bundle": "not-json-at-all"})
        agent_json = json.dumps({"secret-path": "bundle", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="not valid JSON"):
            await resolve_agent_json(agent_json, store)

    @pytest.mark.asyncio
    async def test_bundle_not_a_dict_raises_parse_error(self) -> None:
        store = _store_with(**{"bundle": json.dumps(["a", "b"])})
        agent_json = json.dumps({"secret-path": "bundle", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="must be a JSON object"):
            await resolve_agent_json(agent_json, store)


# ---------------------------------------------------------------------------
# _substitute — unit tests for the substitution step in isolation
# ---------------------------------------------------------------------------


class TestSubstitute:
    def test_root_level_ref_keys_are_replaced(self) -> None:
        agent = {"username": "user_ref", "password": "pass_ref"}
        bundle = {"user_ref": "real_user", "pass_ref": "real_pw"}
        assert _substitute(agent, bundle) == {
            "username": "real_user",
            "password": "real_pw",
        }

    def test_literal_keys_are_skipped(self) -> None:
        agent = {"host": "h.example.com", "port": 5432, "username": "user_ref"}
        bundle = {"h.example.com": "SHOULD_NOT_WIN", "user_ref": "real_user"}
        assert _substitute(agent, bundle) == {
            "host": "h.example.com",
            "port": 5432,
            "username": "real_user",
        }

    def test_non_string_values_pass_through(self) -> None:
        agent = {"port": 5432, "enabled": True, "username": "user_ref"}
        bundle = {"user_ref": "real_user"}
        assert _substitute(agent, bundle) == {
            "port": 5432,
            "enabled": True,
            "username": "real_user",
        }

    def test_nested_extra_dict_is_walked_one_level(self) -> None:
        agent = {"extra": {"database": "db_ref", "pool_size": 10}}
        bundle = {"db_ref": "real_db"}
        assert _substitute(agent, bundle) == {
            "extra": {"database": "real_db", "pool_size": 10}
        }

    def test_unreferenced_values_remain(self) -> None:
        agent = {"username": "user_ref"}
        bundle = {"other_ref": "other"}
        assert _substitute(agent, bundle) == {"username": "user_ref"}


# ---------------------------------------------------------------------------
# _expand_dotted — unit tests for dotted-key collapse
# ---------------------------------------------------------------------------


class TestExpandDotted:
    def test_empty_dict(self) -> None:
        assert _expand_dotted({}) == {}

    def test_no_dotted_keys_passthrough(self) -> None:
        flat = {"host": "h", "port": 5432}
        assert _expand_dotted(flat) == {"host": "h", "port": 5432}

    def test_single_dot_collapses_to_one_nested_dict(self) -> None:
        flat = {"basic.username": "u", "basic.password": "p"}
        assert _expand_dotted(flat) == {"basic": {"username": "u", "password": "p"}}

    def test_multiple_roots(self) -> None:
        flat = {
            "basic.username": "u",
            "extra.database": "d",
            "extra.compiled_url": "url",
            "host": "h",
        }
        assert _expand_dotted(flat) == {
            "basic": {"username": "u"},
            "extra": {"database": "d", "compiled_url": "url"},
            "host": "h",
        }

    def test_deeply_nested(self) -> None:
        flat = {"a.b.c": 1, "a.b.d": 2, "a.e": 3}
        assert _expand_dotted(flat) == {"a": {"b": {"c": 1, "d": 2}, "e": 3}}

    def test_mixed_dotted_and_nested_root_merges(self) -> None:
        """If a non-dotted ``extra: {}`` and a dotted ``extra.foo: bar``
        both appear, they merge into one dict (non-dotted copied first).
        """
        flat = {"extra": {"database": "d"}, "extra.compiled_url": "url"}
        assert _expand_dotted(flat) == {
            "extra": {"database": "d", "compiled_url": "url"}
        }

    def test_dotted_key_conflicting_with_non_dict_root_is_dropped(self) -> None:
        """Pathological: ``extra: "string"`` then ``extra.foo: "bar"``.
        Non-dict root wins; dotted key is silently dropped.
        """
        flat = {"extra": "just-a-string", "extra.foo": "bar"}
        assert _expand_dotted(flat) == {"extra": "just-a-string"}


# ---------------------------------------------------------------------------
# Integration: AppContext.resolve_workflow_credentials routing
# ---------------------------------------------------------------------------


class TestResolveWorkflowCredentialsRouting:
    """Exercises the routing helper on AppContext. Uses the real
    AppContext wiring (no Temporal dependency needed for these tests).
    """

    def _make_context(self, store: InMemorySecretStore):
        from application_sdk.app.context import AppContext

        ctx = AppContext(
            app_name="test-app",
            app_version="0.0.0",
            run_id="run-1",
            correlation_id="",
        )
        ctx._secret_store = store
        return ctx

    @pytest.mark.asyncio
    async def test_agent_path_fires_when_method_and_json_both_present(
        self,
    ) -> None:
        """Happy path: ``extraction_method == 'agent'`` AND a non-empty
        ``agent_json`` both present → agent resolution fires.
        """
        store = _store_with(**{"atlan/dev/cloudsql": _bundle(username="real_user")})
        ctx = self._make_context(store)

        workflow_args = {
            "extraction_method": "agent",
            "agent_json": json.dumps(
                {
                    "secret-path": "atlan/dev/cloudsql",
                    "host": "h",
                    "basic.username": "username",
                }
            ),
            "credential_guid": "",
        }
        resolved = await ctx.resolve_workflow_credentials(workflow_args)
        assert resolved["basic"]["username"] == "real_user"

    @pytest.mark.asyncio
    async def test_agent_method_mixed_case_is_normalised(self) -> None:
        """``extraction_method`` is case-insensitive — ``"AGENT"``,
        ``"Agent"``, ``" agent "`` all count.
        """
        store = _store_with(**{"p": _bundle(username="real_user")})
        ctx = self._make_context(store)

        for method in ("AGENT", "Agent", " agent "):
            resolved = await ctx.resolve_workflow_credentials(
                {
                    "extraction_method": method,
                    "agent_json": json.dumps(
                        {"secret-path": "p", "basic.username": "username"}
                    ),
                }
            )
            assert resolved["basic"]["username"] == "real_user"

    @pytest.mark.asyncio
    async def test_agent_json_without_method_falls_through_to_guid(self) -> None:
        """Strict gate: a populated ``agent_json`` WITHOUT
        ``extraction_method == "agent"`` is NOT treated as an agent
        run. Falls through to the legacy GUID path instead.
        """
        store = _store_with(**{"my-guid-123": _bundle(username="real_user", host="h")})
        ctx = self._make_context(store)

        workflow_args = {
            # No extraction_method set.
            "agent_json": json.dumps(
                {"secret-path": "whatever", "basic.username": "u"}
            ),
            "credential_guid": "my-guid-123",
        }
        resolved = await ctx.resolve_workflow_credentials(workflow_args)
        # Legacy GUID path returns bundle verbatim — proves agent path
        # was skipped despite populated agent_json.
        assert resolved == {"username": "real_user", "host": "h"}

    @pytest.mark.asyncio
    async def test_agent_method_with_empty_json_falls_through_to_guid(
        self,
    ) -> None:
        """Strict gate: ``extraction_method == "agent"`` WITHOUT a
        non-empty ``agent_json`` is NOT the agent path. Falls through.
        """
        store = _store_with(**{"my-guid-123": _bundle(username="real_user", host="h")})
        ctx = self._make_context(store)

        workflow_args = {
            "extraction_method": "agent",
            "agent_json": "{}",  # Placeholder — doesn't qualify.
            "credential_guid": "my-guid-123",
        }
        resolved = await ctx.resolve_workflow_credentials(workflow_args)
        assert resolved == {"username": "real_user", "host": "h"}

    @pytest.mark.asyncio
    async def test_direct_method_with_guid(self) -> None:
        """``extraction_method == "direct"`` with a credential_guid uses
        the legacy GUID path.
        """
        store = _store_with(**{"my-guid-123": _bundle(username="real_user", host="h")})
        ctx = self._make_context(store)

        resolved = await ctx.resolve_workflow_credentials(
            {
                "extraction_method": "direct",
                "agent_json": "{}",
                "credential_guid": "my-guid-123",
            }
        )
        assert resolved == {"username": "real_user", "host": "h"}

    @pytest.mark.asyncio
    async def test_neither_source_raises_value_error(self) -> None:
        store = _store_with()
        ctx = self._make_context(store)

        with pytest.raises(ValueError, match="no credential source"):
            await ctx.resolve_workflow_credentials(
                {
                    "extraction_method": "direct",
                    "agent_json": "",
                    "credential_guid": "",
                }
            )

    @pytest.mark.asyncio
    async def test_missing_keys_entirely_raises_value_error(self) -> None:
        store = _store_with()
        ctx = self._make_context(store)

        with pytest.raises(ValueError, match="no credential source"):
            await ctx.resolve_workflow_credentials({})

    @pytest.mark.asyncio
    async def test_no_secret_store_raises_runtime_error(self) -> None:
        from application_sdk.app.context import AppContext

        ctx = AppContext(
            app_name="test-app",
            app_version="0.0.0",
            run_id="run-1",
            correlation_id="",
        )
        # _secret_store stays None
        with pytest.raises(RuntimeError, match="No secret store"):
            await ctx.resolve_workflow_credentials(
                {
                    "extraction_method": "agent",
                    "agent_json": json.dumps({"secret-path": "p"}),
                }
            )

    @pytest.mark.asyncio
    async def test_whitespace_only_agent_json_with_method_falls_through(
        self,
    ) -> None:
        """Whitespace-only ``agent_json`` is treated as absent even
        under ``extraction_method == "agent"``.
        """
        store = _store_with(**{"my-guid-123": _bundle(username="real_user")})
        ctx = self._make_context(store)

        resolved = await ctx.resolve_workflow_credentials(
            {
                "extraction_method": "agent",
                "agent_json": "   ",
                "credential_guid": "my-guid-123",
            }
        )
        assert resolved["username"] == "real_user"
