"""Unit tests for handler contracts."""

import pytest
from pydantic import ConfigDict, Field, ValidationError

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


class TestPreflightCheck:
    def test_defaults(self):
        check = PreflightCheck(name="connectivity")
        assert check.name == "connectivity"
        assert check.passed is False
        assert check.message == ""
        assert check.duration_ms == 0.0

    def test_passed(self):
        check = PreflightCheck(name="connectivity", passed=True, duration_ms=50.0)
        assert check.passed is True
        assert check.duration_ms == 50.0

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            PreflightCheck(name="")


class TestPreflightOutput:
    def test_required_status(self):
        out = PreflightOutput(status=PreflightStatus.READY)
        assert out.status == PreflightStatus.READY
        assert out.checks == []

    def test_with_checks(self):
        checks = [
            PreflightCheck(name="conn", passed=True),
            PreflightCheck(name="perms", passed=False, message="No read access"),
        ]
        out = PreflightOutput(status=PreflightStatus.PARTIAL, checks=checks)
        assert len(out.checks) == 2


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

    def test_subclass_with_aliased_fields(self):
        """Migration path: subclass declares typed fields with hyphenated aliases."""

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: str = Field(default="{}", alias="include-filter")
            exclude_filter: str = Field(default="{}", alias="exclude-filter")
            page_size: int = Field(default=500, alias="page-size")

        cfg = MyConnectionConfig.model_validate(
            {
                "include-filter": '{"db": ["public"]}',
                "exclude-filter": "{}",
                "page-size": 1000,
            }
        )
        assert cfg.include_filter == '{"db": ["public"]}'
        assert cfg.exclude_filter == "{}"
        assert cfg.page_size == 1000

    def test_subclass_dict_returns_declared_and_extras(self):
        """dict(subclass_instance) covers both declared fields and inherited extras."""

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: str = Field(default="{}", alias="include-filter")

        cfg = MyConnectionConfig.model_validate(
            {"include-filter": "{}", "unexpected": "value"}
        )
        as_dict = dict(cfg)
        assert as_dict["include_filter"] == "{}"
        assert as_dict["unexpected"] == "value"

    def test_subclass_model_dump_by_alias_preserves_wire_keys(self):
        """model_dump(by_alias=True) emits the original hyphenated wire keys."""

        class MyConnectionConfig(BaseConnectionConfig):
            include_filter: str = Field(default="{}", alias="include-filter")
            page_size: int = Field(default=500, alias="page-size")

        cfg = MyConnectionConfig(**{"include-filter": '{"a": ["b"]}', "page-size": 100})
        dumped = cfg.model_dump(by_alias=True)
        assert dumped == {
            "include-filter": '{"a": ["b"]}',
            "page-size": 100,
        }

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

    def test_subclass_with_aliased_fields(self):
        class MyMetadataConfig(BaseMetadataConfig):
            extraction_method: str = Field(default="api", alias="extraction-method")
            include_filter: str = Field(default="{}", alias="include-filter")

        cfg = MyMetadataConfig.model_validate(
            {"extraction-method": "core", "include-filter": '{"x": ["y"]}'}
        )
        assert cfg.extraction_method == "core"
        assert cfg.include_filter == '{"x": ["y"]}'


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


class TestMetadataInputFieldTypes:
    def test_dict_input_coerced_to_typed_base(self):
        inp = MetadataInput.model_validate(
            {"credentials": [], "connection_config": {"host": "db"}}
        )
        assert isinstance(inp.connection_config, BaseConnectionConfig)
        assert inp.connection_config.model_extra == {"host": "db"}
