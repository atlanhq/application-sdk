"""Unit tests for application_sdk.clients.gateway_auth.gateway_auth_headers."""

import pytest

from application_sdk.clients.gateway_auth import gateway_auth_headers


def _set(monkeypatch, value):
    monkeypatch.setattr("application_sdk.constants.GATEWAY_AUTH_HEADERS", value)


def test_unset_returns_empty(monkeypatch):
    _set(monkeypatch, "")
    assert gateway_auth_headers() == {}


def test_valid_json_object(monkeypatch):
    _set(
        monkeypatch,
        '{"X-Client-Id": "abc", "X-Client-Secret": "def"}',
    )
    assert gateway_auth_headers() == {"X-Client-Id": "abc", "X-Client-Secret": "def"}


def test_values_are_stringified(monkeypatch):
    _set(monkeypatch, '{"X-Retry": 3, "X-Enabled": true}')
    assert gateway_auth_headers() == {"X-Retry": "3", "X-Enabled": "True"}


def test_invalid_json_returns_empty(monkeypatch):
    _set(monkeypatch, "not-json{")
    assert gateway_auth_headers() == {}


@pytest.mark.parametrize("value", ['["a", "b"]', '"just-a-string"', "42"])
def test_non_object_json_returns_empty(monkeypatch, value):
    _set(monkeypatch, value)
    assert gateway_auth_headers() == {}
