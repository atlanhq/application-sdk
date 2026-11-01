"""Tests for .github/scripts/sync_marketplace_tags.py."""

from __future__ import annotations

import http.server
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sync_marketplace_tags  # noqa: E402

BODY = '{"app_id":"019c","name":"gcs","display_name":"GCS","app_type":"connector","tags":["google"]}'


class FakePost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.result: tuple[int, str] = (200, '{"app_id":"019c"}')

    def __call__(self, url: str, body: str, token: str) -> tuple[int, str]:
        self.calls.append((url, body, token))
        return self.result


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePost:
    stub = FakePost()
    monkeypatch.setattr(sync_marketplace_tags, "post", stub)
    return stub


def test_posts_body_to_apps_route(fake):
    line = sync_marketplace_tags.sync("https://t.atlan.com/", "tok", BODY)
    # Trailing slash on base_url must not produce a double slash.
    assert fake.calls == [
        ("https://t.atlan.com/api/service/marketplace/apps", BODY, "tok")
    ]
    assert line.startswith("Synced marketplace tags")


def test_empty_body_makes_no_call(fake):
    assert sync_marketplace_tags.sync("https://t.atlan.com", "tok", "") == (
        "No tags in atlan.yaml; nothing to sync."
    )
    assert fake.calls == []


@pytest.mark.parametrize(("status", "label"), [(422, "HTTP 422"), (0, "HTTP error")])
def test_failure_is_a_warning_not_an_error(fake, status, label):
    fake.result = (status, "boom")
    line = sync_marketplace_tags.sync("https://t.atlan.com", "tok", BODY)
    assert line.startswith("::warning title=Tag sync failed::")
    assert label in line and "boom" in line


def test_main_exits_zero_and_skips_summary_on_failure(
    fake, monkeypatch, tmp_path, capsys
):
    fake.result = (500, "down")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("BASE_URL", "https://t.atlan.com")
    monkeypatch.setenv("APP_PUBLISH_TOKEN", "tok")
    monkeypatch.setenv("APP_SYNC_BODY", BODY)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    sync_marketplace_tags.main()  # must not raise
    assert "::warning" in capsys.readouterr().out
    assert not summary.exists()


# ── real urllib path, against a loopback server ─────────────────────────────
# These exercise ``post`` itself, not a stub: redirect following and status-line
# parsing are urllib/http.client behaviour, which only a real response proves.


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def log_message(self, *args: object) -> None:  # keep pytest output quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        _Handler.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        _Handler.seen.append((self.path, self.headers.get("Authorization")))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/redirect/api/service/marketplace/apps":
            # 302, not 307: urllib already refuses a 307 on POST, but follows a
            # 302 as a GET that still carries the Authorization header.
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/garbage/api/service/marketplace/apps":
            self.wfile.write(b"NOT-HTTP\r\n\r\n")
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_redirect_is_not_followed_so_token_stays_put(server):
    line = sync_marketplace_tags.sync(server + "/redirect", "tok", BODY)
    assert line.startswith("::warning title=Tag sync failed::")
    assert "HTTP 302" in line
    # One request only: the token never reached the redirect target.
    assert _Handler.seen == [("/redirect/api/service/marketplace/apps", "Bearer tok")]


def test_malformed_status_line_is_a_warning_not_a_crash(server):
    line = sync_marketplace_tags.sync(server + "/garbage", "tok", BODY)
    assert line.startswith("::warning title=Tag sync failed::")
    assert "HTTP error" in line


def test_unusable_base_url_is_a_warning_not_a_crash():
    # Empty BASE_URL → "unknown url type" ValueError from urllib.
    line = sync_marketplace_tags.sync("", "tok", BODY)
    assert line.startswith("::warning title=Tag sync failed::")


def test_success_against_real_server(server):
    assert sync_marketplace_tags.sync(server, "tok", BODY).startswith(
        "Synced marketplace tags"
    )
