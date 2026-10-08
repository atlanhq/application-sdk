"""Meta-tests for the S-series security / secret-hygiene checks (S001, S002).

These checks fan out across the fleet — a buggy check false-positives across
hundreds of apps and triggers spurious remediations.  So each rule is tested to
fire *exactly* when it should and stay silent otherwise, and the false-positive
traps surfaced by the consumer survey (BLDX-1419) are pinned as explicit
must-not-flag cases: ``os.environ[...] =`` writes, ``*_ACCESS_KEY_ID`` public ids,
``*_TOKEN_URL`` endpoints, ``SECRET_STORE_NAME`` reads, and env-var-name string
references stored under a credential-named key.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.security import discover, main, scan_text
from conformance.suite.checks.security._secret_names import is_credential_value_name
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition
from conformance.suite.schema.disposition import Disposition, EnforcementTier, RuleScope


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, "x.py")]


# ── credential-name predicate (the shared heuristic; oracle = the survey) ────────

_MUST_FLAG_NAMES = [
    "password",
    "client_secret",
    "api_key",
    "ATLAN_API_KEY",
    "ATLAN_BEARER_TOKEN",
    "ATLAN_AUTH_CLIENT_SECRET",
    "AE_TOKEN",
    "AZURE_STORAGE_ACCESS_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "PHOENIX_API_KEY",
    "LD_API_KEY",
]

_MUST_NOT_FLAG_NAMES = [
    "AWS_ACCESS_KEY_ID",  # public identifier, not the secret
    "OAUTH_TOKEN_URL",  # endpoint, not the secret
    "SECRET_STORE_NAME",  # store name, not the secret
    "token_name",  # label
    "ATLAN_BASE_URL",  # config
    "partition_key",  # not a credential token
    "sort_key",
    "user_id",
    "next_page",
]


def test_predicate_flags_survey_secret_names() -> None:
    assert [n for n in _MUST_FLAG_NAMES if not is_credential_value_name(n)] == []


def test_predicate_ignores_labels_and_non_secrets() -> None:
    assert [n for n in _MUST_NOT_FLAG_NAMES if is_credential_value_name(n)] == []


# ── S001 HardcodedCredential ─────────────────────────────────────────────────────


def test_s001_fires_on_assignment() -> None:
    findings = scan_text('password = "hunter2"\n', "x.py")
    assert [f.rule_id for f in findings] == ["S001"]
    assert findings[0].line == 1


def test_s001_fires_on_annotated_assignment() -> None:
    assert _ids('api_key: str = "sk-live-abc123"\n') == ["S001"]


def test_s001_fires_on_keyword_argument() -> None:
    assert _ids('connect(token="abc123def")\n') == ["S001"]


def test_s001_fires_on_dict_value() -> None:
    assert _ids('cfg = {"password": "hunter2"}\n') == ["S001"]


def test_s001_silent_on_empty_string() -> None:
    assert _ids('password = ""\n') == []


def test_s001_silent_on_field_default_call() -> None:
    # value is a Call (Field(default="")), not a literal
    assert _ids('password: str = Field(default="")\n') == []


def test_s001_silent_on_url_template_placeholder() -> None:
    assert _ids('secret = "prefix-{value}-suffix"\n') == []


def test_s001_silent_on_env_var_name_reference() -> None:
    # storing an env-var NAME under a credential-named key is a reference, not a secret
    assert _ids('client_secret = "ATLAN_OAUTH2_CLIENT_SECRET"\n') == []
    assert _ids('cfg = {"client_secret": "ATLAN_OAUTH2_CLIENT_SECRET"}\n') == []


def test_s001_silent_on_prose_value() -> None:
    src = (
        "_SUGGESTED_ACTIONS = {\n"
        '    "AUTH_NO_CREDENTIALS": ("Select a credential on the workflow\'s Credential step."),\n'
        '    "AUTH_INVALID_CREDENTIALS": (\n'
        '        "Check the access key, secret key and region on the configured credential."\n'
        "    ),\n"
        "}\n"
    )
    assert _ids(src) == []


def test_s001_silent_on_credential_name_alias_map() -> None:
    src = (
        "_LEGACY_ALIASES = {\n"
        '    "username": "aws_access_key_id",\n'
        '    "password": "aws_secret_access_key",\n'
        '    "accountid": "aws_account_id",\n'
        "}\n"
    )
    assert _ids(src) == []
    assert (
        _ids(
            'aliases = dict(username="aws_access_key_id", '
            'password="aws_secret_access_key")\n'
        )
        == []
    )


def test_s001_still_fires_on_real_secret_literals() -> None:
    assert _ids('cfg = {"password": "hunter2"}\n') == ["S001"]
    assert _ids('password = "s3cr3t_Value"\n') == ["S001"]


def test_s001_still_fires_on_whitespace_bearing_secrets() -> None:
    assert _ids('private_key = "-----BEGIN RSA PRIVATE KEY-----\\nMIIEow..."\n') == [
        "S001"
    ]
    assert _ids('private_key = "-----BEGIN PRIVATE KEY----- MIIEvQ. Ends here."\n') == [
        "S001"
    ]
    assert _ids('cfg = {"token": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc"}\n') == ["S001"]
    assert _ids('api_key = "Basic dXNlcjpwYXNz"\n') == ["S001"]
    assert _ids('password = "correct horse battery staple"\n') == ["S001"]
    assert _ids('password = "Let me in now!"\n') == ["S001"]
    assert _ids('connect(password="Summer is here 2024!")\n') == ["S001"]
    assert _ids('cfg = {"password": "correct horse battery staple"}\n') == ["S001"]
    assert _ids('password = "Let me in right now please!"\n') == ["S001"]
    assert _ids('password = "correct horse battery staple glue lamp."\n') == ["S001"]
    assert _ids('cfg = {"password": "this is my very secret password."}\n') == ["S001"]
    assert _ids('connect(password="Summer is here and it is 2024!")\n') == ["S001"]
    assert _ids('password = "Enter the password for the service account."\n') == [
        "S001"
    ]
    src = (
        "DATABASE = {\n"
        '    "USER": "admin",\n'
        '    "PASSWORD": "correct horse battery staple glue lamp.",\n'
        "}\n"
    )
    assert _ids(src) == ["S001"]


def test_s001_message_table_does_not_hide_secrets() -> None:
    help_text = "Check the access key and region on the configured credential."
    for value in (
        "Use ghp_AbC123dEf456GhI789 to call the API on the server.",
        "The key sk_live_abcdef123456 is the one to use for the API.",
        " Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc is the token for the API.",
        "correct horse battery staple glue lamp.",
        "Let me in right now please!",
        "Use `ghp_abcdefghijklmnopqrst` to call the API on the server.",
        "Set the header to key:ghp_abcdefghijklmnopqrst for the API.",
    ):
        src = (
            "_SUGGESTED_ACTIONS = {\n"
            f'    "AUTH_INVALID_CREDENTIALS": "{help_text}",\n'
            f'    "AUTH_TOKEN": "{value}",\n'
            "}\n"
        )
        assert _ids(src) == ["S001", "S001"], value
    src = (
        "SETTINGS = {\n"
        '    "DB_PASSWORD": "correct horse battery staple glue lamp.",\n'
        '    "ADMIN_PASSWORD": "Let me in right now please!",\n'
        "}\n"
    )
    assert _ids(src) == ["S001", "S001"]


def test_s001_still_fires_on_credential_name_shaped_weak_defaults() -> None:
    assert _ids('db_password = "secret"\n') == ["S001"]
    assert _ids('cfg = {"password": "secret"}\n') == ["S001"]
    assert _ids('cfg = {"password": "api_key"}\n') == ["S001"]
    assert _ids('password = "aws_secret_access_key"\n') == ["S001"]
    src = (
        "cfg = {\n"
        '    "username": "aws_access_key_id",\n'
        '    "password": "hunter2",\n'
        "}\n"
    )
    assert _ids(src) == ["S001"]
    assert _ids('cfg = {"username": "svc_user", "password": "admin_password"}\n') == [
        "S001"
    ]
    assert _ids(
        'cfg = {"client_id": "my_app", "client_secret": "abc123_def456"}\n'
    ) == ["S001"]
    assert _ids('cfg = {"password": "my_secret", "api_key": "test_token"}\n') == [
        "S001",
        "S001",
    ]
    assert _ids('cfg = {"password": "default_password", "token": "dev_token"}\n') == [
        "S001",
        "S001",
    ]
    assert _ids('cfg = {"user": "svc_id", "password": "admin_db_password"}\n') == [
        "S001"
    ]


def test_s001_silent_on_enum_member() -> None:
    src = (
        "from enum import Enum\n"
        "class CredField(Enum):\n"
        '    PASSWORD = "password"\n'
        '    TOKEN = "token"\n'
    )
    assert _ids(src) == []


def test_s001_silent_on_non_credential_name() -> None:
    assert _ids('username = "admin"\n') == []


def test_s001_fires_on_attribute_assignment() -> None:
    # self.password = "..." in __init__ methods / settings classes
    src = 'class Client:\n    def __init__(self) -> None:\n        self.password = "hunter2"\n'
    assert _ids(src) == ["S001"]


def test_s001_fires_on_subscript_assignment() -> None:
    # cfg["password"] = "..." — item assignment, not a dict literal
    assert _ids('cfg["password"] = "hunter2"\n') == ["S001"]


def test_s001_suppressed_by_directive_line_above() -> None:
    src = (
        "# conformance: ignore[S001] fixture password for a local-only sample\n"
        'password = "hunter2"\n'
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert "fixture" in (findings[0].suppression_justification or "")


def test_s001_suppressed_by_trailing_directive() -> None:
    src = 'password = "hunter2"  # conformance: ignore[S001] local sample\n'
    assert scan_text(src, "x.py")[0].suppressed is True


def test_s001_is_warn_tier() -> None:
    assert get_rule("S001").tier is EnforcementTier.WARN


def test_s001_is_both_scoped() -> None:
    assert get_rule("S001").scope is RuleScope.BOTH


# ── S002 RawEnvCredentialAccess ──────────────────────────────────────────────────


def test_s002_fires_on_getenv() -> None:
    findings = scan_text('import os\nk = os.getenv("ATLAN_API_KEY")\n', "x.py")
    assert [f.rule_id for f in findings] == ["S002"]
    assert findings[0].line == 2


def test_s002_fires_on_environ_subscript() -> None:
    assert _ids('import os\nk = os.environ["AWS_SECRET_ACCESS_KEY"]\n') == ["S002"]


def test_s002_fires_on_environ_get() -> None:
    assert _ids('import os\nk = os.environ.get("AE_TOKEN")\n') == ["S002"]


def test_s002_fires_on_environ_pop() -> None:
    # pop still returns (and exposes) the value, so it counts as a read
    assert _ids('import os\nk = os.environ.pop("ATLAN_API_KEY")\n') == ["S002"]


def test_s002_fires_on_aliased_os_import() -> None:
    assert _ids('import os as o\nk = o.getenv("PHOENIX_API_KEY")\n') == ["S002"]


def test_s002_fires_on_from_import_getenv() -> None:
    assert _ids('from os import getenv\nk = getenv("LD_API_KEY")\n') == ["S002"]


def test_s002_fires_on_from_import_environ() -> None:
    assert _ids('from os import environ\nk = environ["ATLAN_BEARER_TOKEN"]\n') == [
        "S002"
    ]


def test_s002_silent_on_access_key_id() -> None:
    assert _ids('import os\nk = os.getenv("AWS_ACCESS_KEY_ID")\n') == []


def test_s002_silent_on_token_url() -> None:
    assert _ids('import os\nk = os.getenv("OAUTH_TOKEN_URL")\n') == []


def test_s002_silent_on_secret_store_name() -> None:
    assert _ids('import os\nk = os.environ.get("SECRET_STORE_NAME")\n') == []


def test_s002_silent_on_environ_write() -> None:
    # writing into os.environ (e.g. the boto3 env-bridge) is not a read
    assert _ids('import os\nos.environ["AWS_SECRET_ACCESS_KEY"] = v\n') == []


def test_s002_silent_on_dynamic_key() -> None:
    assert _ids("import os\nk = os.getenv(name)\n") == []


def test_s002_silent_on_non_credential_env() -> None:
    assert _ids('import os\nk = os.getenv("ATLAN_BASE_URL")\n') == []


def test_s002_suppressed_by_directive_line_above() -> None:
    src = (
        "import os\n"
        "# conformance: ignore[S002] platform self-auth — no SDK secret-store seam\n"
        'token = os.getenv("ATLAN_API_KEY")\n'
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert "self-auth" in (findings[0].suppression_justification or "")


def test_s002_is_warn_tier() -> None:
    assert get_rule("S002").tier is EnforcementTier.WARN


def test_s002_is_app_scoped() -> None:
    # SDK is the provider of the secret-store seam, so S002 never fires on it.
    assert get_rule("S002").scope is RuleScope.APP


# ── discovery: dev-harness exclusions ────────────────────────────────────────────


def test_discover_excludes_run_dev_and_scripts(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "client.py").write_text("x = 1\n")
    (tmp_path / "app" / "run_dev.py").write_text("x = 1\n")
    (tmp_path / "run_dev_combined.py").write_text("x = 1\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "seed.py").write_text("x = 1\n")
    found = {p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)}
    assert found == {"app/client.py"}


# ── tier / disposition / gate ────────────────────────────────────────────────────


def test_warn_violation_is_warning_disposition_and_keeps_gate_green(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.py").write_text('import os\nk = os.getenv("ATLAN_API_KEY")\n')
    sarif_file = tmp_path / "out.sarif"
    code = main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    # WARN tier: visible/counted but non-blocking.
    assert code == 0
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    dispositions = [derive_disposition(r) for r in report.runs[0].results]
    assert dispositions == [Disposition.WARNING]
