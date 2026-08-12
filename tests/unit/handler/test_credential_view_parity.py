"""Parity between the two views of a single credential.

One resolved credential is read through two different lenses:

* the **runtime view** — `AsyncBaseSQLClient.get_sqlalchemy_connection_string`,
  which resolves each required param top-level-first, then from `extra`
  (`credentials.get(p) or extra.get(p)`);
* the **gate view** — `flatten_credentials_to_pairs`, the flat
  `[{key, value}]` list that is the *only* credential a handler sees on the
  HTTP preflight path and inside the injected preflight gate.

The invariant: anything the runtime lens can resolve, the gate lens must
expose. Break it and the gate fails a connectivity check against params the
extraction path would have found — a false block, reported as a real one.

These tests are deliberately written against `DB_CONFIG.required` rather than
against a fixed key list, so they hold for any connector layout.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient
from application_sdk.handler.contracts import flatten_credentials_to_pairs


class _TopLevelParamClient(AsyncBaseSQLClient):
    """Every required param lives at the top level of the credential.

    This is the common connector layout, and it is *immune* to a dropped
    `extra` — which is exactly why the defect below survived in production
    while the widely-exercised connectors stayed green.
    """

    DB_CONFIG = DatabaseConfig(
        template="fake://{username}:{password}@{host}:{port}",
        required=["username", "password", "host", "port"],
    )


class _ExtraParamClient(AsyncBaseSQLClient):
    """Required params straddle the top level and `extra`.

    `sid` has no top-level home in the credential shape, so it can only be
    resolved out of `extra`. Drop `extra` and the DSN cannot be built at all.
    """

    DB_CONFIG = DatabaseConfig(
        template="fake://{username}:{password}@{host}:{port}/{sid}",
        required=["username", "password", "host", "port", "sid"],
    )


def _gate_view(creds: dict[str, Any]) -> set[str]:
    """The set of credential keys a gate-side handler can actually read."""
    return {pair["key"] for pair in flatten_credentials_to_pairs(creds)}


def _assert_views_agree(
    client_cls: type[AsyncBaseSQLClient], creds: dict[str, Any]
) -> None:
    required = client_cls.DB_CONFIG.required if client_cls.DB_CONFIG else []

    # Runtime lens: raises MissingSqlParamError if any required param is
    # unresolvable, so reaching the assertions proves the credential is
    # complete as far as the extraction path is concerned.
    client_cls(credentials=creds).get_sqlalchemy_connection_string()

    keys = _gate_view(creds)
    for param in required:
        assert param in keys or f"extra.{param}" in keys, (
            f"runtime client resolves {param!r} from this credential but the "
            f"gate view does not expose it (as {param!r} or 'extra.{param}'); "
            f"gate view = {sorted(keys)}"
        )


@pytest.fixture(params=["dict", "json_string"])
def extra_shape(request: pytest.FixtureRequest):
    """Both legal storage shapes of `extra`, applied to the same object."""

    def apply(extra: dict[str, Any]) -> Any:
        return extra if request.param == "dict" else json.dumps(extra)

    return apply


class TestRuntimeAndGateViewsAgree:
    def test_top_level_layout(self, extra_shape):
        """The immune layout — passes with or without `extra` surviving."""
        _assert_views_agree(
            _TopLevelParamClient,
            {
                "username": "u",
                "password": "p",
                "host": "h",
                "port": 1521,
                "extra": extra_shape({"database": "db"}),
            },
        )

    def test_params_inside_extra(self, extra_shape):
        """The layout that exposed the defect: `sid` only exists in `extra`."""
        _assert_views_agree(
            _ExtraParamClient,
            {
                "username": "u",
                "password": "p",
                "extra": extra_shape({"host": "h", "port": 1521, "sid": "DB1"}),
            },
        )

    def test_params_split_across_both(self, extra_shape):
        _assert_views_agree(
            _ExtraParamClient,
            {
                "username": "u",
                "password": "p",
                "host": "h",
                "extra": extra_shape({"port": 1521, "sid": "DB1"}),
            },
        )

    def test_gate_values_match_runtime_resolved_values(self, extra_shape):
        """Key presence is the contract; this locks the values down too.

        Both views serialize from the same `creds_dict`, so agreement here is
        structural rather than coincidental — but nothing enforced it, and a
        future change to `_serialize_credential_value` could hand the gate a
        differently-rendered `port` than the DSN gets.
        """
        creds = {
            "username": "u",
            "password": "p",
            "host": "h",
            "extra": extra_shape({"port": 1521, "sid": "DB1"}),
        }
        extra = (
            json.loads(creds["extra"])
            if isinstance(creds["extra"], str)
            else creds["extra"]
        )
        pairs = {p["key"]: p["value"] for p in flatten_credentials_to_pairs(creds)}

        for param in _ExtraParamClient.DB_CONFIG.required:
            # The runtime lens: top level first, then `extra` (clients/sql.py).
            runtime_value = creds.get(param) or extra.get(param)
            gate_value = pairs.get(param, pairs.get(f"extra.{param}"))
            assert gate_value == str(runtime_value), (
                f"{param!r}: runtime resolves {runtime_value!r} but the gate "
                f"view carries {gate_value!r}"
            )


class TestGateViewNeverRaises:
    """The gate view is built where no caller can act on a parse failure.

    A credential the runtime client will reject must still flatten — the
    typed error belongs on the runtime path, not on the flattening path.
    """

    @pytest.mark.parametrize(
        "extra",
        ["{not-json", '["a", "b"]', "", 7, None],
        ids=["undecodable", "json_array", "empty_string", "non_mapping", "null"],
    )
    def test_unusable_extra_flattens_to_top_level_only(self, extra: Any):
        assert flatten_credentials_to_pairs({"username": "u", "extra": extra}) == [
            {"key": "username", "value": "u"}
        ]
