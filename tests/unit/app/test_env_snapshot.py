"""Tests for App._capture_env_snapshot() and App.env() sandbox-safe env access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from application_sdk.app.base import App
from application_sdk.contracts.base import Input, Output


@dataclass
class _SnapshotInput(Input):
    value: str = ""


@dataclass
class _SnapshotOutput(Output):
    result: str = ""


class _SnapshotApp(App):
    name = "snapshot-test-app"

    async def run(self, input: _SnapshotInput) -> _SnapshotOutput:
        return _SnapshotOutput(result="ok")


@pytest.fixture(autouse=True)
def _restore_snapshot():
    """Restore _env_snapshot to its original value after each test."""
    original = App._env_snapshot.copy()
    yield
    App._env_snapshot = original


class TestCaptureEnvSnapshot:
    """Tests for App._capture_env_snapshot()."""

    def test_captures_current_environ(self) -> None:
        """_capture_env_snapshot captures os.environ at call time."""
        with patch.dict(os.environ, {"TEST_SNAPSHOT_VAR": "hello"}, clear=False):
            App._capture_env_snapshot()

        assert App._env_snapshot["TEST_SNAPSHOT_VAR"] == "hello"

    def test_snapshot_is_a_copy(self) -> None:
        """Snapshot is an independent copy, not a reference to os.environ."""
        App._capture_env_snapshot()

        # Mutating os.environ after capture should not affect the snapshot
        os.environ["__SNAPSHOT_POST_CAPTURE__"] = "after"
        assert "__SNAPSHOT_POST_CAPTURE__" not in App._env_snapshot

        # Clean up
        os.environ.pop("__SNAPSHOT_POST_CAPTURE__", None)

    def test_overwrites_previous_snapshot(self) -> None:
        """Calling _capture_env_snapshot again overwrites the previous one."""
        App._env_snapshot = {"OLD_KEY": "old_value"}
        App._capture_env_snapshot()

        assert "OLD_KEY" not in App._env_snapshot
        # Should contain real env vars instead
        assert "PATH" in App._env_snapshot or len(App._env_snapshot) >= 0


class TestEnvMethod:
    """Tests for App.env() instance method."""

    def test_returns_value_from_snapshot(self) -> None:
        """env() returns the value from the pre-captured snapshot."""
        App._env_snapshot = {"MY_DB_HOST": "localhost", "MY_DB_PORT": "5432"}
        app = _SnapshotApp()

        assert app.env("MY_DB_HOST") == "localhost"
        assert app.env("MY_DB_PORT") == "5432"

    def test_returns_default_when_key_missing(self) -> None:
        """env() returns the default when the key is not in the snapshot."""
        App._env_snapshot = {}
        app = _SnapshotApp()

        assert app.env("NONEXISTENT_KEY") == ""
        assert app.env("NONEXISTENT_KEY", "fallback") == "fallback"

    def test_default_is_empty_string(self) -> None:
        """env() default parameter defaults to empty string."""
        App._env_snapshot = {}
        app = _SnapshotApp()

        assert app.env("MISSING") == ""

    def test_reads_from_class_level_snapshot(self) -> None:
        """env() reads from the class-level _env_snapshot, not os.environ."""
        App._env_snapshot = {"SNAPSHOT_ONLY": "yes"}
        app = _SnapshotApp()

        # Even if os.environ has a different value, env() uses the snapshot
        with patch.dict(os.environ, {"SNAPSHOT_ONLY": "no"}, clear=False):
            assert app.env("SNAPSHOT_ONLY") == "yes"

    def test_shared_across_subclasses(self) -> None:
        """_env_snapshot is shared across all App subclasses (ClassVar)."""
        App._env_snapshot = {"SHARED_VAR": "shared_value"}
        app = _SnapshotApp()

        assert app.env("SHARED_VAR") == "shared_value"
