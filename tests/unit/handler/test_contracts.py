"""Unit tests for handler contracts."""

import pytest
from pydantic import ConfigDict, Field, ValidationError

from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError, PreconditionError
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    BaseConnectionConfig,
    BaseMetadataConfig,
    HandlerCredential,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)


class TestHandlerCredential:
    def test_frozen(self):
        cred = HandlerCredential(key="api_key", value="secret")
        with pytest.raises((AttributeError, TypeError, ValidationError)):
            cred.key = "other"  # type: ignore[misc]

    def test_fields(self):
        cred = HandlerCredential(key="token", value="abc123")
        assert cred.key == "token"
        assert cred.value == "abc123"


class TestAuthStatus:
    def test_values(self):
        assert AuthStatus.SUCCESS == "success"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.EXPIRED == "expired"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


class TestAuthInput:
    def test_defaults(self):
        inp = AuthInput()
        assert inp.credentials == []
        assert inp.connection_id == ""
        assert inp.timeout_seconds == 30

    def test_with_credentials(self):
        creds = [HandlerCredential(key="k", value="v")]
        inp = AuthInput(credentials=creds, connection_id="conn-1", timeout_seconds=60)
        assert len(inp.credentials) == 1
        assert inp.timeout_seconds == 60


class TestAuthOutput:
    def test_required_status(self):
        out = AuthOutput(status=AuthStatus.SUCCESS)
        assert out.status == AuthStatus.SUCCESS
        assert out.message == ""
        assert out.identities == []
        assert out.scopes == []
        assert out.expires_at == ""

    def test_failed_status(self):
        out = AuthOutput(status=AuthStatus.FAILED, message="Bad credentials")
        assert out.status == AuthStatus.FAILED
        assert out.message == "Bad credentials"


class TestPreflightStatus:
    def test_values(self):
        assert PreflightStatus.READY == "ready"
        assert PreflightStatus.NOT_READY == "not_ready"
        assert PreflightStatus.PARTIAL == "partial"

    def test_advisory_only_no_gate_values(self):
        # status is advisory; the gate decision is PreflightOutput.should_block,
        # so there are no success/failed status values to overlap the legacy set.
        assert {s.value for s in PreflightStatus} == {"ready", "not_ready", "partial"}


class TestPreflightCheck:
    def test_defaults(self):
        check = PreflightCheck(name="connectivity", passed=True)
        assert check.name == "connectivity"
        assert check.message == ""
        assert check.duration_ms == 0.0
        assert check.error is None
        assert check.blocking is False

    def test_passed_must_be_stated(self):
        # passed is the observed outcome — no silent default in either direction.
        with pytest.raises(ValidationError):
            PreflightCheck(name="connectivity")

    def test_blocking_is_a_real_field_but_required_is_ignored(self):
        # ``blocking`` is the per-check gate flag now — a real, declared field.
        # The older pre-release ``required`` is gone; extra="ignore" drops it.
        check = PreflightCheck(
            name="auth", passed=False, **{"blocking": True, "required": True}
        )
        assert check.blocking is True
        assert not hasattr(check, "required")

    def test_passed(self):
        check = PreflightCheck(
            name="connectivity",
            passed=True,
            duration_ms=50.0,
        )
        assert check.passed is True
        assert check.duration_ms == 50.0

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            PreflightCheck(name="")


class TestPreflightCheckFromError:
    def test_app_error_keeps_full_classification(self):
        err = AuthError(
            message="Login was rejected by the source",
            suggested_action="Rotate the credential in the connection settings",
        )
        check = PreflightCheck.from_error("auth", err)
        assert check.passed is False
        assert check.message == "Login was rejected by the source"
        assert check.category is FailureCategory.AUTH
        assert check.suggested_action == (
            "Rotate the credential in the connection settings"
        )
        assert check.error is not None
        assert check.error.code == AuthError.code
        assert check.error.category is FailureCategory.AUTH

    def test_app_subclass_code_is_preserved(self):
        class WarehouseSuspendedError(PreconditionError):
            code = "PRECONDITION_WAREHOUSE_SUSPENDED"

        check = PreflightCheck.from_error(
            "warehouse", WarehouseSuspendedError(message="Warehouse is suspended")
        )
        assert check.error is not None
        assert check.error.code == "PRECONDITION_WAREHOUSE_SUSPENDED"
        assert check.category is FailureCategory.PRECONDITION

    def test_untyped_exception_is_unclassified_and_sanitized(self):
        check = PreflightCheck.from_error(
            "connectivity", ValueError("password=hunter2 host=prod.internal")
        )
        assert check.passed is False
        assert check.category is None
        assert check.error is None
        assert "hunter2" not in check.message
        assert "prod.internal" not in check.message
        assert "ValueError" in check.message

    def test_explicit_suggested_action_wins(self):
        check = PreflightCheck.from_error(
            "version",
            AuthError(message="nope", suggested_action="from the error"),
            suggested_action="from the caller",
        )
        assert check.suggested_action == "from the caller"

    def test_blocking_defaults_true(self):
        # The blessed failure path gates by default — both the typed and the
        # untyped arm produce a blocking check unless the caller opts out.
        assert PreflightCheck.from_error("auth", AuthError(message="nope")).blocking
        assert PreflightCheck.from_error("conn", ValueError("boom")).blocking

    def test_blocking_opt_out_is_respected(self):
        typed = PreflightCheck.from_error(
            "auth", AuthError(message="nope"), blocking=False
        )
        untyped = PreflightCheck.from_error("conn", ValueError("boom"), blocking=False)
        assert typed.blocking is False
        assert untyped.blocking is False


class TestPreflightOutput:
    @pytest.mark.parametrize(
        ("checks", "expected_block"),
        [
            # advisory failure never blocks
            ([PreflightCheck(name="c", passed=False, blocking=False)], False),
            # a failing blocking check blocks
            ([PreflightCheck(name="c", passed=False, blocking=True)], True),
            # a passing blocking check does not block (it only declares severity)
            ([PreflightCheck(name="c", passed=True, blocking=True)], False),
            # no checks -> nothing can gate
            ([], False),
        ],
    )
    def test_should_block_folds_over_blocking_flags(self, checks, expected_block):
        assert PreflightOutput(checks=checks).should_block is expected_block

    def test_status_is_ready_unless_should_block(self):
        # A passing blocking check plus a failing advisory check: nothing the app
        # marked blocking failed, so the run is READY (never PARTIAL).
        ready = PreflightOutput(
            checks=[
                PreflightCheck(name="auth", passed=True, blocking=True),
                PreflightCheck(name="conn", passed=False, blocking=False),
            ]
        )
        assert ready.should_block is False
        assert ready.status is PreflightStatus.READY
        assert ready.status is not PreflightStatus.PARTIAL

        blocked = PreflightOutput(
            checks=[PreflightCheck(name="auth", passed=False, blocking=True)]
        )
        assert blocked.status is PreflightStatus.NOT_READY

    def test_stray_verdict_kwargs_are_silently_ignored(self):
        # Clean break: the verdict is derived from the per-check blocking flags.
        # Older callers' passed=/status=/required= kwargs are dropped
        # (extra="ignore"), which also swallows the status key the computed_field
        # re-injects on Temporal round-trips.
        out = PreflightOutput(
            passed=False,
            status=PreflightStatus.NOT_READY,
            required=True,
            checks=[PreflightCheck(name="a", passed=True, blocking=True)],
        )
        assert not hasattr(out, "passed")
        assert not hasattr(out, "required")
        assert out.should_block is False
        assert out.status is PreflightStatus.READY

    def test_round_trip_keeps_status_key_and_drops_passed(self):
        out = PreflightOutput(
            checks=[PreflightCheck(name="auth", passed=False, blocking=True)]
        )
        dumped = out.model_dump(mode="json")
        assert dumped["status"] == "not_ready"
        assert "passed" not in dumped
        assert dumped["checks"][0]["blocking"] is True
        restored = PreflightOutput.model_validate(dumped)
        assert restored.checks[0].blocking is True
        assert restored.status is PreflightStatus.NOT_READY
        assert restored.should_block is True


class TestMetadataOutput:
    """Tests for the MetadataOutput hierarchy."""

    def test_base_class_is_empty(self):
        out = MetadataOutput()
        assert isinstance(out, MetadataOutput)

    def test_sql_output_defaults(self):
        out = SqlMetadataOutput()
        assert isinstance(out, MetadataOutput)
        assert out.objects == []

    def test_sql_output_with_objects(self):
        out = SqlMetadataOutput(
            objects=[
                SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"),
                SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"),
            ]
        )
        assert len(out.objects) == 2
        assert out.objects[0].TABLE_CATALOG == "DEFAULT"
        assert out.objects[0].TABLE_SCHEMA == "FINANCE"

    def test_sql_output_model_dump(self):
        obj = SqlMetadataObject(TABLE_CATALOG="DB", TABLE_SCHEMA="SCH")
        assert obj.model_dump() == {"TABLE_CATALOG": "DB", "TABLE_SCHEMA": "SCH"}

    def test_api_output_defaults(self):
        out = ApiMetadataOutput()
        assert isinstance(out, MetadataOutput)
        assert out.objects == []

    def test_api_output_with_flat_objects(self):
        out = ApiMetadataOutput(
            objects=[
                ApiMetadataObject(value="t1", title="Tag 1", node_type="tag"),
            ]
        )
        assert len(out.objects) == 1
        assert out.objects[0].value == "t1"
        assert out.objects[0].title == "Tag 1"
        assert out.objects[0].node_type == "tag"
        assert out.objects[0].children == []

    def test_api_output_nested_children(self):
        child = ApiMetadataObject(value="c1", title="Child 1")
        parent = ApiMetadataObject(
            value="p1",
            title="Parent",
            node_type="project",
            children=[child],
        )
        out = ApiMetadataOutput(objects=[parent])
        assert len(out.objects[0].children) == 1
        assert out.objects[0].children[0].value == "c1"

    def test_api_output_model_dump_nested(self):
        obj = ApiMetadataObject(
            value="p1",
            title="Parent",
            node_type="folder",
            children=[ApiMetadataObject(value="c1", title="Child")],
        )
        dumped = obj.model_dump()
        assert dumped == {
            "value": "p1",
            "title": "Parent",
            "node_type": "folder",
            "children": [
                {"value": "c1", "title": "Child", "node_type": "", "children": []},
            ],
        }

    def test_isinstance_both_subtypes(self):
        sql = SqlMetadataOutput()
        api = ApiMetadataOutput()
        assert isinstance(sql, MetadataOutput)
        assert isinstance(api, MetadataOutput)

    def test_api_output_rejects_invalid_child_type(self):
        """Passing a non-ApiMetadataObject item raises ValidationError."""
        with pytest.raises(ValidationError):
            ApiMetadataObject(
                value="p",
                title="P",
                children=["not-an-object"],  # type: ignore[list-item]
            )


class TestBaseConnectionConfig:
    """Tests for the BaseConnectionConfig public extension point."""

    def test_default_construction(self):
        cfg = BaseConnectionConfig()
        assert cfg.model_extra == {}
        assert cfg.model_dump() == {}

    def test_dict_input_lands_in_model_extra(self):
        """Raw dict ingress: extras preserved on model_extra (extra='allow')."""
        cfg = BaseConnectionConfig.model_validate(
            {"host": "db.local", "port": 5432, "database": "prod"}
        )
        assert cfg.model_extra == {
            "host": "db.local",
            "port": 5432,
            "database": "prod",
        }

    def test_dict_round_trip(self):
        """dict(model) yields all keys; model_dump() round-trips through validate."""
        original = {"host": "db", "port": 5432}
        cfg = BaseConnectionConfig.model_validate(original)
        assert dict(cfg) == original
        round_tripped = BaseConnectionConfig.model_validate(cfg.model_dump())
        assert round_tripped.model_extra == original

    def test_subclass_with_real_typed_fields(self):
        """Migration path: subclass declares typed fields with hyphenated aliases.

        Filters use real ``dict[str, list[str]]`` types — no stringified JSON.
        """

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )
            exclude_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="exclude-filter"
            )
            page_size: int = Field(default=500, alias="page-size")

        cfg = MyConnectionConfig.model_validate(
            {
                "include-filter": {"db": ["public", "analytics"]},
                "exclude-filter": {},
                "page-size": 1000,
            }
        )
        assert cfg.include_filter == {"db": ["public", "analytics"]}
        assert cfg.exclude_filter == {}
        assert cfg.page_size == 1000

    def test_subclass_dict_returns_declared_and_extras(self):
        """dict(subclass_instance) covers both declared fields and inherited extras."""

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )

        cfg = MyConnectionConfig.model_validate(
            {"include-filter": {"db": ["s"]}, "unexpected": "value"}
        )
        as_dict = dict(cfg)
        assert as_dict["include_filter"] == {"db": ["s"]}
        assert as_dict["unexpected"] == "value"

    def test_subclass_model_dump_by_alias_preserves_wire_keys(self):
        """model_dump(by_alias=True) emits the original hyphenated wire keys."""

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )
            page_size: int = Field(default=500, alias="page-size")

        cfg = MyConnectionConfig(**{"include-filter": {"a": ["b"]}, "page-size": 100})
        dumped = cfg.model_dump(by_alias=True)
        assert dumped == {
            "include-filter": {"a": ["b"]},
            "page-size": 100,
        }

    def test_dict_style_access_on_extras(self):
        """cfg["k"], cfg.get(...), and "k" in cfg work for dict-shaped extras."""
        cfg = BaseConnectionConfig.model_validate({"host": "db", "port": 5432})

        # __getitem__
        assert cfg["host"] == "db"
        assert cfg["port"] == 5432
        with pytest.raises(KeyError):
            _ = cfg["missing"]

        # get with default
        assert cfg.get("host") == "db"
        assert cfg.get("missing") is None
        assert cfg.get("missing", "fallback") == "fallback"

        # __contains__
        assert "host" in cfg
        assert "missing" not in cfg

    def test_dict_style_access_on_declared_fields_and_aliases(self):
        """Lookups try field name, then alias, then extras."""

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )

        cfg = MyConnectionConfig.model_validate(
            {"include-filter": {"db": ["s"]}, "extra_key": "x"}
        )

        # By Python field name
        assert cfg["include_filter"] == {"db": ["s"]}
        # By alias
        assert cfg["include-filter"] == {"db": ["s"]}
        # Extra
        assert cfg["extra_key"] == "x"
        # Non-existent
        assert cfg.get("nope", "default") == "default"
        assert "include_filter" in cfg
        assert "include-filter" in cfg
        assert "extra_key" in cfg
        assert "nope" not in cfg

    def test_contains_rejects_non_string_keys(self):
        """Non-string keys are not in the model — no TypeError."""
        cfg = BaseConnectionConfig.model_validate({"host": "db"})
        assert 42 not in cfg  # type: ignore[operator]
        assert None not in cfg  # type: ignore[operator]

    def test_mapping_protocol_on_extras(self):
        """keys/values/items/len work over extras."""
        cfg = BaseConnectionConfig.model_validate({"host": "db", "port": 5432})

        assert sorted(cfg.keys()) == ["host", "port"]
        assert sorted(cfg.values(), key=str) == [5432, "db"]
        assert sorted(cfg.items()) == [("host", "db"), ("port", 5432)]
        assert len(cfg) == 2

    def test_mapping_protocol_with_declared_and_extras(self):
        """Iteration covers declared fields and extras together."""

        class MyConnectionConfig(BaseConnectionConfig):
            page_size: int = Field(default=500, alias="page-size")

        cfg = MyConnectionConfig.model_validate({"page-size": 100, "extra_key": "x"})
        assert dict(cfg.items()) == {"page_size": 100, "extra_key": "x"}
        assert len(cfg) == 2
        assert "page_size" in cfg.keys()
        assert "extra_key" in cfg.keys()

    def test_for_loop_iteration(self):
        """``for k, v in cfg`` yields all keys/values."""
        cfg = BaseConnectionConfig.model_validate({"host": "db", "port": 5432})
        result = dict(cfg)  # uses __iter__
        assert result == {"host": "db", "port": 5432}

    def test_subclass_can_forbid_extras(self):
        """Apps subclass and override extra='forbid' for strict validation."""

        class StrictConfig(BaseConnectionConfig):
            model_config = ConfigDict(
                extra="forbid", populate_by_name=True, frozen=True
            )

            host: str = Field(default="")

        # Declared key passes
        cfg = StrictConfig(host="db.local")
        assert cfg.host == "db.local"

        # Undeclared key rejected at parse time
        with pytest.raises(ValidationError):
            StrictConfig.model_validate({"host": "db", "random_typo": "x"})


class TestBaseMetadataConfig:
    """Tests for the BaseMetadataConfig public extension point."""

    def test_default_construction(self):
        cfg = BaseMetadataConfig()
        assert cfg.model_extra == {}
        assert cfg.model_dump() == {}

    def test_dict_input_lands_in_model_extra(self):
        cfg = BaseMetadataConfig.model_validate(
            {"include-filter": "{}", "extraction-method": "api"}
        )
        assert cfg.model_extra == {
            "include-filter": "{}",
            "extraction-method": "api",
        }

    def test_subclass_with_real_typed_fields(self):
        class MyMetadataConfig(BaseMetadataConfig):
            extraction_method: str = Field(default="api", alias="extraction-method")
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )
            enable_tag_sync: bool = Field(default=False, alias="enable-tag-sync")

        cfg = MyMetadataConfig.model_validate(
            {
                "extraction-method": "core",
                "include-filter": {"x": ["y"]},
                "enable-tag-sync": True,
            }
        )
        assert cfg.extraction_method == "core"
        assert cfg.include_filter == {"x": ["y"]}
        assert cfg.enable_tag_sync is True

    def test_dict_style_access(self):
        """Same dict-compat behavior as BaseConnectionConfig."""
        cfg = BaseMetadataConfig.model_validate(
            {"extraction-method": "api", "page-size": 500}
        )
        assert cfg["extraction-method"] == "api"
        assert cfg.get("page-size") == 500
        assert cfg.get("missing", "default") == "default"
        assert "extraction-method" in cfg
        assert "missing" not in cfg


class TestPreflightInputFieldTypes:
    """Verify PreflightInput coerces dict inputs into the typed bases."""

    def test_dict_inputs_coerced_to_typed_bases(self):
        inp = PreflightInput.model_validate(
            {
                "credentials": [],
                "connection_config": {"host": "db", "port": 5432},
                "metadata": {"extraction-method": "api"},
            }
        )
        assert isinstance(inp.connection_config, BaseConnectionConfig)
        assert isinstance(inp.metadata, BaseMetadataConfig)
        assert inp.connection_config.model_extra == {"host": "db", "port": 5432}
        assert inp.metadata.model_extra == {"extraction-method": "api"}

    def test_defaults_are_empty_typed_instances(self):
        inp = PreflightInput()
        assert isinstance(inp.connection_config, BaseConnectionConfig)
        assert isinstance(inp.metadata, BaseMetadataConfig)
        assert inp.connection_config.model_extra == {}
        assert inp.metadata.model_extra == {}

    def test_unknown_runtime_keys_are_dropped(self):
        inp = PreflightInput.model_validate({"credentials": [], "tenant": "default"})
        assert inp.model_extra is None
        assert not hasattr(inp, "tenant")


class TestMetadataInputFieldTypes:
    def test_dict_input_coerced_to_typed_base(self):
        inp = MetadataInput.model_validate(
            {"credentials": [], "connection_config": {"host": "db"}}
        )
        assert isinstance(inp.connection_config, BaseConnectionConfig)
        assert inp.connection_config.model_extra == {"host": "db"}

    def test_metadata_template_key_accepts_canonical_field(self):
        inp = MetadataInput.model_validate(
            {"credentials": [], "metadata_template_key": "feedbacks"}
        )
        assert inp.metadata_template_key == "feedbacks"

    def test_metadata_template_key_accepts_wire_aliases(self):
        # The orchestrator sends the routing key as ``metadataTemplateKey`` (with
        # a ``type`` mirror); both populate ``metadata_template_key`` via the
        # field's validation alias rather than being punned into object_filter.
        for key in ("metadataTemplateKey", "type"):
            inp = MetadataInput.model_validate({"credentials": [], key: "feedbacks"})
            assert inp.metadata_template_key == "feedbacks"


class TestEntrypointRefAlias:
    """`entrypoint_ref` is a NEW field added by this PR. It accepts the wire key
    `connector` (validation_alias) and serializes back to `connector`
    (serialization_alias), so Heracles' existing payload shape works without an
    orchestrator-side change. It is informational only — per-entrypoint routing
    uses the separate `entrypoint` field, not this one.
    """

    @pytest.mark.parametrize("cls", [AuthInput, PreflightInput, MetadataInput])
    def test_wire_connector_key_populates_entrypoint_ref(self, cls):
        inp = cls.model_validate({"credentials": [], "connector": "bundle-ep-a"})
        assert inp.entrypoint_ref == "bundle-ep-a"

    @pytest.mark.parametrize("cls", [AuthInput, PreflightInput, MetadataInput])
    def test_canonical_entrypoint_ref_key_accepted(self, cls):
        inp = cls.model_validate({"credentials": [], "entrypoint_ref": "bundle-ep-a"})
        assert inp.entrypoint_ref == "bundle-ep-a"

    def test_serializes_back_to_connector_wire_key(self):
        inp = AuthInput.model_validate({"credentials": [], "connector": "bundle-ep-a"})
        dumped = inp.model_dump(by_alias=True)
        assert dumped["connector"] == "bundle-ep-a"
        assert "entrypoint_ref" not in dumped

    @pytest.mark.parametrize("cls", [AuthInput, PreflightInput, MetadataInput])
    def test_explicit_entrypoint_field_is_independent(self, cls):
        """The authoritative `entrypoint` (bare name) is a distinct field from
        the legacy `entrypoint_ref` (bundle-prefixed connector)."""
        inp = cls.model_validate(
            {
                "credentials": [],
                "entrypoint": "asset-export-advanced",
                "connector": "csa-uber-asset-export-advanced",
            }
        )
        assert inp.entrypoint == "asset-export-advanced"
        assert inp.entrypoint_ref == "csa-uber-asset-export-advanced"
