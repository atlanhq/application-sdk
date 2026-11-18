"""Unit tests for ``application_sdk.dev._download``, the release-asset fetcher
shared by the embedded daprd and the pinned pkl. No network: the HTTP layer is
a private ``urllib.request`` copy, and ``fetch_release_asset`` takes a stubbed
``fetch``."""

from __future__ import annotations

import http.client
import io
import os
import ssl
import stat
import threading
import urllib.error
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from application_sdk.dev import _download
from application_sdk.dev._download import (
    fetch_release_asset,
    fetch_to_file,
    is_retryable_network_error,
)
from tests.unit.private_stdlib import private_stdlib


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_length: int | None) -> None:
        super().__init__(body)
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)


class TestFetchToFile:
    @pytest.fixture
    def urlopen_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[list[tuple[str, dict[str, Any]]]]:
        calls: list[tuple[str, dict[str, Any]]] = []
        self.response = _FakeResponse(b"binary", 6)

        def fake_urlopen(url: str, **kwargs: Any) -> _FakeResponse:
            calls.append((url, kwargs))
            return self.response

        with (
            private_stdlib(_download, "urllib") as local_urllib,
            private_stdlib(local_urllib, "request") as local_request,
        ):
            monkeypatch.setattr(local_request, "urlopen", fake_urlopen)
            yield calls

    def test_passes_the_timeout_and_writes_the_body(
        self, tmp_path: Path, urlopen_calls: list[tuple[str, dict[str, Any]]]
    ) -> None:
        dest = tmp_path / "asset"
        fetch_to_file("https://example.invalid/a", dest, timeout=12.5)
        assert urlopen_calls == [("https://example.invalid/a", {"timeout": 12.5})]
        assert dest.read_bytes() == b"binary"

    def test_body_shorter_than_content_length_is_incomplete(
        self, tmp_path: Path, urlopen_calls: list[tuple[str, dict[str, Any]]]
    ) -> None:
        """``HTTPResponse`` ends a length-delimited body quietly when the peer
        closes early; the check turns that into a retryable error."""
        self.response = _FakeResponse(b"bin", 6)
        with pytest.raises(http.client.IncompleteRead) as exc_info:
            fetch_to_file("https://example.invalid/a", tmp_path / "a", timeout=1)
        assert is_retryable_network_error(exc_info.value)

    def test_missing_content_length_is_accepted(
        self, tmp_path: Path, urlopen_calls: list[tuple[str, dict[str, Any]]]
    ) -> None:
        self.response = _FakeResponse(b"binary", None)
        fetch_to_file("https://example.invalid/a", tmp_path / "a", timeout=1)
        assert (tmp_path / "a").read_bytes() == b"binary"


class TestIsRetryableNetworkError:
    @pytest.mark.parametrize(
        ("code", "retryable"),
        [
            (500, True),
            (502, True),
            (503, True),
            (429, True),
            (404, False),
            (403, False),
        ],
    )
    def test_http_status(self, code: int, retryable: bool) -> None:
        err = urllib.error.HTTPError("u", code, "x", Message(), None)
        assert is_retryable_network_error(err) is retryable

    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError("dns"),
            ConnectionResetError("reset"),
            TimeoutError("stalled"),
            http.client.IncompleteRead(b"", 10),
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
        ],
    )
    def test_network_failures_are_retryable(self, exc: BaseException) -> None:
        assert is_retryable_network_error(exc)

    @pytest.mark.parametrize("exc", [PermissionError("disk"), ValueError("bug")])
    def test_local_failures_are_not(self, exc: BaseException) -> None:
        assert not is_retryable_network_error(exc)


def _write(body: bytes = b"binary"):
    def fetch(_url: str, dest: Path) -> None:
        dest.write_bytes(body)

    return fetch


class TestFetchReleaseAsset:
    def test_publishes_an_executable_binary_and_leaves_no_temp_files(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "cache" / "tool"
        fetch_release_asset("u", dest, backoff_seconds=0, fetch=_write())
        assert dest.read_bytes() == b"binary"
        if os.name != "nt":
            assert dest.stat().st_mode & stat.S_IXUSR
        assert sorted(p.name for p in dest.parent.iterdir()) == ["tool"]

    def test_extract_turns_the_download_into_the_binary(self, tmp_path: Path) -> None:
        dest = tmp_path / "tool"

        def extract(downloaded: Path, staged: Path) -> None:
            staged.write_bytes(downloaded.read_bytes().upper())

        fetch_release_asset(
            "u", dest, backoff_seconds=0, fetch=_write(), extract=extract
        )
        assert dest.read_bytes() == b"BINARY"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["tool"]

    def test_retries_a_retryable_failure_and_reports_it(self, tmp_path: Path) -> None:
        calls: list[int] = []
        reported: list[tuple[int, int, str]] = []

        def flaky(url: str, dest: Path) -> None:
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionResetError("reset")
            dest.write_bytes(b"binary")

        fetch_release_asset(
            "u",
            tmp_path / "tool",
            backoff_seconds=0,
            fetch=flaky,
            on_retry=lambda a, n, e: reported.append((a, n, type(e).__name__)),
        )
        assert len(calls) == 3
        assert reported == [
            (1, 3, "ConnectionResetError"),
            (2, 3, "ConnectionResetError"),
        ]

    def test_a_client_error_is_raised_after_one_attempt(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def not_found(url: str, _dest: Path) -> None:
            calls.append(1)
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

        with pytest.raises(urllib.error.HTTPError):
            fetch_release_asset(
                "u", tmp_path / "tool", backoff_seconds=0, fetch=not_found
            )
        assert len(calls) == 1
        assert list(tmp_path.iterdir()) == []

    def test_exhausted_retries_raise_the_last_error_and_publish_nothing(
        self, tmp_path: Path
    ) -> None:
        calls: list[int] = []

        def partial_then_fail(_url: str, dest: Path) -> None:
            calls.append(1)
            dest.write_bytes(b"bin")
            raise TimeoutError("stalled")

        with pytest.raises(TimeoutError):
            fetch_release_asset(
                "u",
                tmp_path / "tool",
                attempts=2,
                backoff_seconds=0,
                fetch=partial_then_fail,
            )
        assert len(calls) == 2
        assert list(tmp_path.iterdir()) == []

    def test_retryable_decides_which_extract_errors_are_retried(
        self, tmp_path: Path
    ) -> None:
        calls: list[int] = []

        def extract(downloaded: Path, staged: Path) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise EOFError("archive cut short")
            staged.write_bytes(downloaded.read_bytes())

        fetch_release_asset(
            "u",
            tmp_path / "tool",
            backoff_seconds=0,
            fetch=_write(),
            extract=extract,
            retryable=lambda e: isinstance(e, EOFError),
        )
        assert len(calls) == 2
        assert (tmp_path / "tool").read_bytes() == b"binary"

    def test_concurrent_calls_for_one_target_do_not_share_staging_files(
        self, tmp_path: Path
    ) -> None:
        """Two threads filling the same missing cache entry each stage to their
        own file: with a shared per-process name, one thread's cleanup unlinks
        the file the other is still writing."""
        dest = tmp_path / "tool"
        both_fetching = threading.Barrier(2, timeout=10)
        staged_at: list[Path] = []
        errors: list[BaseException] = []

        def fetch(_url: str, path: Path) -> None:
            staged_at.append(path)
            path.write_bytes(b"binary")
            both_fetching.wait()

        def run() -> None:
            try:
                fetch_release_asset("u", dest, backoff_seconds=0, fetch=fetch)
            except BaseException as exc:  # noqa: BLE001 — surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert len(set(staged_at)) == 2
        assert dest.read_bytes() == b"binary"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["tool"]


class TestPublish:
    """The lost race on Windows: ``os.replace`` onto a target another writer is
    replacing at that moment is access-denied. Simulated on every platform by a
    private ``os`` whose ``replace`` raises after the winner has published."""

    def _denied_after(self, winner: bytes):
        def replace(_src: object, dst: object) -> None:
            Path(str(dst)).write_bytes(winner)
            raise PermissionError(13, "Access is denied")

        return replace

    def test_windows_loser_accepts_the_winners_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staged, dest = tmp_path / "staged", tmp_path / "tool"
        staged.write_bytes(b"binary")
        with private_stdlib(_download, "os") as local_os:
            monkeypatch.setattr(local_os, "name", "nt")
            monkeypatch.setattr(local_os, "replace", self._denied_after(b"binary"))
            _download._publish(staged, dest)
        assert dest.read_bytes() == b"binary"

    def test_posix_permission_error_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX has no such race, so a denial there is a real permissions
        problem and must not be hidden by an existing file."""
        staged, dest = tmp_path / "staged", tmp_path / "tool"
        staged.write_bytes(b"binary")
        with private_stdlib(_download, "os") as local_os:
            monkeypatch.setattr(local_os, "name", "posix")
            monkeypatch.setattr(local_os, "replace", self._denied_after(b"old"))
            with pytest.raises(PermissionError):
                _download._publish(staged, dest)

    def test_windows_denial_with_nothing_published_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staged, dest = tmp_path / "staged", tmp_path / "tool"
        staged.write_bytes(b"binary")

        def replace(_src: object, _dst: object) -> None:
            raise PermissionError(13, "Access is denied")

        with private_stdlib(_download, "os") as local_os:
            monkeypatch.setattr(local_os, "name", "nt")
            monkeypatch.setattr(local_os, "replace", replace)
            with pytest.raises(PermissionError):
                _download._publish(staged, dest)
