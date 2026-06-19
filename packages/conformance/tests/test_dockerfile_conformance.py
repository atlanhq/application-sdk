"""Meta-tests for the I-series Dockerfile conformance checks (I001–I005).

Each rule is tested to fire *exactly* on the pattern it targets and to stay
silent for all valid alternatives — including multi-stage builds and inline
suppression.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.dockerfile_conformance import (
    _env_vars,
    _final_stage_start_idx,
    _parse_directives,
    _parse_dockerfile,
    discover,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_FILE = "Dockerfile"

_VALID_DOCKERFILE = """\
FROM registry.atlan.com/public/app-runtime-base:3

COPY --chown=appuser:appuser requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=appuser:appuser . /app/

ENV ATLAN_APP_MODULE=myapp.app:MyApp
"""


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, _FILE)]


def _active(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, _FILE) if not f.suppressed]


# ── Helper: fully-valid Dockerfile is clean ─────────────────────────────────


def test_valid_dockerfile_produces_no_findings() -> None:
    assert _ids(_VALID_DOCKERFILE) == []


# ── I001 — DockerfileWrongBaseImage ──────────────────────────────────────────


def test_f001_fires_on_wrong_registry() -> None:
    src = "FROM python:3.11\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" in _ids(src)


def test_f001_fires_on_cgr_image() -> None:
    src = "FROM cgr.dev/atlan.com/app-framework-golden:3.13\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" in _ids(src)


def test_f001_fires_on_latest_tag() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:latest\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I001"]
    assert len(findings) == 1
    assert ":latest" in findings[0].message


def test_f001_fires_on_dev_branch_tag() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:main\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" in _ids(src)


def test_f001_fires_on_pinned_patch_version() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3.2.1\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" in _ids(src)


def test_f001_fires_on_v2_major_tag() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:2\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" in _ids(src)


def test_f001_fires_on_no_tag() -> None:
    # No tag implies :latest
    src = "FROM registry.atlan.com/public/app-runtime-base\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" in _ids(src)


def test_f001_silent_on_correct_base() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" not in _ids(src)


def test_f001_silent_on_correct_base_with_as_alias() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3 AS app\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" not in _ids(src)


def test_f001_silent_on_correct_base_with_platform() -> None:
    src = "FROM --platform=linux/amd64 registry.atlan.com/public/app-runtime-base:3\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    assert "I001" not in _ids(src)


def test_f001_checks_final_from_in_multistage() -> None:
    # Intermediate builder stage may use any image; final stage must be correct.
    src = (
        "FROM python:3.11 AS builder\n"
        "RUN echo build\n"
        "\n"
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "COPY --from=builder /out /app\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
    )
    assert "I001" not in _ids(src)


def test_f001_fires_when_final_from_is_wrong_in_multistage() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3 AS base\n"
        "RUN echo first\n"
        "\n"
        "FROM python:3.11\n"  # wrong final stage
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
    )
    assert "I001" in _ids(src)


def test_f001_pointing_at_from_line() -> None:
    src = "FROM python:3.11\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I001"]
    assert findings[0].line == 1


def test_f001_suppressed_by_preceding_comment() -> None:
    src = (
        "# conformance: ignore[I001] SDK is the base image builder, not a consumer\n"
        "FROM cgr.dev/atlan.com/app-framework-golden:3.13\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I001"]
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert "I001" not in _active(src)


def test_f001_bare_suppress_without_reason_not_accepted() -> None:
    src = (
        "# conformance: ignore[I001]\n"
        "FROM python:3.11\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
    )
    assert "I001" in _active(src)


# ── I002 — DockerfileEntrypointOverride ──────────────────────────────────────


def test_f002_fires_on_cmd() -> None:
    src = _VALID_DOCKERFILE + '\nCMD ["python", "-m", "myapp"]\n'
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I002"]
    assert len(findings) == 1
    assert "CMD" in findings[0].message


def test_f002_fires_on_entrypoint() -> None:
    src = _VALID_DOCKERFILE + '\nENTRYPOINT ["/entrypoint.sh"]\n'
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I002"]
    assert len(findings) == 1
    assert "ENTRYPOINT" in findings[0].message


def test_f002_fires_on_both_cmd_and_entrypoint() -> None:
    src = _VALID_DOCKERFILE + '\nCMD ["app"]\nENTRYPOINT ["/ep.sh"]\n'
    rule_ids = [f.rule_id for f in scan_text(src, _FILE) if f.rule_id == "I002"]
    assert len(rule_ids) == 2


def test_f002_silent_on_clean_dockerfile() -> None:
    assert "I002" not in _ids(_VALID_DOCKERFILE)


def test_f002_pointing_at_instruction_line() -> None:
    src = 'FROM registry.atlan.com/public/app-runtime-base:3\nENV ATLAN_APP_MODULE=myapp:MyApp\nCMD ["x"]\n'
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I002"]
    assert findings[0].line == 3


def test_f002_suppressed_by_preceding_comment() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
        "# conformance: ignore[I002] legacy wrapper requires explicit CMD\n"
        'CMD ["legacy_wrapper"]\n'
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I002"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── I003 — DockerfileAppModuleMissing ────────────────────────────────────────


def test_f003_fires_when_env_absent() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3\nRUN echo hi\n"
    assert "I003" in _ids(src)


def test_f003_fires_when_env_set_empty() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3\nENV ATLAN_APP_MODULE=\n"
    assert "I003" in _ids(src)


def test_f003_silent_on_key_value_form() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3\nENV ATLAN_APP_MODULE=myapp.app:MyApp\n"
    assert "I003" not in _ids(src)


def test_f003_silent_on_space_separated_form() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3\nENV ATLAN_APP_MODULE myapp.app:MyApp\n"
    assert "I003" not in _ids(src)


def test_f003_silent_on_multi_pair_env() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV OTHER=val ATLAN_APP_MODULE=myapp:MyApp\n"
    )
    assert "I003" not in _ids(src)


def test_f003_finding_at_line_1() -> None:
    src = "FROM registry.atlan.com/public/app-runtime-base:3\n"
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I003"]
    assert findings[0].line == 1


def test_f003_suppressed_by_first_line_comment() -> None:
    # For a file-level missing finding, a directive at line 1 suppresses it.
    src = (
        "# conformance: ignore[I003] module set via deployment env var injection\n"
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I003"]
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert "I003" not in _active(src)


# ── I004 — DockerfileAppModeHardcoded ────────────────────────────────────────


def test_f004_fires_on_hardcoded_mode() -> None:
    src = _VALID_DOCKERFILE + "\nENV ATLAN_APP_MODE=worker\n"
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I004"]
    assert len(findings) == 1


def test_f004_fires_on_server_mode() -> None:
    src = _VALID_DOCKERFILE + "\nENV ATLAN_APP_MODE=server\n"
    assert "I004" in _ids(src)


def test_f004_fires_on_mode_in_multi_pair_env() -> None:
    src = _VALID_DOCKERFILE + "\nENV SOME_VAR=val ATLAN_APP_MODE=worker\n"
    assert "I004" in _ids(src)


def test_f004_silent_when_mode_not_set() -> None:
    assert "I004" not in _ids(_VALID_DOCKERFILE)


def test_f004_pointing_at_env_line() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
        "ENV ATLAN_APP_MODE=worker\n"
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I004"]
    assert findings[0].line == 3


def test_f004_suppressed_by_preceding_comment() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
        "# conformance: ignore[I004] single-mode app, mode is always worker\n"
        "ENV ATLAN_APP_MODE=worker\n"
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I004"]
    assert findings[0].suppressed is True
    assert "I004" not in _active(src)


# ── I005 — DockerfileRootUser ─────────────────────────────────────────────────


def test_f005_fires_on_user_root() -> None:
    src = _VALID_DOCKERFILE + "\nUSER root\n"
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I005"]
    assert len(findings) == 1
    assert "root" in findings[0].message


def test_f005_fires_on_user_zero() -> None:
    src = _VALID_DOCKERFILE + "\nUSER 0\n"
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I005"]
    assert len(findings) == 1


def test_f005_fires_on_root_case_insensitive() -> None:
    src = _VALID_DOCKERFILE + "\nUSER ROOT\n"
    assert "I005" in _ids(src)


def test_f005_silent_on_user_appuser() -> None:
    # Explicit USER appuser is fine (base already sets it; no harm in being explicit).
    src = _VALID_DOCKERFILE + "\nUSER appuser\n"
    assert "I005" not in _ids(src)


def test_f005_silent_when_no_user_instruction() -> None:
    assert "I005" not in _ids(_VALID_DOCKERFILE)


def test_f005_ignores_builder_stage_root_user() -> None:
    # USER root in an intermediate builder stage is not flagged.
    src = (
        "FROM python:3.11 AS builder\n"
        "USER root\n"
        "RUN apt-get install -y curl\n"
        "\n"
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "COPY --from=builder /usr/bin/curl /usr/bin/curl\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
    )
    assert "I005" not in _ids(src)


def test_f005_fires_on_root_in_final_stage_of_multistage() -> None:
    src = (
        "FROM python:3.11 AS builder\n"
        "USER root\n"
        "\n"
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
        "USER root\n"  # bad — in final stage
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I005"]
    assert len(findings) == 1
    assert findings[0].line == 6


def test_f005_pointing_at_user_line() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
        "USER root\n"
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I005"]
    assert findings[0].line == 3


def test_f005_suppressed_by_preceding_comment() -> None:
    src = (
        "FROM registry.atlan.com/public/app-runtime-base:3\n"
        "ENV ATLAN_APP_MODULE=myapp:MyApp\n"
        "# conformance: ignore[I005] needs root to bind privileged port\n"
        "USER root\n"
    )
    findings = [f for f in scan_text(src, _FILE) if f.rule_id == "I005"]
    assert findings[0].suppressed is True
    assert "I005" not in _active(src)


# ── _parse_dockerfile internals ──────────────────────────────────────────────


def test_parse_handles_line_continuation() -> None:
    src = "ENV FOO=bar \\\n    BAZ=qux\n"
    instrs = _parse_dockerfile(src)
    assert len(instrs) == 1
    assert instrs[0].keyword == "ENV"
    assert "FOO=bar" in instrs[0].args
    assert "BAZ=qux" in instrs[0].args


def test_parse_skips_comments_and_blank_lines() -> None:
    src = "\n# this is a comment\nFROM python:3.11\n"
    instrs = _parse_dockerfile(src)
    assert len(instrs) == 1
    assert instrs[0].keyword == "FROM"
    assert instrs[0].line == 3


def test_parse_records_correct_start_line() -> None:
    src = "FROM python:3.11\n\nENV ATLAN_APP_MODULE=myapp:MyApp\n"
    instrs = _parse_dockerfile(src)
    assert instrs[0].line == 1
    assert instrs[1].line == 3


# ── _env_vars internals ──────────────────────────────────────────────────────


def test_env_vars_key_value_form() -> None:
    assert _env_vars("ATLAN_APP_MODULE=myapp.app:MyApp") == {
        "ATLAN_APP_MODULE": "myapp.app:MyApp"
    }


def test_env_vars_multiple_pairs() -> None:
    result = _env_vars("FOO=bar BAZ=qux")
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_env_vars_deprecated_space_form() -> None:
    assert _env_vars("ATLAN_APP_MODULE myapp.app:MyApp") == {
        "ATLAN_APP_MODULE": "myapp.app:MyApp"
    }


def test_env_vars_empty_value() -> None:
    assert _env_vars("ATLAN_APP_MODULE=") == {"ATLAN_APP_MODULE": ""}


# ── _parse_directives internals ──────────────────────────────────────────────


def test_parse_directives_captures_rule_id_and_reason() -> None:
    src = "# conformance: ignore[I001] this is the base image builder\n"
    d = _parse_directives(src)
    assert 1 in d
    rule_ids, just = d[1]
    assert rule_ids == frozenset({"I001"})
    assert just == "this is the base image builder"


def test_parse_directives_rejects_bare_without_reason() -> None:
    src = "# conformance: ignore[I001]\n"
    assert _parse_directives(src) == {}


def test_parse_directives_multiple_ids() -> None:
    src = "# conformance: ignore[I001,I002] multi-rule suppress\n"
    d = _parse_directives(src)
    rule_ids, _ = d[1]
    assert rule_ids == frozenset({"I001", "I002"})


# ── _final_stage_start_idx internals ─────────────────────────────────────────


def test_final_stage_start_idx_single_stage() -> None:
    instrs = _parse_dockerfile("FROM python:3.11\nRUN echo hi\n")
    assert _final_stage_start_idx(instrs) == 0


def test_final_stage_start_idx_multistage() -> None:
    src = "FROM python:3.11 AS builder\nRUN echo b\nFROM base:3\nRUN echo f\n"
    instrs = _parse_dockerfile(src)
    idx = _final_stage_start_idx(instrs)
    assert instrs[idx].keyword == "FROM"
    assert "base:3" in instrs[idx].args


# ── discover() ───────────────────────────────────────────────────────────────


def test_discover_finds_dockerfile_at_root(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    found = discover(tmp_path)
    assert len(found) == 1
    assert found[0].name == "Dockerfile"


def test_discover_empty_when_no_dockerfile(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


# ── Rule metadata wiring ──────────────────────────────────────────────────────


def test_f001_rule_metadata() -> None:
    rule = get_rule("I001")
    assert rule.name == "DockerfileWrongBaseImage"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is True
    assert rule.rationale.strip()


def test_f002_rule_metadata() -> None:
    rule = get_rule("I002")
    assert rule.name == "DockerfileEntrypointOverride"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP


def test_f003_rule_metadata() -> None:
    rule = get_rule("I003")
    assert rule.name == "DockerfileAppModuleMissing"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP


def test_f004_rule_metadata() -> None:
    rule = get_rule("I004")
    assert rule.name == "DockerfileAppModeHardcoded"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP


def test_f005_rule_metadata() -> None:
    rule = get_rule("I005")
    assert rule.name == "DockerfileRootUser"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP
