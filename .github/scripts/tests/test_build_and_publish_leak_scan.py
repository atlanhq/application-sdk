"""Tests for .github/scripts/build_and_publish/leak_scan.py."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))

# The scripts import each other as top-level modules (`import _bap`), so the
# tests do too: that keeps one module object for monkeypatching. pyright
# resolves them through the package path instead.
if TYPE_CHECKING:
    from build_and_publish import _bap, leak_scan
else:
    import _bap
    import leak_scan

WEBHOOK_SECRET_PATH = "/services/T0SECRET/B0SECRET/x0secret"
WEBHOOK = "https://hooks.slack.com" + WEBHOOK_SECRET_PATH


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    files = {"output": tmp_path / "out", "summary": tmp_path / "summary"}
    for f in files.values():
        f.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(files["output"]))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(files["summary"]))
    monkeypatch.setenv("REPO", "atlanhq/demo-app")
    monkeypatch.setenv("RUN_URL", "https://run")
    return files


class Recorder:
    def __init__(
        self, on_call: Callable[[list[str], dict[str, object]], int] | None = None
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.on_call = on_call

    def __call__(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(cmd), dict(kwargs)))
        rc = self.on_call(list(cmd), dict(kwargs)) if self.on_call else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")


def _install(monkeypatch: pytest.MonkeyPatch, rec: Recorder) -> Recorder:
    monkeypatch.setattr(_bap, "run", rec)
    return rec


# ── gitleaks ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "app"
    for rel in (
        "src/main.py",
        "tests/fixture.py",
        "test/x.py",
        ".github/ci.yaml",
        ".git/HEAD",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")
    monkeypatch.chdir(root)
    return root


def _gitleaks_writing(
    report: Path, findings: object, rc: int
) -> Callable[[list[str], dict[str, object]], int]:
    def on_call(cmd: list[str], kwargs: dict) -> int:
        report.write_text(json.dumps(findings))
        return rc

    return on_call


def test_clean_scan_passes_on_a_pruned_copy(
    app: Path,
    tmp_path: Path,
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scan, report = tmp_path / "scan", tmp_path / "report.json"
    seen: dict[str, object] = {}

    def on_call(cmd: list[str], kwargs: dict) -> int:
        seen["tree"] = sorted(p.relative_to(scan).as_posix() for p in scan.rglob("*"))
        report.write_text("[]")
        return 0

    rec = _install(monkeypatch, Recorder(on_call))
    assert leak_scan.gitleaks(str(scan), str(report)) == 0
    ((cmd, kwargs),) = rec.calls
    assert cmd == [
        "gitleaks",
        "dir",
        "--redact",
        "--report-format",
        "json",
        "--report-path",
        str(report),
        ".",
    ]
    assert kwargs["cwd"] == str(scan)
    assert seen["tree"] == ["src", "src/main.py"]
    # The app checkout itself keeps its tests for the creds-into-logs layer.
    assert (app / "tests" / "fixture.py").exists()
    assert not (app / ".git").exists()
    assert "gitleaks: no hardcoded secrets found." in capsys.readouterr().out
    assert gh["summary"].read_text() == ""


def test_app_gitleaks_config_is_used(
    app: Path, tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (app / ".gitleaks.toml").write_text("")
    report = tmp_path / "r.json"
    rec = _install(monkeypatch, Recorder(_gitleaks_writing(report, [], 0)))
    assert leak_scan.gitleaks(str(tmp_path / "scan"), str(report)) == 0
    assert rec.calls[0][0][-3:] == ["--config", ".gitleaks.toml", "."]


def test_findings_block_with_locations_only(
    app: Path,
    tmp_path: Path,
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "r.json"
    findings = [
        {
            "File": "src/main.py",
            "StartLine": 3,
            "RuleID": "generic-api-key",
            "Secret": "REDACTED",
        }
    ]
    _install(monkeypatch, Recorder(_gitleaks_writing(report, findings, 1)))
    assert leak_scan.gitleaks(str(tmp_path / "scan"), str(report)) == 1
    summary = gh["summary"].read_text()
    assert summary.startswith(
        "### ❌ Publish blocked — hardcoded secret(s) detected\n\n"
    )
    assert "gitleaks found 1 hardcoded secret(s)" in summary
    assert "- `src/main.py:3` — rule: generic-api-key\n" in summary
    assert "REDACTED" not in summary
    assert "::error title=Publish blocked::gitleaks found 1 hardcoded secret(s)." in (
        capsys.readouterr().out
    )


def test_gitleaks_error_without_report_still_blocks(
    app: Path, tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, Recorder(lambda cmd, kw: 2))
    assert leak_scan.gitleaks(str(tmp_path / "scan"), str(tmp_path / "none.json")) == 1
    assert "gitleaks report unreadable" in gh["summary"].read_text()


@pytest.mark.parametrize("content", [None, "not json", '{"not": "a list"'])
def test_gitleaks_clean_exit_without_a_readable_report_fails_closed(
    app: Path,
    tmp_path: Path,
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    """Exit 0 with no readable report is an incomplete scan, not a clean one."""
    report = tmp_path / "r.json"
    if content is not None:
        report.write_text(content)
    _install(monkeypatch, Recorder(lambda cmd, kw: 0))
    assert leak_scan.gitleaks(str(tmp_path / "scan"), str(report)) == 1
    assert "gitleaks report unreadable" in gh["summary"].read_text()
    assert "the hardcoded-secret scan is incomplete" in capsys.readouterr().out


# ── Slack notifiers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("webhook", "message"),
    [
        ("", "::notice::SLACK_LEAK_SCAN_WEBHOOK not set"),
        (
            "https://evil.example/hook",
            "::warning::SLACK_LEAK_SCAN_WEBHOOK is not a hooks.slack.com URL",
        ),
    ],
)
@pytest.mark.parametrize("command", ["notify-gitleaks", "notify-credscan"])
def test_notifiers_refuse_missing_or_foreign_webhooks(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    webhook: str,
    message: str,
    command: str,
) -> None:
    monkeypatch.setenv("WEBHOOK", webhook)
    rec = _install(monkeypatch, Recorder())
    assert leak_scan.main([command]) == 0
    assert not rec.calls
    assert message in capsys.readouterr().out


def _posted(rec: Recorder) -> tuple[list[str], str]:
    ((cmd, kwargs),) = rec.calls
    # The webhook URL is a credential: it reaches curl on stdin via
    # `--config -`, never in argv.
    assert cmd[-2:] == ["--config", "-"]
    # The secret is the path, so check argv for that, not for the host.
    assert not any(WEBHOOK_SECRET_PATH in arg for arg in cmd)
    assert kwargs["input"] == f'url = "{WEBHOOK}"\n'
    return cmd, json.loads(cmd[cmd.index("--data") + 1])["text"]


def test_notify_gitleaks_posts_top_ten_locations(
    tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "r.json"
    report.write_text(
        json.dumps(
            [
                {
                    "File": f"f{i}.py",
                    "StartLine": i,
                    "RuleID": "r",
                    "Secret": "TOPSECRETVALUE",
                }
                for i in range(12)
            ]
        )
    )
    monkeypatch.setenv("WEBHOOK", WEBHOOK)
    rec = _install(monkeypatch, Recorder())
    assert leak_scan.notify_gitleaks(str(report)) == 0
    cmd, text = _posted(rec)
    assert cmd[:3] == ["curl", "-sS", "--fail"]
    assert text.startswith(
        ":no_entry: *Publish BLOCKED — hardcoded secret(s)*\n"
        "*App repo:* `atlanhq/demo-app`\ngitleaks found 12 secret(s)"
    )
    assert "• `f9.py:9` — r\n<https://run|View workflow run>" in text
    assert "f10.py" not in text and "TOPSECRETVALUE" not in text


def test_notify_gitleaks_without_report_says_unknown(
    tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBHOOK", WEBHOOK)
    rec = _install(monkeypatch, Recorder())
    leak_scan.notify_gitleaks(str(tmp_path / "missing.json"))
    assert "gitleaks found ? secret(s)" in _posted(rec)[1]


def test_slack_failure_is_only_a_warning(
    tmp_path: Path,
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "r.json").write_text("[]")
    monkeypatch.setenv("WEBHOOK", WEBHOOK)
    _install(monkeypatch, Recorder(lambda cmd, kw: 22))
    assert leak_scan.notify_gitleaks(str(tmp_path / "r.json")) == 0
    assert (
        "::warning::Slack notification failed (non-blocking)."
        in capsys.readouterr().out
    )


VERDICT = {
    "summary": {
        "total_findings": 4,
        "blocking": 2,
        "critical": 1,
        "high": 1,
        "medium": 2,
    },
    "findings": [
        {
            "file": "app/a.py",
            "line": 1,
            "pattern_id": "P1",
            "variable_name": "pw",
            "severity": "CRITICAL",
        },
        {
            "file": "app/b.py",
            "line": 2,
            "pattern_id": "P2",
            "variable_name": "tok",
            "severity": "HIGH",
        },
        {
            "file": "app/c.py",
            "line": 3,
            "pattern_id": "P3",
            "variable_name": "x",
            "severity": "MEDIUM",
        },
    ],
}


def test_notify_credscan_posts_critical_and_high_only(
    tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict = tmp_path / "v.json"
    verdict.write_text(json.dumps(VERDICT))
    for key, value in {"WEBHOOK": WEBHOOK, "CRIT": "1", "HIGH": "1"}.items():
        monkeypatch.setenv(key, value)
    rec = _install(monkeypatch, Recorder())
    assert leak_scan.notify_credscan(str(verdict)) == 0
    text = _posted(rec)[1]
    assert text == (
        ":warning: *Credential-leak warning — publish NOT blocked*\n"
        "*App repo:* `atlanhq/demo-app`\n"
        "1 critical, 1 high credential(s) reaching log/CLI sinks — locations below (values withheld):\n"
        "• `app/a.py:1` — P1 (CRITICAL)\n• `app/b.py:2` — P2 (HIGH)\n"
        "<https://run|View workflow run>"
    )


# ── credscan ──────────────────────────────────────────────────────────────────


def _outputs(gh: dict[str, Path]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in gh["output"].read_text().splitlines())


def test_credscan_writes_counts_and_never_fails(
    tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict = tmp_path / "v.json"

    def on_call(cmd: list[str], kwargs: dict) -> int:
        verdict.write_text(json.dumps(VERDICT))
        return 1  # scan.py exits non-zero on CRITICAL/HIGH; advisory here

    rec = _install(monkeypatch, Recorder(on_call))
    assert leak_scan.credscan(str(verdict)) == 0
    cmd = rec.calls[0][0]
    assert cmd[1].endswith(".github/scripts/credential-leak-gate/scan.py")
    assert cmd[2:] == ["--root", "app", "--report", str(verdict)]
    assert _outputs(gh) == {
        "findings": "4",
        "blocking": "2",
        "critical": "1",
        "high": "1",
        "medium": "2",
    }


def test_credscan_missing_summary_fields_default_to_zero(
    tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict = tmp_path / "v.json"
    verdict.write_text('{"summary": {"blocking": null}}')
    _install(monkeypatch, Recorder())
    leak_scan.credscan(str(verdict))
    assert set(_outputs(gh).values()) == {"0"}


@pytest.mark.parametrize("content", [None, "not json"])
def test_credscan_without_a_valid_verdict_reports_zero(
    tmp_path: Path,
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    verdict = tmp_path / "v.json"
    if content is not None:
        verdict.write_text(content)
    _install(monkeypatch, Recorder())
    assert leak_scan.credscan(str(verdict)) == 0
    assert gh["output"].read_text() == "findings=0\nblocking=0\n"
    assert (
        "::warning title=Creds-into-logs scan incomplete::" in capsys.readouterr().out
    )


# ── credscan-report ───────────────────────────────────────────────────────────


def _report_env(monkeypatch: pytest.MonkeyPatch, warn: str) -> None:
    for key, value in {"WARN": warn, "CRIT": "1", "HIGH": "1", "MED": "2"}.items():
        monkeypatch.setenv(key, value)


def test_report_without_warnings(
    tmp_path: Path, gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _report_env(monkeypatch, "0")
    assert leak_scan.credscan_report(str(tmp_path / "unused.json")) == 0
    assert gh["summary"].read_text() == (
        "### ✅ Credential leak gate\n\ngitleaks: ✅ no hardcoded secrets (blocking). "
        "Creds-into-logs: ✅ no warnings (2 medium, non-blocking).\n"
    )


def test_report_with_warnings_lists_locations(
    tmp_path: Path,
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verdict = tmp_path / "v.json"
    verdict.write_text(json.dumps(VERDICT))
    _report_env(monkeypatch, "2")
    assert leak_scan.credscan_report(str(verdict)) == 0
    summary = gh["summary"].read_text()
    assert summary.startswith("### ⚠️ Credential leak warnings (non-blocking)\n")
    assert "Creds-into-logs found 2 likely finding(s) (1 critical, 1 high)." in summary
    assert (
        "- `app/a.py:1` — P1 (var: pw, CRITICAL)\n- `app/b.py:2` — P2 (var: tok, HIGH)\n\n"
        "See docs/standards/credential-leak-gate.md.\n"
    ) in summary
    assert "c.py" not in summary
    assert (
        "::warning title=Credential-leak warnings::2 credential(s)"
        in capsys.readouterr().out
    )
