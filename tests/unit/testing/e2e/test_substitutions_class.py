"""Tests for the substitutions_class hook (item 3).

A connector that declares extra manifest mustache keys only points
``substitutions_class`` at its own MustacheSubstitutions subclass (whose typed
fields carry the connector's config defaults); the base fills the universal
fields and the subclass's extra fields fall to their declared defaults — so no
``_mustache_substitutions()`` override is needed just to add params.
"""

from __future__ import annotations

from pydantic import Field

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e import BaseE2ETest
from application_sdk.testing.e2e.payload import ConnectionSpec
from application_sdk.testing.e2e.substitutions import (
    MustacheSubstitutions,
    SQLMustacheSubstitutions,
)

_SPEC = ConnectionSpec(
    name="e2e-ci-x-1",
    qualified_name="default/x/e2e-ci-1",
    connector_name="x",
    source_logo="https://assets.atlan.com/assets/x.png",
)


class _ConnSubs(MustacheSubstitutions):
    api_version: str = Field(default="v52.0", alias="{{api-version}}")
    fetch_reports: str = Field(default="true", alias="{{fetch-reports}}")


class _SQLConnSubs(SQLMustacheSubstitutions):
    incremental: bool = Field(default=False, alias="{{incremental-enabled}}")


class _WithSubsClass(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    substitutions_class = _ConnSubs

    def connection_spec(self) -> ConnectionSpec:
        return _SPEC


class _DefaultSubsClass(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"

    def connection_spec(self) -> ConnectionSpec:
        return _SPEC


def test_default_class_is_universal() -> None:
    assert _DefaultSubsClass.substitutions_class is MustacheSubstitutions
    subs = _DefaultSubsClass()._mustache_substitutions()
    assert isinstance(subs, MustacheSubstitutions)


def test_hook_returns_declared_subclass_instance() -> None:
    subs = _WithSubsClass()._mustache_substitutions()
    assert isinstance(subs, _ConnSubs)


def test_subclass_extra_fields_default_and_serialize() -> None:
    subs = _WithSubsClass()._mustache_substitutions()
    dumped = subs.model_dump(by_alias=True, mode="json")
    assert dumped["{{api-version}}"] == "v52.0"
    assert dumped["{{fetch-reports}}"] == "true"
    # Universal field still filled by the base.
    assert dumped["{{connection}}"]["attributes"]["qualifiedName"] == (
        "default/x/e2e-ci-1"
    )


def test_typed_bool_serializes_as_json_bool() -> None:
    # A typed bool field dumps as a real JSON false, not the "{{...}}" literal —
    # the point of the typed-substitution hook.
    conn_ref = ConnectionRef.model_validate(
        {"typeName": "Connection", "attributes": _SPEC.attributes()}
    )
    dumped = _SQLConnSubs.model_validate({"connection": conn_ref}).model_dump(
        by_alias=True, mode="json"
    )
    assert dumped["{{incremental-enabled}}"] is False
