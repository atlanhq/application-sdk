"""Unit tests for ``application_sdk.dev.pkl``.

Covers the platform→asset mapping, the caching/retry behaviour of the download
(stubbed — no binary is ever fetched), version parsing, and each CLI subcommand.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.dev import pkl as pkl_mod
from application_sdk.dev._pkl_errors import (
    PklDownloadError,
    UnsupportedPklPlatformError,
)
from application_sdk.pkl_version import PKL_VERSION


class TestPlatformKey:
    """The asset names are enumerated, not templated: Linux/macOS spell 64-bit
    ARM ``aarch64`` (Dapr spells the same thing ``arm64``) and Windows publishes
    amd64 only."""

    @pytest.mark.parametrize(
        ("sys_name", "machine", "expected_asset"),
        [
            ("Linux", "x86_64", "pkl-linux-amd64"),
            ("Linux", "aarch64", "pkl-linux-aarch64"),
            ("Darwin", "arm64", "pkl-macos-aarch64"),
            ("Darwin", "x86_64", "pkl-macos-amd64"),
            ("Windows", "AMD64", "pkl-windows-amd64.exe"),
        ],
    )
    def test_supported_platforms(
        self, sys_name: str, machine: str, expected_asset: str
    ) -> None:
        with (
            patch("platform.system", return_value=sys_name),
            patch("platform.machine", return_value=machine),
        ):
            assert pkl_mod.asset_name() == expected_asset

    def test_unsupported_arch_raises(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="riscv64"),
            pytest.raises(UnsupportedPklPlatformError),
        ):
            pkl_mod.platform_key()

    def test_unsupported_os_raises(self) -> None:
        with (
            patch("platform.system", return_value="FreeBSD"),
            patch("platform.machine", return_value="x86_64"),
            pytest.raises(UnsupportedPklPlatformError),
        ):
            pkl_mod.platform_key()

    def test_arm64_windows_has_no_asset(self) -> None:
        # pkl publishes no aarch64 Windows build. The arch normalises fine, so
        # only the (os, arch) lookup catches it — a templated asset name would
        # have produced a URL that 404s at download time instead.
        with (
            patch("platform.system", return_value="Windows"),
            patch("platform.machine", return_value="ARM64"),
            pytest.raises(UnsupportedPklPlatformError),
        ):
            pkl_mod.platform_key()


class TestAssetUrl:
    def test_url_names_the_pinned_version(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
        ):
            url = pkl_mod.asset_url()
        assert url == (
            f"https://github.com/apple/pkl/releases/download/{PKL_VERSION}/pkl-linux-amd64"
        )

    def test_explicit_version_overrides(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
        ):
            assert "0.28.2" in pkl_mod.asset_url("0.28.2")


class TestEnsurePkl:
    def test_downloads_once_then_reuses_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pkl_mod, "_cache_dir", lambda version: tmp_path / version)
        calls: list[tuple[str, Path]] = []

        def fake_download(url: str, target: Path) -> None:
            calls.append((url, target))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.setattr(pkl_mod, "_download", fake_download)
        first = pkl_mod.ensure_pkl("0.32.1")
        second = pkl_mod.ensure_pkl("0.32.1")
        assert first == second == tmp_path / "0.32.1" / "pkl"
        assert len(calls) == 1

    def test_cache_is_keyed_by_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bump must be a fresh download, not an overwrite — otherwise two
        # pins cannot coexist and a stale binary answers for the new version.
        monkeypatch.setattr(pkl_mod, "_cache_dir", lambda version: tmp_path / version)
        monkeypatch.setattr(
            pkl_mod,
            "_download",
            lambda url, target: (
                target.parent.mkdir(parents=True, exist_ok=True),
                target.write_text("x", encoding="utf-8"),
            )
            and None,
        )
        a = pkl_mod.ensure_pkl("0.27.2")
        b = pkl_mod.ensure_pkl("0.32.1")
        assert a != b
        assert a.parent.name == "0.27.2"
        assert b.parent.name == "0.32.1"


class TestDownload:
    def test_marks_the_binary_executable_and_renames_into_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "cache" / "pkl"

        def fake_urlretrieve(url: str, filename: str) -> None:
            Path(filename).write_text("binary", encoding="utf-8")

        monkeypatch.setattr(pkl_mod.urllib.request, "urlretrieve", fake_urlretrieve)
        pkl_mod._download("https://example.invalid/pkl", target)
        assert target.read_text(encoding="utf-8") == "binary"
        assert target.stat().st_mode & stat.S_IXUSR
        # No .part left behind: a truncated fetch must not survive as a
        # cached binary every later run would execute.
        assert not list(target.parent.glob("*.part"))

    def test_retries_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pkl_mod.time, "sleep", lambda *_a: None)
        attempts = {"n": 0}

        def flaky(url: str, filename: str) -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("503 from the release CDN")
            Path(filename).write_text("binary", encoding="utf-8")

        monkeypatch.setattr(pkl_mod.urllib.request, "urlretrieve", flaky)
        pkl_mod._download("https://example.invalid/pkl", tmp_path / "pkl")
        assert attempts["n"] == 3

    def test_raises_typed_error_when_retries_exhaust(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pkl_mod.time, "sleep", lambda *_a: None)
        monkeypatch.setattr(
            pkl_mod.urllib.request,
            "urlretrieve",
            MagicMock(side_effect=OSError("503")),
        )
        with pytest.raises(PklDownloadError) as exc:
            pkl_mod._download("https://example.invalid/pkl", tmp_path / "pkl")
        assert exc.value.attempts == pkl_mod._DOWNLOAD_MAX_ATTEMPTS
        assert not (tmp_path / "pkl").exists()


class TestVersionReading:
    @pytest.mark.parametrize(
        ("banner", "expected"),
        [
            ("Pkl 0.32.1 (macOS 26.4, native)", "0.32.1"),
            ("Pkl 0.27.2 (Linux 6.8, native)", "0.27.2"),
            ("0.32.1", "0.32.1"),
            ("Pkl 0.33.0-rc.1 (Linux, native)", "0.33.0-rc.1"),
        ],
    )
    def test_parse_version(self, banner: str, expected: str) -> None:
        assert pkl_mod.parse_version(banner) == expected

    def test_parse_version_returns_none_when_absent(self) -> None:
        assert pkl_mod.parse_version("command not found") is None

    def test_runtime_version_none_when_binary_missing(self) -> None:
        with patch("subprocess.run", side_effect=OSError):
            assert pkl_mod.runtime_version("pkl") is None

    def test_runtime_version_none_on_nonzero_exit(self) -> None:
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ):
            assert pkl_mod.runtime_version("pkl") is None

    def test_runtime_version_reads_stdout(self) -> None:
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="Pkl 0.32.1 (Linux, native)"),
        ):
            assert pkl_mod.runtime_version("pkl") == "0.32.1"


class TestCli:
    def test_print_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert pkl_mod.main(["print-version"]) == 0
        assert capsys.readouterr().out.strip() == PKL_VERSION

    def test_path_prints_the_cached_binary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(pkl_mod, "ensure_pkl", lambda: tmp_path / "pkl")
        assert pkl_mod.main(["path"]) == 0
        assert capsys.readouterr().out.strip() == str(tmp_path / "pkl")

    def test_path_puts_nothing_but_the_path_on_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Download progress goes to stderr, never stdout.

        The documented call shape captures stdout —
        ``PKL="$(python -m application_sdk.dev.pkl path)"`` — so one progress
        line on stdout makes the captured value a multi-line string and the
        next command fails with a path that does not exist. The SDK logger
        writes to stdout (right for app runtime, where the collector reads it
        for OTel), which is why this module reports on stderr instead.
        """
        binary = tmp_path / "0.32.1" / "pkl"

        def fake_ensure() -> Path:
            pkl_mod._progress("Downloading pkl 0.32.1 for linux/amd64 …")
            pkl_mod._progress(f"pkl 0.32.1 cached at {binary}")
            return binary

        monkeypatch.setattr(pkl_mod, "ensure_pkl", fake_ensure)
        assert pkl_mod.main(["path"]) == 0
        captured = capsys.readouterr()
        assert captured.out == f"{binary}\n"
        assert "Downloading pkl" in captured.err

    def test_run_forwards_args_and_returns_the_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = tmp_path / "pkl"
        monkeypatch.setattr(pkl_mod, "ensure_pkl", lambda: binary)
        seen: list[list[str]] = []

        def fake_run(cmd: list[str]) -> MagicMock:
            seen.append(cmd)
            return MagicMock(returncode=3)

        monkeypatch.setattr(pkl_mod.subprocess, "run", fake_run)
        code = pkl_mod.main(
            ["run", "--", "eval", "--project-dir", "contract", "contract/app.pkl"]
        )
        assert code == 3
        # The `--` separator is consumed, not handed to pkl as an argument.
        assert seen == [
            [str(binary), "eval", "--project-dir", "contract", "contract/app.pkl"]
        ]

    def test_run_without_the_separator_also_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = tmp_path / "pkl"
        monkeypatch.setattr(pkl_mod, "ensure_pkl", lambda: binary)
        seen: list[list[str]] = []
        monkeypatch.setattr(
            pkl_mod.subprocess,
            "run",
            lambda cmd: (seen.append(cmd), MagicMock(returncode=0))[1],
        )
        assert pkl_mod.main(["run", "eval", "contract/app.pkl"]) == 0
        assert seen == [[str(binary), "eval", "contract/app.pkl"]]

    def test_check_passes_when_path_pkl_matches(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(pkl_mod, "runtime_version", lambda binary: PKL_VERSION)
        assert pkl_mod.main(["check"]) == 0
        assert "matches the SDK pin" in capsys.readouterr().out

    def test_check_fails_on_skew(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(pkl_mod, "runtime_version", lambda binary: "0.27.2")
        assert pkl_mod.main(["check"]) == 1
        err = capsys.readouterr().err
        assert "version skew" in err
        assert "0.27.2" in err and PKL_VERSION in err

    def test_check_warn_reports_skew_without_failing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --warn exists so wiring the check into an app's `poe generate` cannot
        # break the very command the developer is running.
        monkeypatch.setattr(pkl_mod, "runtime_version", lambda binary: "0.27.2")
        assert pkl_mod.main(["check", "--warn"]) == 0
        assert "version skew" in capsys.readouterr().err

    def test_check_reports_absent_pkl_separately_from_skew(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(pkl_mod, "runtime_version", lambda binary: None)
        assert pkl_mod.main(["check"]) == 1
        err = capsys.readouterr().err
        assert "not found on PATH" in err
        assert "skew" not in err
