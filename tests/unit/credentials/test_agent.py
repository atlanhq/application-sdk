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
    async def test_invalid_agent_json_raises_parse_error(self) -> None:
        store = _store_with()
        with pytest.raises(CredentialParseError, match="not valid JSON"):
            await resolve_agent_json("{not-json", store)

    async def test_agent_json_not_a_dict_raises_parse_error(self) -> None:
        store = _store_with()
        with pytest.raises(CredentialParseError, match="must be a JSON object"):
            await resolve_agent_json(json.dumps(["a", "b"]), store)

    async def test_missing_secret_path_raises_parse_error(self) -> None:
        store = _store_with()
        agent_json = json.dumps({"host": "h", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="secret-path"):
            await resolve_agent_json(agent_json, store)

    async def test_empty_secret_path_raises_parse_error(self) -> None:
        store = _store_with()
        agent_json = json.dumps({"secret-path": "", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="secret-path"):
            await resolve_agent_json(agent_json, store)

    async def test_secret_not_found_raises_credential_not_found(self) -> None:
        store = _store_with()  # empty — no bundle at the path
        agent_json = json.dumps({"secret-path": "missing/path", "basic.username": "u"})
        with pytest.raises(CredentialNotFoundError):
            await resolve_agent_json(agent_json, store)

    async def test_generic_store_failure_wrapped_in_credential_error(self) -> None:
        class FlakyStore:
            async def get(self, name: str) -> str:
                raise SecretStoreError("upstream dapr outage")

        agent_json = json.dumps({"secret-path": "p", "basic.username": "u"})
        with pytest.raises(CredentialError, match="dapr outage"):
            await resolve_agent_json(agent_json, FlakyStore())  # type: ignore[arg-type]

    async def test_bundle_not_valid_json_raises_parse_error(self) -> None:
        store = _store_with(**{"bundle": "not-json-at-all"})
        agent_json = json.dumps({"secret-path": "bundle", "basic.username": "u"})
        with pytest.raises(CredentialParseError, match="not valid JSON"):
            await resolve_agent_json(agent_json, store)

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


class TestCredentialRefFromWorkflowArgs:
    """Unit tests for the ``CredentialRef.from_workflow_args`` factory.

    This is the single construction site where ``extraction_method`` is
    interpreted. Once the factory returns, the ref carries the routing
    decision as typed fields (``agent_json`` or ``credential_guid``),
    and the resolver never touches ``workflow_args`` directly.
    """

    def test_agent_method_and_populated_agent_json_makes_agent_ref(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        agent_json = json.dumps(
            {"secret-path": "atlan/dev/cloudsql", "basic.username": "u"}
        )
        ref = CredentialRef.from_workflow_args(
            {
                "extraction_method": "agent",
                "agent_json": agent_json,
                "credential_guid": "",
            }
        )
        assert ref.agent_json == agent_json
        assert ref.credential_guid == ""

    def test_agent_method_accepts_dict_agent_json(self) -> None:
        """The generated Pydantic ``Input`` contracts declare
        ``agent_json: dict[str, Any]`` even though the wire format is a
        JSON string. The factory handles both shapes.
        """
        from application_sdk.credentials.ref import CredentialRef

        spec_dict = {"secret-path": "p", "basic.username": "u"}
        ref = CredentialRef.from_workflow_args(
            {"extraction_method": "agent", "agent_json": spec_dict}
        )
        # Always normalised to a JSON string on the ref.
        assert isinstance(ref.agent_json, str)
        assert json.loads(ref.agent_json) == spec_dict

    def test_agent_method_case_insensitive_and_trimmed(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        agent_json = json.dumps({"secret-path": "p"})
        for method in ("AGENT", "Agent", " agent ", "agent"):
            ref = CredentialRef.from_workflow_args(
                {"extraction_method": method, "agent_json": agent_json}
            )
            assert ref.agent_json == agent_json, f"failed for method={method!r}"

    def test_populated_agent_json_without_method_falls_through_to_guid(self) -> None:
        """Strict gate: an upstream bug that populates ``agent_json``
        under a non-agent method doesn't silently take the agent path.
        """
        from application_sdk.credentials.ref import CredentialRef

        ref = CredentialRef.from_workflow_args(
            {
                # No extraction_method.
                "agent_json": json.dumps({"secret-path": "p"}),
                "credential_guid": "my-guid-123",
            }
        )
        assert ref.agent_json == ""
        assert ref.credential_guid == "my-guid-123"

    def test_agent_method_with_empty_agent_json_falls_through_to_guid(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        for placeholder in ("", "{}", "   "):
            ref = CredentialRef.from_workflow_args(
                {
                    "extraction_method": "agent",
                    "agent_json": placeholder,
                    "credential_guid": "my-guid-123",
                }
            )
            assert ref.agent_json == "", f"failed for placeholder={placeholder!r}"
            assert ref.credential_guid == "my-guid-123"

    def test_agent_method_with_empty_dict_falls_through_to_guid(self) -> None:
        """Empty dict ``{}`` counts as no agent_json even when typed."""
        from application_sdk.credentials.ref import CredentialRef

        ref = CredentialRef.from_workflow_args(
            {
                "extraction_method": "agent",
                "agent_json": {},
                "credential_guid": "my-guid-123",
            }
        )
        assert ref.agent_json == ""
        assert ref.credential_guid == "my-guid-123"

    def test_direct_method_with_guid_uses_guid_path(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        ref = CredentialRef.from_workflow_args(
            {
                "extraction_method": "direct",
                "agent_json": "{}",
                "credential_guid": "my-guid-123",
            }
        )
        assert ref.credential_guid == "my-guid-123"
        assert ref.agent_json == ""
        assert ref.name == "my-guid-123"  # legacy ref sets name = guid

    def test_guid_only_no_method(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        ref = CredentialRef.from_workflow_args({"credential_guid": "g"})
        assert ref.credential_guid == "g"

    def test_no_routable_source_raises(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        with pytest.raises(ValueError, match="no routable credential source"):
            CredentialRef.from_workflow_args({})

        with pytest.raises(ValueError, match="no routable credential source"):
            CredentialRef.from_workflow_args(
                {"extraction_method": "direct", "agent_json": "", "credential_guid": ""}
            )

    def test_agent_method_but_no_json_and_no_guid_raises(self) -> None:
        from application_sdk.credentials.ref import CredentialRef

        with pytest.raises(ValueError, match="no routable credential source"):
            CredentialRef.from_workflow_args(
                {"extraction_method": "agent", "agent_json": "{}"}
            )


class TestCredentialResolverAgentBranch:
    """Exercises the agent branch of CredentialResolver. Uses a real
    resolver + InMemorySecretStore end-to-end; proves the single typed
    ``resolve_raw(ref)`` API handles agent refs identically to legacy
    refs from the caller's perspective.
    """

    async def test_resolve_raw_agent_ref_returns_flat_dict_with_nested_extra(
        self,
    ) -> None:
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver

        store = _store_with(
            **{
                "atlan/dev/cloudsql": _bundle(
                    username="real_pg_user",
                    password="real_pg_password",
                    database="real_pg_db",
                )
            }
        )
        ref = CredentialRef.from_workflow_args(
            {
                "extraction_method": "agent",
                "agent_json": CLOUDSQL_POSTGRES_AGENT_JSON,
            }
        )
        resolver = CredentialResolver(store)

        resolved = await resolver.resolve_raw(ref)

        assert resolved["host"] == "34.122.182.89"
        assert resolved["port"] == 5432
        assert resolved["basic"] == {
            "username": "real_pg_user",
            "password": "real_pg_password",
        }
        assert resolved["extra"]["database"] == "real_pg_db"

    async def test_resolve_raw_legacy_guid_ref_still_works(self) -> None:
        """Legacy GUID refs continue to resolve via the local secret
        store path — agent branch is additive, not a replacement.
        """
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver

        store = _store_with(**{"my-guid-123": _bundle(username="real_user", host="h")})
        ref = CredentialRef.from_workflow_args(
            {"extraction_method": "direct", "credential_guid": "my-guid-123"}
        )
        resolver = CredentialResolver(store)

        resolved = await resolver.resolve_raw(ref)
        assert resolved == {"username": "real_user", "host": "h"}

    async def test_resolve_typed_agent_ref_uses_auth_type_for_parser(self) -> None:
        """When ``credential_type`` is empty on an agent ref (which is
        what ``from_workflow_args`` produces), the resolver reads
        ``auth-type`` from the agent JSON to pick the registry parser.
        """
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.credentials.types import BasicCredential

        store = _store_with(**{"p": _bundle(username="real_user", password="real_pw")})
        agent_json = json.dumps(
            {
                "secret-path": "p",
                "auth-type": "basic",
                "basic.username": "username",
                "basic.password": "password",
            }
        )
        ref = CredentialRef.from_workflow_args(
            {"extraction_method": "agent", "agent_json": agent_json}
        )
        resolver = CredentialResolver(store)

        cred = await resolver.resolve(ref)
        assert isinstance(cred, BasicCredential)
        assert cred.username == "real_user"
        assert cred.password == "real_pw"
