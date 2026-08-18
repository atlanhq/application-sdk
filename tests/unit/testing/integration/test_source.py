"""Unit tests for the uniform integration source accessor.

These pin the two properties every connector relies on: the SAME reader resolves
a source whether it arrived as the DataForge ``E2E_SOURCE_RAW_JSON`` blob or as
flat ``E2E_<DATASOURCE>_*`` vars, and an absent source is reported (skip), never
an error.
"""

from __future__ import annotations

import json

from application_sdk.testing.integration.source import DataForgeSource


def test_public_reexport_is_the_same_class():
    # The public package path re-exports the class; pin it so an __all__ or
    # import regression in __init__.py can't ship green under these tests.
    from application_sdk.testing.integration import DataForgeSource as PublicSource

    assert PublicSource is DataForgeSource


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


def test_require_with_no_arguments_is_false():
    # The availability gate must not read as "source present" when it names no
    # fields: a bare require() is vacuous, and vacuous must not gate green.
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps({"host": "db.internal"}),
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.available
    assert not src.require()


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


def test_non_scalar_blob_values_are_skipped_but_reserve_the_key():
    # The blob contract is scalars-only (the fetch puts structured fields in
    # E2E_SOURCE_EXTRA_JSON, which this reader ignores). A hand-written blob
    # with a nested value must not be stored as its Python repr — but the key
    # stays RESERVED, so a flat var can't backfill it with a divergent scalar.
    env = {
        "E2E_SOURCE_DATASOURCE": "postgres",
        "E2E_SOURCE_RAW_JSON": json.dumps(
            {"host": "db.internal", "region": {"name": "us-east-1"}}
        ),
        "E2E_POSTGRES_REGION": "flat-region",
    }
    src = DataForgeSource.from_env(environ=env)

    assert src.get("host") == "db.internal"
    assert src.get("region") is None
    assert src.as_dict() == {"host": "db.internal"}


def test_flat_pass_resolves_vars_under_a_custom_output_prefix():
    # The fetch exports flat vars under E2E_<dataforge-output-prefix>_ when the
    # caller sets one (metabase → E2E_META_BASE_HOST). On the static path there
    # is no blob and no prefix breadcrumb, so the reader must be told the alias
    # via env_prefix — otherwise the bag comes back empty and the suite
    # silently skips.
    env = {
        "E2E_META_BASE_HOST": "mb.internal",
        "E2E_META_BASE_USERNAME": "u",
        "E2E_META_BASE_PASSWORD": "p",
    }
    src = DataForgeSource.from_env("metabase", env_prefix="meta_base", environ=env)

    assert src.available
    assert src.datasource == "metabase"
    assert src.require("host", "username", "password")
    assert src.get("host") == "mb.internal"

    # Without the hint the derived prefix (E2E_METABASE_) matches nothing —
    # this is the bug env_prefix exists to close, pinned so it stays visible.
    assert not DataForgeSource.from_env("metabase", environ=env).available


def test_flat_pass_resolves_a_single_token_datasources_underscored_fields():
    # The export contract (_env_name in fetch_dataforge_source.py) folds a
    # source's OWN multi-word fields with "_" — iam_role_arn exports as
    # E2E_POSTGRES_IAM_ROLE_ARN — so on the flat/static path these must resolve.
    env = {"E2E_POSTGRES_IAM_ROLE_ARN": "arn:aws:iam::1:role/x"}
    src = DataForgeSource.from_env("postgres", environ=env)

    assert src.available
    assert src.get("iam_role_arn") == "arn:aws:iam::1:role/x"


def test_flat_pass_resolves_an_already_underscored_datasources_fields():
    # power_bi and power bi are the SAME datasource spelled two ways; both must
    # resolve their underscored fields identically on the flat path.
    env = {"E2E_POWER_BI_TENANT_ID": "t-1", "E2E_POWER_BI_CLIENT_SECRET": "s-1"}

    assert DataForgeSource.from_env("power_bi", environ=env).require(
        "tenant_id", "client_secret"
    )
    assert DataForgeSource.from_env("power bi", environ=env).require(
        "tenant_id", "client_secret"
    )


def test_sibling_datasource_vars_leak_is_a_known_documented_limitation():
    # With no delimiter between datasource and field that the datasource
    # segment can never produce, a sibling's vars DO fold into the bag today
    # (E2E_POSTGRES_READONLY_* → readonlyhost). This pins the current behavior
    # so the export-contract fix (a ``__`` delimiter at _env_name) can flip it
    # deliberately rather than silently.
    env = {
        "E2E_POSTGRES_HOST": "db.internal",
        "E2E_POSTGRES_READONLY_HOST": "sibling.internal",
    }
    src = DataForgeSource.from_env("postgres", environ=env)

    assert src.get("host") == "db.internal"
    assert src.get("readonlyhost") == "sibling.internal"  # the leak, documented
