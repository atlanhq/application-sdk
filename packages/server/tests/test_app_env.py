"""AppEnv — per-app environment reads in a shared process (ARUN-942).

The property that carries the design is that two apps in ONE process can read
the same key and get different answers. Everything else here guards a way that
could quietly stop being true.
"""

from __future__ import annotations

import os

import pytest

from server_sdk.config.env import APPENV_PREFIX, AppEnv, prefix_for


# ------------------------------------------------------------------ prefixing


def test_prefix_normalises_hyphens_and_case():
    assert prefix_for("ai-memory") == "ATLAN_APPENV__AI_MEMORY__"
    assert prefix_for("governance-studio-packages") == (
        "ATLAN_APPENV__GOVERNANCE_STUDIO_PACKAGES__"
    )


def test_prefix_starts_with_the_shared_marker():
    # CI greps for this marker to find every app-scoped var; if the two drift,
    # variables are written that nothing reads.
    assert prefix_for("redshift").startswith(APPENV_PREFIX)


@pytest.mark.parametrize("bad", ["", "   "])
def test_prefix_rejects_empty_app_name(bad):
    with pytest.raises(ValueError):
        prefix_for(bad)


# ------------------------------------------------------------------- resolution


def test_two_apps_in_one_process_see_different_values(monkeypatch):
    """The whole point. One process, one key, two answers."""
    monkeypatch.setenv(
        "ATLAN_APPENV__AI_MEMORY__EMBEDDING_MODEL", "text-embedding-3-large"
    )
    monkeypatch.setenv(
        "ATLAN_APPENV__ENRICHMENT__EMBEDDING_MODEL", "text-embedding-3-small"
    )

    assert AppEnv("ai-memory").get("EMBEDDING_MODEL") == "text-embedding-3-large"
    assert AppEnv("enrichment").get("EMBEDDING_MODEL") == "text-embedding-3-small"


def test_falls_back_to_the_bare_name(monkeypatch):
    """Shared infra stays unprefixed, so it needs no per-app copy."""
    monkeypatch.delenv("ATLAN_APPENV__AI_MEMORY__S3_BUCKET", raising=False)
    monkeypatch.setenv("S3_BUCKET", "atlan-shared")
    assert AppEnv("ai-memory").get("S3_BUCKET") == "atlan-shared"


def test_prefixed_wins_over_bare(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "shared")
    monkeypatch.setenv("ATLAN_APPENV__AI_MEMORY__S3_BUCKET", "mine")
    assert AppEnv("ai-memory").get("S3_BUCKET") == "mine"


def test_default_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.delenv("ATLAN_APPENV__REDSHIFT__NOPE", raising=False)
    assert AppEnv("redshift").get("NOPE", "fallback") == "fallback"
    assert AppEnv("redshift").get("NOPE") is None


def test_empty_string_is_a_value_not_an_absence(monkeypatch):
    """``FOO=""`` means "explicitly blank" -- server.py:245 relies on it."""
    monkeypatch.setenv("ATLAN_CONTRACT_GENERATED_DIR", "/from/env")
    monkeypatch.setenv("ATLAN_APPENV__GOVERNANCE__ATLAN_CONTRACT_GENERATED_DIR", "")
    assert AppEnv("governance").get("ATLAN_CONTRACT_GENERATED_DIR") == ""


def test_reads_are_live_not_cached(monkeypatch):
    """No caching, so import-time and request-time reads behave identically.

    A cached reader would freeze whatever the environment was at construction,
    which is the trap this class exists to remove.
    """
    env = AppEnv("redshift")
    monkeypatch.setenv("ATLAN_APPENV__REDSHIFT__LATE", "first")
    assert env.get("LATE") == "first"
    monkeypatch.setenv("ATLAN_APPENV__REDSHIFT__LATE", "second")
    assert env.get("LATE") == "second"


def test_one_app_cannot_read_another_apps_scoped_value(monkeypatch):
    monkeypatch.delenv("SECRET_KNOB", raising=False)
    monkeypatch.setenv("ATLAN_APPENV__SNOWFLAKE__SECRET_KNOB", "snowflake-only")
    assert AppEnv("redshift").get("SECRET_KNOB") is None


# ----------------------------------------------------------------------- typed


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        (" True ", True),
    ],
)
def test_get_bool_parses(monkeypatch, raw, expected):
    monkeypatch.setenv("ATLAN_APPENV__AI_MEMORY__ENABLE_MCP", raw)
    assert AppEnv("ai-memory").get_bool("ENABLE_MCP") is expected


def test_get_bool_is_lenient_on_garbage(monkeypatch):
    """A typo'd flag must not crash a hosted app at import and take the mount."""
    monkeypatch.setenv("ATLAN_APPENV__AI_MEMORY__ENABLE_MCP", "yeppers")
    assert AppEnv("ai-memory").get_bool("ENABLE_MCP", default=True) is True
    assert AppEnv("ai-memory").get_bool("ENABLE_MCP", default=False) is False


def test_get_int_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("ATLAN_APPENV__SNOWFLAKE__ATLAN_SNOWFLAKE_SQL_THREADS", " 8 ")
    assert AppEnv("snowflake").get_int("ATLAN_SNOWFLAKE_SQL_THREADS", 4) == 8
    monkeypatch.setenv("ATLAN_APPENV__SNOWFLAKE__ATLAN_SNOWFLAKE_SQL_THREADS", "lots")
    assert AppEnv("snowflake").get_int("ATLAN_SNOWFLAKE_SQL_THREADS", 4) == 4


# --------------------------------------------------------------------- require


def test_require_returns_the_value(monkeypatch):
    monkeypatch.setenv("ATLAN_APPENV__ENRICHMENT__KEYCLOAK_CLIENT_ID", "svc-account")
    assert AppEnv("enrichment").require("KEYCLOAK_CLIENT_ID") == "svc-account"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_require_raises_when_missing_or_blank(monkeypatch, value):
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    key = "ATLAN_APPENV__ENRICHMENT__KEYCLOAK_CLIENT_SECRET"
    monkeypatch.delenv(key, raising=False)
    if value is not None:
        monkeypatch.setenv(key, value)
    with pytest.raises(KeyError) as err:
        AppEnv("enrichment").require("KEYCLOAK_CLIENT_SECRET")
    # the message must name both places it looked, or debugging is guesswork
    assert "KEYCLOAK_CLIENT_SECRET" in str(err.value)
    assert "enrichment" in str(err.value)


def test_require_does_not_leak_the_value_in_the_error(monkeypatch):
    monkeypatch.setenv("ATLAN_APPENV__ENRICHMENT__TOKEN", "   ")
    with pytest.raises(KeyError) as err:
        AppEnv("enrichment").require("TOKEN")
    assert "   " not in str(err.value).replace("TOKEN", "")


# ------------------------------------------------------------------- hygiene


def test_reader_does_not_mutate_the_environment(monkeypatch):
    """Reading must never write -- a mutating reader would race the concurrent
    lifespan phase in the host."""
    monkeypatch.setenv("ATLAN_APPENV__REDSHIFT__A", "1")
    before = dict(os.environ)
    env = AppEnv("redshift")
    env.get("A")
    env.get("MISSING", "d")
    env.get_bool("A")
    env.get_int("A", 0)
    assert dict(os.environ) == before
