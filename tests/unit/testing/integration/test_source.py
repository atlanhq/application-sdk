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


def test_alias_ordering_prefers_the_first_name_that_resolves():
    # Only the canonical field present: the alias falls back to it.
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps({"host": "canonical"}),
    }
    src = DataForgeSource.from_env(environ=env)
    assert src.get("basic_auth_host", "host") == "canonical"

    # Both present: the first (most-specific) name wins.
    env["E2E_SOURCE_RAW_JSON"] = json.dumps(
        {"host": "canonical", "basic_auth_host": "specific"}
    )
    src = DataForgeSource.from_env(environ=env)
    assert src.get("basic_auth_host", "host") == "specific"


def test_datasource_names_with_spaces_and_hyphens_derive_the_prefix():
    # Prefix derivation uppercases and folds non-alphanumerics to "_", so a
    # spaced/hyphenated datasource name still scopes its flat vars correctly.
    env = {
        "E2E_MY_DS_HOST": "db.internal",
        "E2E_OTHER_HOST": "unrelated",
    }
    src = DataForgeSource.from_env("my ds", environ=env)
    assert src.get("host") == "db.internal"
    assert src.as_dict() == {"host": "db.internal"}

    src = DataForgeSource.from_env("my-ds", environ=env)
    assert src.get("host") == "db.internal"


def test_blob_loads_with_no_datasource_anywhere():
    # No datasource argument and no breadcrumb: the blob still resolves; the
    # flat pass is skipped (there is no prefix to scope it to).
    env = {
        "E2E_SOURCE_RAW_JSON": json.dumps({"host": "db.internal"}),
        "E2E_POSTGRES_HOST": "ignored",
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.datasource == ""
    assert src.get("host") == "db.internal"
    assert src.as_dict() == {"host": "db.internal"}


def test_datasource_that_normalises_to_empty_skips_the_flat_pass():
    # A datasource of only separator characters derives no usable prefix; the
    # flat pass must not match every E2E_* var in the environment — including
    # one that starts with the bare-underscore folding of that name.
    env = {"E2E_POSTGRES_HOST": "db.internal", "E2E___HOST": "leaked"}
    src = DataForgeSource.from_env("-", environ=env)

    assert not src.available
    assert src.as_dict() == {}


def test_var_exactly_equal_to_the_prefix_is_not_a_field():
    # E2E_POSTGRES_ has no field name after the prefix; storing it under the
    # empty-string key would make an unreadable field count as present.
    env = {
        "E2E_POSTGRES_": "weird",
        "E2E_POSTGRES_HOST": "db.internal",
        "E2E_SOURCE_RAW_JSON": json.dumps({"": "also-weird", "port": "5432"}),
    }
    src = DataForgeSource.from_env("postgres", environ=env)

    assert "" not in src.as_dict()
    assert src.as_dict() == {"port": "5432", "host": "db.internal"}


def test_blob_field_left_empty_is_not_backfilled_by_a_flat_var():
    # Precedence policy: a blob field present but empty RESERVES its key, so
    # the fetch's explicit "this field is empty" beats a stale flat var.
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps({"host": "db", "password": ""}),
        "E2E_POSTGRES_PASSWORD": "stale-static",
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.get("password") is None
    assert not src.require("host", "password")


def test_whitespace_only_flat_var_reads_as_absent():
    env = {
        "E2E_POSTGRES_HOST": "   ",
        "E2E_POSTGRES_USERNAME": "u",
    }
    src = DataForgeSource.from_env("postgres", environ=env)

    assert src.get("host") is None
    assert src.as_dict() == {"username": "u"}


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
        "E2E_SOURCE_RAW_JSON": json.dumps(
            {"host": "db", "password": "", "extra": None}
        ),
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


def test_flat_pass_does_not_leak_a_sibling_datasources_vars():
    # A single-token datasource derives an exact prefix, so the vars of a
    # sibling whose name merely SHARES that prefix (postgres vs
    # postgres-readonly) must not fold into this source's bag: the remainder
    # after an exact prefix is a field name, which never begins with "_".
    env = {
        "E2E_POSTGRES_HOST": "db.internal",
        "E2E_POSTGRES_READONLY_HOST": "sibling.internal",
        "E2E_POSTGRES_READONLY_USERNAME": "sibling-user",
    }
    src = DataForgeSource.from_env("postgres", environ=env)

    assert src.as_dict() == {"host": "db.internal"}

    # …and the sibling reads its own bag untouched.
    sibling = DataForgeSource.from_env("postgres-readonly", environ=env)
    assert sibling.as_dict() == {"host": "sibling.internal", "username": "sibling-user"}


def test_leading_underscore_remainder_still_reads_for_a_folded_datasource():
    # A datasource whose own name carries a separator ("sql.dwh") folds to a
    # prefix where "_" in the remainder CAN be a legitimate fold of the
    # datasource's own characters, so the sibling guard stays off and a
    # separated remainder still resolves (pre-guard behaviour).
    env = {"E2E_SQL_DWH__HOST": "db.internal"}
    src = DataForgeSource.from_env("sql.dwh", environ=env)

    assert src.get("host") == "db.internal"


def test_underscored_field_still_reads_when_datasource_name_is_underscored():
    # The guard keys off the RAW datasource name, not the derived prefix, so
    # "power bi" (which itself contains a separator) keeps the old loose
    # matching even though its derived prefix looks single-token.
    env = {"E2E_POWER_BI_TENANT_ID": "t-1", "E2E_POWER_BI_CLIENT_SECRET": "s-1"}
    src = DataForgeSource.from_env("power bi", environ=env)

    assert src.require("tenant_id", "client_secret")
