"""Unit tests for agent-json v2 models and parsing."""

import pytest

from application_sdk.credentials.errors import CredentialParseError
from application_sdk.credentials.sources import (
    V2_ENVELOPE_KEYS,
    FieldSource,
    is_v2,
    parse_v2_fields,
)

_PASSWORD_REF = {"valueFrom": {"secretKeyRef": {"name": "db-secret", "key": "pw"}}}


def _v2(**fields):
    return {"format": 2, "agent-name": "a", **fields}


class TestIsV2:
    def test_exact_int_two(self):
        assert is_v2({"format": 2}) is True

    @pytest.mark.parametrize("fmt", [True, "2", 2.0, 1, 3, None, [2]])
    def test_other_values_are_not_v2(self, fmt):
        assert is_v2({"format": fmt}) is False

    def test_missing_format(self):
        assert is_v2({"agent-name": "a"}) is False


class TestParseValid:
    def test_full_example(self):
        fields = parse_v2_fields(
            _v2(
                **{
                    "secret-manager": "awssecretmanager",
                    "name": "atlan-example-app-abcd",
                    "host": {"value": "db.example.com"},
                    "port": {"value": 1521},
                    "basic.username": {
                        "valueFrom": {"secretKeyRef": {"name": "u", "key": "u"}}
                    },
                    "basic.password": _PASSWORD_REF,
                    "extra.ca_cert": {
                        "valueFrom": {"fileRef": {"path": "objectstore://certs/ca.pem"}}
                    },
                }
            )
        )
        assert set(fields) == {
            "host",
            "port",
            "basic.username",
            "basic.password",
            "extra.ca_cert",
        }
        assert fields["port"].value == 1521
        assert type(fields["port"].value) is int
        ref = fields["basic.password"].value_from.secret_key_ref
        assert (ref.name, ref.key) == ("db-secret", "pw")
        assert fields["extra.ca_cert"].value_from.file_ref.path.endswith("ca.pem")

    @pytest.mark.parametrize("literal", ["s", 5, 1.5, True, False, None, ""])
    def test_value_keeps_json_scalar_type(self, literal):
        src = parse_v2_fields(_v2(host={"value": literal}))["host"]
        assert src.value == literal and type(src.value) is type(literal)

    def test_envelope_keys_are_not_parsed_as_sources(self):
        raw = _v2(**{k: "x" for k in V2_ENVELOPE_KEYS if k != "format"})
        assert parse_v2_fields(raw) == {}

    def test_empty_v1_fetch_keys_from_a_defaults_dump_are_ignored(self):
        assert parse_v2_fields(_v2(**{"key-type": "", "secret-path": ""})) == {}


class TestParseInvalid:
    @pytest.mark.parametrize(
        "bad",
        [
            "plain-string",
            5,
            None,
            [],
            {},
            {"value": "x", "valueFrom": {"fileRef": {"path": "p"}}},
            {"valueFrom": {}},
            {"valueFrom": {"secretKeyRef": None}},
            {
                "valueFrom": {
                    "secretKeyRef": {"name": "n", "key": "k"},
                    "fileRef": {"path": "p"},
                }
            },
            {"valueFrom": {"secretKeyRef": {"name": "", "key": "k"}}},
            {"valueFrom": {"secretKeyRef": {"name": "n", "key": ""}}},
            {"valueFrom": {"secretKeyRef": {"name": "n"}}},
            {"valueFrom": {"fileRef": {"path": ""}}},
            {"valueFrom": {"configMapKeyRef": {"name": "n", "key": "k"}}},
            {"valueFrom": {"secretKeyRef": {"name": "n", "key": "k", "extra": 1}}},
            {"value": "x", "unknown": 1},
            {"value": {"nested": 1}},
            {"value": ["a"]},
        ],
    )
    def test_invalid_shape_names_field_only(self, bad):
        with pytest.raises(CredentialParseError) as exc:
            parse_v2_fields(_v2(**{"basic.password": bad}))
        assert "'basic.password'" in str(exc.value)
        assert exc.value.__cause__ is None

    def test_message_never_contains_the_secret_name_or_value(self):
        bad = {"valueFrom": {"secretKeyRef": {"name": "very-secret-name", "key": ""}}}
        with pytest.raises(CredentialParseError) as exc:
            parse_v2_fields(_v2(x=bad))
        assert "very-secret-name" not in str(exc.value)
        with pytest.raises(CredentialParseError) as exc:
            parse_v2_fields(_v2(x="hunter2"))
        assert "hunter2" not in str(exc.value)

    @pytest.mark.parametrize("key", ["key-type", "secret-path"])
    def test_v1_fetch_keys_conflict(self, key):
        with pytest.raises(CredentialParseError, match=key):
            parse_v2_fields(_v2(host={"value": "h"}, **{key: "single-key"}))

    def test_non_v2_payload_is_rejected(self):
        with pytest.raises(CredentialParseError):
            parse_v2_fields({"agent-name": "a", "host": {"value": "h"}})


def test_field_source_is_frozen():
    src = FieldSource.model_validate({"value": 1})
    with pytest.raises(Exception):
        src.value = 2  # type: ignore[misc]
