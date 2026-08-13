"""Tests for C004 UnretriedToolDownload check."""

from __future__ import annotations

import pytest
from conformance.suite.checks.download_retry import (
    RULE_ID,
    is_retried,
    is_tool_download,
    iter_logical_lines,
    retry_is_complete,
    scan_text,
    wrapper_variables,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier

# ── Rule metadata ──────────────────────────────────────────────────────────────


def test_rule_is_registered_and_warn_tier() -> None:
    """WARN, not BLOCK: repos carry these throughout and a one-shot fetch is a
    reliability defect, not a security or correctness one."""
    rule = get_rule(RULE_ID)
    assert rule.name == "UnretriedToolDownload"
    assert rule.tier is EnforcementTier.WARN
    assert rule.autofixable is False


# ── Logical-line joining ───────────────────────────────────────────────────────


def test_backslash_continuations_are_joined() -> None:
    """The wrapper and the command it wraps are conventionally on separate
    physical lines; without joining, every correct call reads as unwrapped."""
    text = 'with-retry.sh \\\n  curl -fsSL -o /tmp/pkl "https://example.invalid/pkl"\n'
    lines = list(iter_logical_lines(text))
    assert len(lines) == 1
    assert "with-retry.sh" in lines[0].text
    assert "curl" in lines[0].text


def test_joined_line_reports_the_first_physical_line() -> None:
    text = "echo hi\nwith-retry.sh \\\n  curl -o /tmp/x https://example.invalid/x\n"
    joined = [ln for ln in iter_logical_lines(text) if "curl" in ln.text][0]
    assert joined.line == 2


def test_unterminated_continuation_at_eof_is_still_yielded() -> None:
    lines = list(iter_logical_lines("curl -o /tmp/x https://example.invalid/x \\"))
    assert len(lines) == 1


# ── What counts as a tool download ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        'curl -fsSL -o /tmp/pkl "https://github.com/apple/pkl/releases/download/0.27.2/pkl-linux-amd64"',
        "wget -q https://github.com/dapr/dapr/releases/download/v1.16.5/daprd.tar.gz -O /tmp/d.tar.gz",
        "curl -fsSL https://get.helm.sh/helm-v3.14.0-linux-amd64.tar.gz | tar xz",
        "curl -sSf https://temporal.download/cli.sh | sh",
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
    ],
)
def test_installing_fetches_are_downloads(command: str) -> None:
    assert is_tool_download(command)


def test_env_assignment_between_pipe_and_shell_is_still_a_download() -> None:
    """Dapr's documented installer is invoked exactly this way; missing it would
    let the single most incident-prone line in the fleet read as 'not a download'."""
    command = (
        "curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh "
        '| DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s 1.16.5'
    )
    assert is_tool_download(command)


@pytest.mark.parametrize(
    "command",
    [
        # Body is read, not installed — a version lookup.
        "curl -fsS https://api.github.com/repos/aquasecurity/trivy/releases/latest",
        # Health probe: -o /dev/null discards the body by definition.
        'curl -s -o /dev/null -w "%{http_code}" --max-time 30 https://example.invalid/health',
        # Local service, not a download.
        "curl -o /tmp/x http://localhost:3500/v1.0/healthz",
        "curl -o /tmp/x http://127.0.0.1:8000/ready",
        # Not a fetch at all.
        "echo curl",
    ],
)
def test_non_installing_fetches_are_not_downloads(command: str) -> None:
    assert not is_tool_download(command)


# ── Retry detection ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "with-retry.sh curl -fsSL -o /tmp/pkl https://example.invalid/pkl",
        "curl --retry 5 --retry-delay 5 --retry-all-errors -o /tmp/x https://example.invalid/x",
        "wget --tries=5 --waitretry=10 https://example.invalid/x -O /tmp/x",
        "wget --retry-on-http-error=503 https://example.invalid/x -O /tmp/x",
    ],
)
def test_retried_commands_are_recognised(command: str) -> None:
    assert is_retried(command)


def test_wrapper_held_in_a_shell_variable_counts_as_retried() -> None:
    """Scripts hoist the wrapper path into a variable; without resolving that,
    the rule fires on exactly the code it is asking people to write."""
    text = (
        'WITH_RETRY="$SCRIPT_DIR/../../scripts/with-retry.sh"\n'
        '"$WITH_RETRY" wget -q https://example.invalid/x -O /tmp/x\n'
    )
    assert wrapper_variables(text) == frozenset({"WITH_RETRY"})
    assert scan_text(text, "f.sh") == []


def test_unrelated_variable_does_not_count_as_a_wrapper() -> None:
    """Only a variable holding with-retry.sh suppresses a finding — any other
    variable on the line must leave the unretried download flagged."""
    text = (
        'PREFIX="/usr/bin/time"\n"$PREFIX" curl -o /tmp/x https://example.invalid/x\n'
    )
    assert wrapper_variables(text) == frozenset()
    assert len(scan_text(text, "f.sh")) == 1


def test_fetcher_hidden_behind_a_variable_is_not_detected() -> None:
    """Documented limitation, pinned so it is a known gap rather than a surprise.

    Detection keys on a literal `curl`/`wget` token; resolving `"$TOOL" -o ...`
    back to curl would need real shell parsing. This shape does not occur in the
    fleet today, and under-reporting is the right failure direction for a rule
    whose findings are read by humans.
    """
    text = 'TOOL="/usr/bin/curl"\n"$TOOL" -o /tmp/x https://example.invalid/x\n'
    assert scan_text(text, "f.sh") == []


# ── Tuning-only companion flags do not enable a retry ──────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        # wget --waitretry paces retries but only --tries enables them: with no
        # --tries, wget makes exactly one attempt.
        "wget --waitretry=10 https://example.invalid/x -O /tmp/x",
        # curl --retry-delay / --retry-max-time tune a retry that --retry never
        # enabled — the fetch is still one-shot.
        "curl --retry-delay 5 -o /tmp/x https://example.invalid/x",
        "curl --retry-max-time 30 -o /tmp/x https://example.invalid/x",
    ],
)
def test_tuning_only_flags_alone_are_still_flagged(command: str) -> None:
    """A companion flag without its enabling flag must not read as retried —
    that is exactly the false negative a guard for this class must not have."""
    assert not is_retried(command)
    findings = scan_text(command, "f.sh")
    assert len(findings) == 1
    # The message must be the generic "add a retry" remediation, NOT the
    # "add --retry-all-errors" variant — the command has no --retry to complete.
    assert "with-retry.sh" in findings[0].message
    assert "Add `--retry-all-errors`" not in findings[0].message


# ── Per-segment evaluation: one command's flags do not excuse a sibling ────────


def test_complete_retry_does_not_mask_incomplete_sibling() -> None:
    """`--retry-all-errors` on the second curl must not suppress the finding
    for the first — a 503 on the first download still fails the job."""
    text = (
        "curl --retry 5 -o /tmp/a https://example.invalid/a && "
        "curl --retry 5 --retry-all-errors -o /tmp/b https://example.invalid/b"
    )
    findings = scan_text(text, "f.sh")
    assert len(findings) == 1
    assert "--retry-all-errors" in findings[0].message


def test_two_complete_curls_on_one_line_stay_clean() -> None:
    text = (
        "curl --retry 5 --retry-all-errors -o /tmp/a https://example.invalid/a && "
        "curl --retry 5 --retry-all-errors -o /tmp/b https://example.invalid/b"
    )
    assert scan_text(text, "f.sh") == []


def test_retried_sibling_does_not_excuse_an_unretried_download() -> None:
    """Flags are scoped to their own segment: a retried wget cannot satisfy a
    bare curl in the next command of the same logical line."""
    text = (
        "wget --tries=5 -O /tmp/a https://example.invalid/a && "
        "curl -o /tmp/b https://example.invalid/b"
    )
    assert len(scan_text(text, "f.sh")) == 1


def test_wrapper_in_one_segment_does_not_cover_a_sibling_fetcher() -> None:
    """The wrapper wraps the command it invokes — a curl in the NEXT segment
    is not re-run by it."""
    text = (
        "with-retry.sh wget -O /tmp/a https://example.invalid/a && "
        "curl -o /tmp/b https://example.invalid/b"
    )
    assert len(scan_text(text, "f.sh")) == 1


def test_pipe_into_tar_is_one_segment_and_stays_clean_when_complete() -> None:
    """The pipe split must not orphan the fetcher's flags from its download:
    `curl --retry … | tar xz` is one command for retry purposes."""
    text = (
        "curl --retry 5 --retry-all-errors -fsSL https://get.helm.sh/x.tar.gz | tar xz"
    )
    assert scan_text(text, "f.sh") == []


# ── curl --retry without --retry-all-errors ────────────────────────────────────


def test_curl_retry_without_retry_all_errors_is_incomplete() -> None:
    """Plain --retry covers transport errors but NOT an HTTP 503, which is the
    exact failure this rule exists for."""
    command = "curl --retry 5 -o /tmp/x https://example.invalid/x"
    assert is_retried(command)
    assert not retry_is_complete(command)
    findings = scan_text(command, "f.sh")
    assert len(findings) == 1
    assert "--retry-all-errors" in findings[0].message


def test_curl_with_retry_all_errors_is_complete() -> None:
    command = "curl --retry 5 --retry-all-errors -o /tmp/x https://example.invalid/x"
    assert retry_is_complete(command)
    assert scan_text(command, "f.sh") == []


def test_wget_does_not_need_retry_all_errors() -> None:
    """--retry-all-errors is a curl flag; requiring it of wget would be wrong."""
    command = "wget --tries=5 https://example.invalid/x -O /tmp/x"
    assert retry_is_complete(command)
    assert scan_text(command, "f.sh") == []


def test_wrapper_satisfies_completeness_without_retry_all_errors() -> None:
    """The wrapper re-runs the whole command on any non-zero exit, so it covers
    a 503 regardless of which curl flags are present."""
    command = "with-retry.sh curl -fsSL -o /tmp/x https://example.invalid/x"
    assert retry_is_complete(command)


# ── uv python install ──────────────────────────────────────────────────────────


def test_uv_python_install_is_flagged() -> None:
    """The download that started the incident behind this rule."""
    findings = scan_text("- run: uv python install 3.12\n", "w.yaml")
    assert len(findings) == 1
    assert findings[0].rule_id == RULE_ID
    assert "setup-deps" in findings[0].message


def test_uv_python_install_under_the_wrapper_is_not_flagged() -> None:
    text = "with-retry.sh uv python install 3.12\n"
    assert scan_text(text, "w.yaml") == []


def test_uv_sync_is_not_a_python_install() -> None:
    assert scan_text("uv sync --all-extras\n", "w.yaml") == []


# ── Scanning ───────────────────────────────────────────────────────────────────


def test_comment_lines_are_ignored() -> None:
    text = "# curl -o /tmp/pkl https://example.invalid/pkl\n"
    assert scan_text(text, "w.yaml") == []


def test_finding_points_at_the_url_and_the_rule() -> None:
    text = 'curl -fsSL -o /tmp/pkl "https://github.com/apple/pkl/releases/download/0.27.2/pkl-linux-amd64"\n'
    findings = scan_text(text, ".github/workflows/w.yaml")
    assert len(findings) == 1
    assert findings[0].rule_id == RULE_ID
    assert findings[0].file == ".github/workflows/w.yaml"
    assert "github.com/apple/pkl" in findings[0].message
    assert "with-retry.sh" in findings[0].message


def test_multiple_downloads_each_get_a_finding() -> None:
    text = (
        "curl -o /tmp/a https://example.invalid/a\n"
        "curl -o /tmp/b https://example.invalid/b\n"
    )
    assert len(scan_text(text, "f.sh")) == 2


def test_dockerfile_run_lines_are_scanned() -> None:
    """A build-time RUN curl is the same exposure as a job-time one — it failed
    the same way in the incident behind this rule."""
    text = (
        "ARG DAPR_CLI_VERSION=1.16.5\n"
        "RUN curl -fsSL https://raw.githubusercontent.com/dapr/cli/master/install/install.sh \\\n"
        '        | DAPR_INSTALL_DIR="/usr/local/bin" /bin/bash -s ${DAPR_CLI_VERSION} \\\n'
        "    && dapr init --slim\n"
    )
    findings = scan_text(text, "Dockerfile")
    assert len(findings) == 1
    assert findings[0].line == 2
