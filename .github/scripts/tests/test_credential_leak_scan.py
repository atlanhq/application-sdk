"""Tests for .github/scripts/credential-leak-gate/scan.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "credential-leak-gate"))

import scan  # noqa: E402


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _by_file(result: dict) -> dict[str, dict]:
    return {f["file"]: f for f in result["findings"]}


def test_real_credential_in_logger_is_critical_and_blocks(tmp_path: Path) -> None:
    _write(tmp_path, "handler.py", 'logger.info(f"credentials={credentials}")\n')
    result = scan.scan(str(tmp_path))
    assert result["decision"] == "fail"
    assert result["summary"]["blocking"] == 1
    finding = _by_file(result)["handler.py"]
    assert finding["severity"] == "CRITICAL"
    assert finding["pattern_id"] == "logger-call"


def test_prose_log_message_is_not_flagged(tmp_path: Path) -> None:
    # The word "password" appears only inside the message string, never as code.
    _write(tmp_path, "h.py", 'logger.info("password reset email sent to user")\n')
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []
    assert result["decision"] == "pass"


def test_masked_value_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "h.py", 'logger.info("auth %s", mask(password))\n')
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []


def test_metadata_identifier_is_not_a_leak(tmp_path: Path) -> None:
    # credential_name / secret_id are names/ids, not the secret value.
    _write(
        tmp_path,
        "h.py",
        'logger.info(f"credential={cred.credential_name} id={cred.secret_id}")\n',
    )
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []


def test_logging_credential_keys_is_not_a_leak(tmp_path: Path) -> None:
    # Logging key NAMES / shape, not the value — the dominant FP on real apps.
    _write(
        tmp_path,
        "h.py",
        'logger.info(f"keys: {list(credentials.keys())}")\n'
        'logger.debug(f"has pwd: {bool(password)}")\n'
        'logger.error(f"type: {type(credentials)}")\n'
        "logger.info(f\"present: {'credentials' in cfg}\")\n",
    )
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []
    assert result["decision"] == "pass"


def test_logging_raw_credential_object_still_blocks(tmp_path: Path) -> None:
    # The genuine April-audit leak class: the raw object reaches the sink.
    _write(
        tmp_path,
        "h.js",
        'console.log("returning credentials:", credentials)\n',
    )
    result = scan.scan(str(tmp_path))
    assert _by_file(result)["h.js"]["severity"] in ("CRITICAL", "HIGH")
    assert result["decision"] == "fail"


def test_helm_set_is_medium_non_blocking(tmp_path: Path) -> None:
    _write(tmp_path, "deploy.sh", "helm upgrade app --set password=$DB_PASSWORD\n")
    result = scan.scan(str(tmp_path))
    finding = _by_file(result)["deploy.sh"]
    assert finding["severity"] == "MEDIUM"
    assert result["summary"]["blocking"] == 0
    assert result["decision"] == "pass"


def test_password_stdin_pipe_is_not_a_leak(tmp_path: Path) -> None:
    # The recommended secure docker-login idiom: secret piped to stdin, never
    # a log/console sink. Present in most app repos' CI scaffold.
    _write(
        tmp_path,
        "ci/registry-login.sh",
        'echo "$PASSWORD" | docker login "$REG" -u "$USER" --password-stdin\n',
    )
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []
    assert result["decision"] == "pass"


def test_bare_echo_of_secret_still_flagged(tmp_path: Path) -> None:
    # No pipe / stdin — echoes the secret to stdout (CI log). Still a leak.
    _write(tmp_path, "run.sh", 'echo "password=$DB_PASSWORD"\n')
    result = scan.scan(str(tmp_path))
    assert _by_file(result)["run.sh"]["pattern_id"] == "shell-echo"


def test_test_path_is_capped_at_medium(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_conn.py", 'logger.info(f"password={password}")\n')
    result = scan.scan(str(tmp_path))
    finding = _by_file(result)["tests/test_conn.py"]
    assert finding["severity"] == "MEDIUM"
    assert result["summary"]["blocking"] == 0


def test_inline_comment_does_not_fire(tmp_path: Path) -> None:
    _write(tmp_path, "h.py", 'print("hello")  # remember the password=secret\n')
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []


def test_allowlist_line_suppresses_finding(tmp_path: Path) -> None:
    _write(tmp_path, "h.py", 'logger.info(f"secret={secret}")\n')
    _write(tmp_path, ".credential-leak-allow", "h.py:1\n")
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []
    assert result["decision"] == "pass"


def test_allowlist_file_suppresses_whole_file(tmp_path: Path) -> None:
    _write(tmp_path, "h.py", 'logger.info(f"secret={secret}")\n')
    _write(tmp_path, ".credential-leak-allow", "# ignore vault helper\nh.py\n")
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []


def test_skips_vendored_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/x.js", "console.log(`secret=${secret}`)\n")
    result = scan.scan(str(tmp_path))
    assert result["findings"] == []


def test_clean_tree_passes(tmp_path: Path) -> None:
    _write(tmp_path, "h.py", 'logger.info("started worker")\n')
    result = scan.scan(str(tmp_path))
    assert result["decision"] == "pass"
    assert result["summary"]["total_findings"] == 0
