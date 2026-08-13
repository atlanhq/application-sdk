"""Unit tests for the uniform integration source accessor.

These pin the two properties every connector relies on: the SAME reader resolves
a source whether it arrived as the DataForge ``E2E_SOURCE_RAW_JSON`` blob or as
flat ``E2E_<DATASOURCE>_*`` vars, and an absent source is reported (skip), never
an error.
"""

from __future__ import annotations

import json

from application_sdk.testing.integration.source import DataForgeSource


def test_reads_the_dataforge_raw_json_blob():
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps(
            {"host": "db.internal", "port": "5432", "username": "u", "password": "p"}
        ),
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.available
    assert src.datasource == "postgres"
    assert src.get("host") == "db.internal"
    assert src.require("host", "username", "password")


def test_reads_flat_vars_on_the_static_path_with_no_blob():
    # The static E2E_SOURCE_ENV_JSON path writes no breadcrumb, so the connector
    # supplies its datasource name and the flat prefix vars still resolve.
    env = {
        "E2E_POSTGRES_HOST": "db.internal",
        "E2E_POSTGRES_USERNAME": "u",
        "E2E_POSTGRES_PASSWORD": "p",
    }
    src = DataForgeSource.from_env("postgres", environ=env)

    assert src.available
    assert src.get("host") == "db.internal"
    assert src.require("host", "username", "password")


def test_blob_wins_over_flat_vars_on_collision():
    # The reusable exports static first then overwrites with the fetch; the blob
    # (fetch) is authoritative, so it wins here too.
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps({"host": "from-dataforge"}),
        "E2E_POSTGRES_HOST": "from-static",
    }
    assert DataForgeSource.from_env(environ=env).get("host") == "from-dataforge"


def test_lookup_is_case_and_separator_insensitive_with_aliases():
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps({"iam_role_arn": "arn:aws:iam::1:role/x"}),
    }
    src = DataForgeSource.from_env(environ=env)

    # Same field, asked for three ways.
    assert src.get("iam_role_arn") == "arn:aws:iam::1:role/x"
    assert src.get("iamRoleArn") == "arn:aws:iam::1:role/x"
    assert src.get("IAM-ROLE-ARN") == "arn:aws:iam::1:role/x"
    # Alias fallback: prefer the specific name, fall back to the canonical one.
    assert src.get("basic_auth_host", "host", default="none") == "none"


def test_absent_source_is_unavailable_not_an_error():
    src = DataForgeSource.from_env("postgres", environ={})

    assert not src.available
    assert not src.require("host")
    assert src.get("host") is None
    assert src.get("host", default="fallback") == "fallback"


def test_datasource_prefix_scoping_never_leaks_unrelated_env():
    # Tenant creds and flags share the E2E_ namespace; they must not land in the
    # source bag just because they start with E2E_.
    env = {
        "E2E_POSTGRES_HOST": "db.internal",
        "E2E_TENANT_MATRIX_JSON": "{...}",
        "E2E_SOURCE_ENABLED": "true",
    }
    src = DataForgeSource.from_env("postgres", environ=env)

    assert src.as_dict() == {"host": "db.internal"}


def test_empty_and_null_fields_are_dropped():
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps({"host": "db", "password": "", "extra": None}),
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.get("host") == "db"
    assert src.get("password") is None
    assert not src.require("host", "password")


def test_malformed_blob_falls_back_to_flat_vars():
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": "{not valid json",
        "E2E_POSTGRES_HOST": "db.internal",
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.get("host") == "db.internal"
