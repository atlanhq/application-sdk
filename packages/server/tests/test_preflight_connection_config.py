"""The preflight connection-config surface and the ``preflight_tiers`` seam.

Why these exist: a connector whose real preflight is a multi-tier permission
check was ported onto :class:`SQLHandler` by declaring class attributes only,
which silently replaced every authorization row with the base ``SELECT 1``
connectivity probe. The blocking tier needs the setup form's include filter,
i.e. ``input.connection_config`` — a field that was already on
:class:`PreflightInput` and already populated by the route, but that the generic
handler never read and that had no seam to read it *from*. That seam is
``preflight_tiers``.

Scope: unit-level. No real data source is contacted anywhere here — the client
is a stub that records the SQL it was asked to run, so "connectivity passed" in
these tests means "the base class took its success path", not "a warehouse
answered". Exercising the real probe requires live credentials and is not
simulated.
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from server_sdk.clients.models import DatabaseConfig
from server_sdk.clients.sql import BaseSQLClient
from server_sdk.errors.leaves import InvalidInputError
from server_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    BaseConnectionConfig,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from server_sdk.handler.sql import SQLHandler
from server_sdk.server import build_asgi_app

# ---------------------------------------------------------------------------
# Stub client — records queries, never touches a network
# ---------------------------------------------------------------------------


class _StubClient(BaseSQLClient):
    """Stands in for a connector client. Records queries; opens nothing."""

    DB_CONFIG = DatabaseConfig(
        template="stub://{username}:{password}@{host}:{port}/{database}",
        required=["host", "port", "database", "username", "password"],
        defaults={"port": 5439, "database": "dev"},
    )

    queries: ClassVar[list[str]] = []
    closed: ClassVar[int] = 0
    fail_with: ClassVar[Exception | None] = None

    async def load(self, credentials: dict[str, Any]) -> None:
        self.credentials = credentials or {}
        # Keep the base class's real credential validation in the path — a
        # missing required field must still raise, as it does in production.
        self.get_sqlalchemy_connection_string()
        self.engine = object()

    async def run_query(self, query: str, batch_size: int = 100000):  # type: ignore[override]
        type(self).queries.append(query)
        if type(self).fail_with is not None:
            raise type(self).fail_with
        yield [{"ok": 1}]

    async def close(self) -> None:
        type(self).closed += 1
        self.engine = None

    @classmethod
    def reset(cls) -> None:
        cls.queries = []
        cls.closed = 0
        cls.fail_with = None


class _Plain(SQLHandler):
    """A connector ported with class attributes only — no preflight override."""

    client_class = _StubClient
    filter_metadata_sql = "SELECT catalog_name, schema_name FROM stub"


CREDS = [
    {"key": "host", "value": "warehouse.example.internal"},
    {"key": "username", "value": "svc"},
    {"key": "password", "value": "pw"},
]


@pytest.fixture(autouse=True)
def _clean() -> None:
    _StubClient.reset()


# ---------------------------------------------------------------------------
# 1. A handler that overrides the preflight receives connection_config
# ---------------------------------------------------------------------------


class _Tiered(SQLHandler):
    """The shape a real tiered connector uses: append rows, block on the tier."""

    client_class = _StubClient
    filter_metadata_sql = "SELECT catalog_name, schema_name FROM stub"

    seen: ClassVar[dict[str, Any]] = {}

    async def preflight_tiers(
        self,
        *,
        input: PreflightInput,
        client: BaseSQLClient,
        checks: list[PreflightCheck],
        deadline: float | None,
    ) -> PreflightOutput | None:
        type(self).seen = {
            "include_filter": input.connection_config.get("include-filter"),
            "as_dict": input.connection_config.as_dict(),
            "entrypoint": input.entrypoint,
            "deadline": deadline,
            "tier1_names": [c.name for c in checks],
            "client_is_tier1": client is not None,
        }
        allowed = bool(input.connection_config.get("include-filter"))
        checks.append(
            PreflightCheck(
                name="databaseSchemaCheck",
                passed=allowed,
                message="Authorized" if allowed else "No include filter to authorize",
            )
        )
        if not allowed:
            return PreflightOutput(status=PreflightStatus.NOT_READY, checks=checks)
        checks.append(PreflightCheck(name="tablesCheck", passed=True, message="ok"))
        return PreflightOutput(status=PreflightStatus.READY, checks=checks)


@pytest.mark.asyncio
async def test_overriding_handler_receives_connection_config():
    """The blocking tier can finally see the include filter it authorizes by."""
    result = await _Tiered().preflight_check(
        PreflightInput(
            credentials=CREDS,
            entrypoint="redshift-crawler",
            connection_config={"include-filter": '{"dev":["public"]}'},
            timeout_seconds=25,
        )
    )
    assert _Tiered.seen["include_filter"] == '{"dev":["public"]}'
    assert _Tiered.seen["as_dict"] == {"include-filter": '{"dev":["public"]}'}
    assert _Tiered.seen["entrypoint"] == "redshift-crawler"
    assert _Tiered.seen["tier1_names"] == ["connectivity"]
    assert _Tiered.seen["client_is_tier1"] is True
    assert result.status is PreflightStatus.READY
    assert [c.name for c in result.checks] == [
        "connectivity",
        "databaseSchemaCheck",
        "tablesCheck",
    ]


@pytest.mark.asyncio
async def test_deadline_is_absolute_and_derived_from_timeout():
    """deadline is a monotonic cutoff, not a duration — bounded by the budget."""
    before = time.monotonic()
    await _Tiered().preflight_check(
        PreflightInput(
            credentials=CREDS,
            connection_config={"include-filter": "x"},
            timeout_seconds=25,
        )
    )
    deadline = _Tiered.seen["deadline"]
    assert deadline is not None
    assert before + 25 <= deadline <= time.monotonic() + 25


@pytest.mark.asyncio
async def test_no_timeout_means_no_deadline():
    await _Tiered().preflight_check(
        PreflightInput(
            credentials=CREDS,
            connection_config={"include-filter": "x"},
            timeout_seconds=0,
        )
    )
    assert _Tiered.seen["deadline"] is None


@pytest.mark.asyncio
async def test_failed_required_tier_blocks_and_keeps_prior_rows():
    """A tier that cannot authorize must report NOT_READY, never a bare READY."""
    result = await _Tiered().preflight_check(
        PreflightInput(credentials=CREDS, connection_config={})
    )
    assert result.status is PreflightStatus.NOT_READY
    assert [c.name for c in result.checks] == ["connectivity", "databaseSchemaCheck"]
    assert result.checks[1].passed is False


@pytest.mark.asyncio
async def test_tier_reuses_the_tier1_client_no_second_connect():
    """One connect for the whole preflight — the budget cannot afford two."""
    await _Tiered().preflight_check(
        PreflightInput(credentials=CREDS, connection_config={"include-filter": "x"})
    )
    assert _StubClient.queries == ["SELECT 1"]
    assert _StubClient.closed == 1


@pytest.mark.asyncio
async def test_raising_from_a_tier_is_not_a_500():
    """The base boundary owns the failure: NOT_READY, and the client is closed."""

    class _Boom(_Plain):
        async def preflight_tiers(self, **kwargs: Any) -> PreflightOutput | None:
            raise RuntimeError("permission probe exploded")

    result = await _Boom().preflight_check(PreflightInput(credentials=CREDS))
    assert result.status is PreflightStatus.NOT_READY
    assert [c.name for c in result.checks] == ["connectivity"]
    assert "permission probe exploded" in result.checks[0].message
    assert result.checks[0].passed is False
    assert _StubClient.closed == 1


# ---------------------------------------------------------------------------
# 2. A handler that does NOT override behaves byte-identically to before
# ---------------------------------------------------------------------------

# Captured from the pre-change SQLHandler.preflight_check success path. Anything
# but duration_ms (a clock reading) is pinned exactly.
GOLDEN_CONNECTIVITY = {
    "name": "connectivity",
    "passed": True,
    "message": "Connected and authenticated",
    "error": None,
}


@pytest.mark.asyncio
async def test_default_handler_output_is_unchanged():
    """The default path emits exactly one connectivity row, same strings."""
    result = await _Plain().preflight_check(
        PreflightInput(
            credentials=CREDS,
            connection_config={"include-filter": "ignored-by-the-default-path"},
        )
    )
    assert result.status is PreflightStatus.READY
    assert result.message == ""
    assert result.total_duration_ms == 0.0
    assert len(result.checks) == 1
    dumped = result.checks[0].model_dump()
    assert dumped.pop("duration_ms") >= 0.0
    assert dumped == GOLDEN_CONNECTIVITY
    assert _StubClient.queries == ["SELECT 1"]
    assert _StubClient.closed == 1


@pytest.mark.asyncio
async def test_default_preflight_tiers_returns_none():
    """The seam's default is 'no further tiers' — that is what keeps it inert."""
    assert (
        await _Plain().preflight_tiers(
            input=PreflightInput(), client=_StubClient(), checks=[], deadline=None
        )
        is None
    )


@pytest.mark.asyncio
async def test_returning_none_after_appending_keeps_rows():
    """A subclass that appends and forgets to return does not lose the rows."""

    class _Forgetful(_Plain):
        async def preflight_tiers(self, *, checks: list[PreflightCheck], **kw: Any):
            checks.append(PreflightCheck(name="extra", passed=True))
            # No return — the omission this test pins.

    result = await _Forgetful().preflight_check(PreflightInput(credentials=CREDS))
    assert [c.name for c in result.checks] == ["connectivity", "extra"]
    assert result.status is PreflightStatus.READY


@pytest.mark.asyncio
async def test_returning_none_with_a_failing_row_fails_closed():
    """The load-bearing one: an appended FAILING row must not read as READY.

    Forgetting the return is the exact mistake that would otherwise hand the
    gate a green verdict while an authorization check sat failed in the rows.
    """

    class _ForgotToBlock(_Plain):
        async def preflight_tiers(self, *, checks: list[PreflightCheck], **kw: Any):
            checks.append(
                PreflightCheck(
                    name="databaseSchemaCheck", passed=False, message="denied"
                )
            )
            # No return — the omission this test pins.

    result = await _ForgotToBlock().preflight_check(PreflightInput(credentials=CREDS))
    assert result.status is PreflightStatus.NOT_READY
    assert [c.name for c in result.checks] == ["connectivity", "databaseSchemaCheck"]


@pytest.mark.asyncio
async def test_default_error_path_unchanged():
    """The failure path is untouched: one row, raw driver message, NOT_READY."""
    _StubClient.fail_with = RuntimeError("could not connect to server")
    result = await _Plain().preflight_check(PreflightInput(credentials=CREDS))
    assert result.status is PreflightStatus.NOT_READY
    assert len(result.checks) == 1
    assert result.checks[0].name == "connectivity"
    assert result.checks[0].passed is False
    assert result.checks[0].message == "could not connect to server"


@pytest.mark.asyncio
async def test_malformed_credentials_still_not_ready_never_raises():
    """A bad credential payload is still NOT_READY, not an exception."""
    result = await _Plain().preflight_check(
        PreflightInput(credentials=[{"key": "host", "value": "h"}])
    )
    assert result.status is PreflightStatus.NOT_READY
    assert result.checks[0].passed is False


def test_auth_and_metadata_inputs_unaffected_by_populate_by_name():
    """The aliases that already existed keep resolving both spellings."""
    assert AuthInput.model_validate({"connector": "c"}).entrypoint_ref == "c"
    md = MetadataInput.model_validate({"connector": "c", "metadataTemplateKey": "k"})
    assert (md.entrypoint_ref, md.metadata_template_key) == ("c", "k")
    assert MetadataInput(entrypoint_ref="c").entrypoint_ref == "c"


# ---------------------------------------------------------------------------
# 3. The stub short-circuit is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_short_circuit_unchanged_and_never_connects():
    """No identity field → READY no-op, no client built, tiers never consulted."""

    class _NeverCalled(_Plain):
        async def preflight_tiers(self, **kwargs: Any) -> PreflightOutput | None:
            raise AssertionError("tiers must not run on the stub path")

    result = await _NeverCalled().preflight_check(
        PreflightInput(
            credentials=[
                {"key": "authType", "value": "basic"},
                {"key": "connectorConfigName", "value": "cfg"},
            ],
            connection_config={"include-filter": '{"dev":["public"]}'},
        )
    )
    assert result.status is PreflightStatus.READY
    assert len(result.checks) == 1
    assert result.checks[0].name == "credentialsProvided"
    assert result.checks[0].passed is True
    assert "real preflight runs at workflow execution time" in result.checks[0].message
    assert _StubClient.queries == []
    assert _StubClient.closed == 0


@pytest.mark.asyncio
async def test_stub_detection_still_reads_extra_and_custom_identity_fields():
    class _Snowflakeish(_Plain):
        connection_identity_fields = ("account_id",)

    stub = await _Snowflakeish().preflight_check(PreflightInput(credentials=[]))
    assert stub.checks[0].name == "credentialsProvided"

    _StubClient.reset()
    real = await _Snowflakeish().preflight_check(
        PreflightInput(
            credentials=[{"key": "extra.account_id", "value": "acct"}],
        )
    )
    # Not the stub path: it tried to build a client and failed on missing fields.
    assert real.status is PreflightStatus.NOT_READY
    assert real.checks[0].name == "connectivity"


# ---------------------------------------------------------------------------
# 4. connection_config absent or malformed degrades gracefully
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param({"include-filter": "x"}, {"include-filter": "x"}, id="mapping"),
        pytest.param(None, {}, id="explicit-null"),
        pytest.param(
            json.dumps({"include-filter": "x"}), {"include-filter": "x"}, id="json-str"
        ),
        pytest.param("", {}, id="empty-str"),
        pytest.param("   ", {}, id="blank-str"),
        pytest.param("not-json-at-all", {}, id="unparseable-str"),
        pytest.param("[1,2,3]", {}, id="json-str-of-scalars"),
        pytest.param('"just-a-string"', {}, id="json-str-of-str"),
        pytest.param("42", {}, id="json-str-of-int"),
        pytest.param(
            [{"key": "include-filter", "value": "x"}],
            {"include-filter": "x"},
            id="kv-pair-list",
        ),
        pytest.param(
            [{"key": "extra.region", "value": "us-east-1"}],
            {"extra": {"region": "us-east-1"}},
            id="kv-pair-list-extra-hoisted",
        ),
        pytest.param([{"no": "key"}, "junk", 7], {}, id="kv-pair-list-all-skipped"),
        pytest.param([], {}, id="empty-list"),
        pytest.param(42, {}, id="int"),
        pytest.param(True, {}, id="bool"),
    ],
)
def test_connection_config_coerces_and_never_raises(raw: Any, expected: dict):
    """Every one of these reached the route; four used to be a bare HTTP 500."""
    got = PreflightInput.model_validate(
        {"credentials": [], "connection_config": raw}
    ).connection_config
    assert isinstance(got, BaseConnectionConfig)
    assert got.as_dict() == expected


def test_connection_config_absent_is_an_empty_config_not_none():
    cfg = PreflightInput.model_validate({"credentials": []}).connection_config
    assert isinstance(cfg, BaseConnectionConfig)
    assert cfg.as_dict() == {}
    assert cfg.get("include-filter") is None
    assert cfg.get("include-filter", "fallback") == "fallback"
    assert "include-filter" not in cfg


@pytest.mark.parametrize(
    ("raw", "degraded"),
    [
        pytest.param({"include-filter": "x"}, False, id="mapping"),
        pytest.param({}, False, id="empty-mapping"),
        pytest.param(None, False, id="null-is-absent-not-degraded"),
        pytest.param("", False, id="empty-str-is-absent"),
        pytest.param("   ", False, id="blank-str-is-absent"),
        pytest.param([], False, id="empty-list-is-absent"),
        pytest.param('{"include-filter": "x"}', False, id="readable-json-str"),
        pytest.param("not-json-at-all", True, id="unparseable-str"),
        pytest.param('"scalar"', True, id="json-scalar-str"),
        pytest.param("42", True, id="json-int-str"),
        pytest.param(42, True, id="int"),
        pytest.param([{"key": "a", "value": "1"}], False, id="clean-kv-list"),
        pytest.param([{"key": "a", "value": "1"}, "junk"], True, id="partial-kv-list"),
        pytest.param([{"no": "key"}], True, id="all-skipped-kv-list"),
    ],
)
def test_wire_degraded_separates_unreadable_from_absent(raw: Any, degraded: bool):
    """An empty config is ambiguous; this is what disambiguates it.

    A blocking tier that iterates the include filter passes vacuously on an
    empty one, so "no filter sent" and "filter I could not parse" must not look
    identical to the handler — the second verified nothing.
    """
    cfg = PreflightInput.model_validate({"connection_config": raw}).connection_config
    assert cfg.wire_degraded is degraded


def test_wire_degraded_is_not_part_of_the_config_data():
    """It must not leak into the dict protocol or the dump a handler forwards."""
    cfg = PreflightInput.model_validate(
        {"connection_config": "not-json"}
    ).connection_config
    assert cfg.wire_degraded is True
    assert cfg.as_dict() == {}
    assert cfg.model_dump() == {}
    assert list(cfg.keys()) == []
    assert len(cfg) == 0
    assert "wire_degraded" not in cfg
    assert "_wire_degraded" not in cfg.as_dict()


@pytest.mark.asyncio
async def test_degraded_config_is_visible_to_a_tier():
    """The port depends on this: the tier can fail closed on an unreadable
    config instead of authorizing against an empty filter."""
    seen: list[bool] = []

    class _Strict(_Plain):
        async def preflight_tiers(
            self, *, input: PreflightInput, checks: list[PreflightCheck], **kw: Any
        ) -> PreflightOutput | None:
            seen.append(input.connection_config.wire_degraded)
            if input.connection_config.wire_degraded:
                checks.append(
                    PreflightCheck(
                        name="databaseSchemaCheck",
                        passed=False,
                        message="Could not read the connection config",
                    )
                )
                return PreflightOutput(status=PreflightStatus.NOT_READY, checks=checks)
            return None

    result = await _Strict().preflight_check(
        PreflightInput.model_validate(
            {"credentials": CREDS, "connection_config": "not-json"}
        )
    )
    assert seen == [True]
    assert result.status is PreflightStatus.NOT_READY
    assert result.checks[-1].name == "databaseSchemaCheck"


@pytest.mark.parametrize(
    "bomb",
    [
        pytest.param("[" * 60000 + "]" * 60000, id="nested-arrays"),
        pytest.param('{"a":' * 40000 + "1" + "}" * 40000, id="nested-objects"),
    ],
)
def test_deeply_nested_json_string_does_not_raise(bomb: str):
    """json.loads raises RecursionError, not ValueError, on these — and this
    string sat *inside* the request body, so starlette never parsed it."""
    cfg = PreflightInput.model_validate({"connection_config": bomb}).connection_config
    assert cfg.as_dict() == {}
    assert cfg.wire_degraded is True


def test_camelcase_wire_spelling_is_no_longer_dropped():
    """Silently dropped before: extra='ignore' plus no alias on the field."""
    cfg = PreflightInput.model_validate(
        {"credentials": [], "connectionConfig": {"include-filter": "x"}}
    ).connection_config
    assert cfg.get("include-filter") == "x"


def test_field_name_wins_when_both_spellings_are_sent():
    cfg = PreflightInput.model_validate(
        {
            "connection_config": {"which": "snake"},
            "connectionConfig": {"which": "camel"},
        }
    ).connection_config
    assert cfg.get("which") == "snake"


def test_keyword_construction_by_field_name_still_works():
    """Handler unit tests build PreflightInput directly; populate_by_name keeps
    that working now that the field declares a validation alias."""
    assert PreflightInput(connection_config={"a": 1}).connection_config.get("a") == 1
    assert (
        PreflightInput(
            connection_config=BaseConnectionConfig(a=1)  # pyright: ignore[reportCallIssue]  # extra field is the point
        ).connection_config.get("a")
        == 1
    )


def test_metadata_block_gets_the_same_treatment():
    assert PreflightInput.model_validate({"metadata": None}).metadata.as_dict() == {}
    assert (
        PreflightInput.model_validate({"metadata": '{"user-id": "u"}'}).metadata.get(
            "user-id"
        )
        == "u"
    )


def test_metadata_input_connection_config_coerces_too():
    mi = MetadataInput.model_validate({"connection_config": None})
    assert mi.connection_config.as_dict() == {}
    assert (
        MetadataInput.model_validate(
            {"connectionConfig": {"include-filter": "x"}}
        ).connection_config.get("include-filter")
        == "x"
    )


def test_dict_protocol_and_as_dict_agree():
    cfg = BaseConnectionConfig.model_validate({"a": 1, "b": None})
    assert cfg.as_dict() == {"a": 1, "b": None}
    assert cfg["a"] == 1
    assert list(cfg.keys()) == ["a", "b"]
    with pytest.raises(KeyError):
        cfg["missing"]


# ---------------------------------------------------------------------------
# Route level: the same shapes over HTTP
# ---------------------------------------------------------------------------


class _Echo(SQLHandler):
    client_class = _StubClient
    filter_metadata_sql = "SELECT catalog_name, schema_name FROM stub"
    received: ClassVar[list[dict[str, Any]]] = []

    async def preflight_tiers(
        self,
        *,
        input: PreflightInput,
        client: BaseSQLClient,
        checks: list[PreflightCheck],
        deadline: float | None,
    ) -> PreflightOutput | None:
        type(self).received.append(input.connection_config.as_dict())
        checks.append(
            PreflightCheck(
                name="databaseSchemaCheck", passed=True, message="Authorized"
            )
        )
        return PreflightOutput(status=PreflightStatus.READY, checks=checks)


@pytest.fixture
def client() -> TestClient:
    _Echo.received = []
    return TestClient(
        build_asgi_app(_Echo(), app_name="stubapp"), raise_server_exceptions=False
    )


@pytest.mark.parametrize(
    ("body_key", "value", "expected"),
    [
        pytest.param(
            "connection_config",
            {"include-filter": "x"},
            {"include-filter": "x"},
            id="snake",
        ),
        pytest.param(
            "connectionConfig",
            {"include-filter": "x"},
            {"include-filter": "x"},
            id="camel",
        ),
        pytest.param(
            "connection_config",
            '{"include-filter": "x"}',
            {"include-filter": "x"},
            id="json-str",
        ),
        pytest.param("connection_config", None, {}, id="null"),
        pytest.param("connection_config", "garbage", {}, id="garbage"),
        pytest.param(
            "connection_config",
            [{"key": "include-filter", "value": "x"}],
            {"include-filter": "x"},
            id="kv-list",
        ),
    ],
)
def test_check_route_never_500s_on_a_config_shape(
    client: TestClient, body_key: str, value: Any, expected: dict
):
    """PreflightInput.model_validate runs outside the route's try, so a
    ValidationError there was an unhandled 500 with the handler never invoked —
    taking down every check on the request, including the ones that need no
    config at all."""
    r = client.post(
        "/workflows/v1/check",
        json={"credentials": CREDS, body_key: value},
    )
    assert r.status_code == 200, r.text
    assert _Echo.received == [expected]
    assert r.json()["data"]["databaseSchemaCheck"]["success"] is True


def test_check_route_mirrors_metadata_into_connection_config(client: TestClient):
    r = client.post(
        "/workflows/v1/check",
        json={"credentials": CREDS, "metadata": {"include-filter": "x"}},
    )
    assert r.status_code == 200, r.text
    assert _Echo.received == [{"include-filter": "x"}]


def test_check_route_camel_config_still_mirrors_into_metadata():
    """The mirror inspects the raw body, so it has to see the camel spelling."""
    seen: list[dict[str, Any]] = []

    class _Md(SQLHandler):
        client_class = _StubClient
        filter_metadata_sql = "SELECT 1"

        async def preflight_tiers(self, *, input: PreflightInput, **kw: Any):
            seen.append(input.metadata.as_dict())
            return PreflightOutput(
                status=PreflightStatus.READY,
                checks=[PreflightCheck(name="connectivity", passed=True)],
            )

    c = TestClient(build_asgi_app(_Md(), app_name="md"), raise_server_exceptions=False)
    r = c.post(
        "/workflows/v1/check",
        json={"credentials": CREDS, "connectionConfig": {"include-filter": "x"}},
    )
    assert r.status_code == 200, r.text
    assert seen == [{"include-filter": "x"}]


def test_stub_check_over_http_is_still_ready(client: TestClient):
    r = client.post(
        "/workflows/v1/check",
        json={"credentials": [{"key": "authType", "value": "basic"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["credentialsProvided"]["success"] is True
    assert _Echo.received == []


def test_bad_entrypoint_is_still_a_400_not_a_config_problem(client: TestClient):
    r = client.post(
        "/workflows/v1/check",
        json={
            "credentials": CREDS,
            "entrypoint": "../etc/passwd",
            "connection_config": None,
        },
    )
    assert r.status_code == 400


def test_no_config_content_is_ever_logged(caplog, client: TestClient):
    """A config block can carry customer-identifying form state; the warnings
    must name only the type and the length."""
    secretish = "tenant-acme-prod-not-json"
    with caplog.at_level("WARNING"):
        r = client.post(
            "/workflows/v1/check",
            json={"credentials": CREDS, "connection_config": secretish},
        )
    assert r.status_code == 200
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "unreadable JSON string config" in text
    assert secretish not in text
    assert str(len(secretish)) in text


def test_metadata_route_also_survives_a_null_config():
    class _M(SQLHandler):
        client_class = _StubClient
        filter_metadata_sql = "SELECT catalog_name, schema_name FROM stub"

    c = TestClient(build_asgi_app(_M(), app_name="m"), raise_server_exceptions=False)
    r = c.post(
        "/workflows/v1/metadata",
        json={"credentials": CREDS, "connection_config": None},
    )
    assert r.status_code == 200, r.text


def test_handler_subclassing_handler_directly_is_untouched():
    """Governance-style handlers do not inherit SQLHandler; nothing here reaches
    them, and their input.metadata read keeps working."""
    from server_sdk.handler.base import Handler

    seen: list[dict[str, Any]] = []

    class _Gov(Handler):
        async def test_auth(self, input: AuthInput) -> AuthOutput:
            return AuthOutput(status=AuthStatus.SUCCESS)

        async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
            seen.append(input.metadata.model_dump() if input.metadata else {})
            return PreflightOutput(
                status=PreflightStatus.READY,
                checks=[PreflightCheck(name="userDisabledCheck", passed=True)],
            )

        async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
            return SqlMetadataOutput(objects=[])

    c = TestClient(
        build_asgi_app(_Gov(), app_name="gov"), raise_server_exceptions=False
    )
    r = c.post(
        "/workflows/v1/check",
        json={"entrypoint": "delete-user", "metadata": {"user-id": "u1"}},
    )
    assert r.status_code == 200, r.text
    assert seen == [{"user-id": "u1"}]
    assert not hasattr(_Gov, "preflight_tiers")


def test_stub_client_credential_validation_is_real():
    """Guards the suite itself: the stub keeps the base credential check, so a
    NOT_READY above is a real resolution failure, not a stubbed-out one."""
    c = _StubClient()
    c.credentials = {"host": "h"}
    with pytest.raises(InvalidInputError):
        c.get_sqlalchemy_connection_string()
