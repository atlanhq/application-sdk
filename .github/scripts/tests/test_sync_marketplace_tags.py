"""Tests for .github/scripts/sync_marketplace_tags.py."""

from __future__ import annotations

import sys
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
