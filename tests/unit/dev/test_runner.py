"""Unit tests for ``run_dev_combined`` (``application_sdk.dev._runner``).

Moved from ``tests/unit/test_main.py`` with the code (FND-4689). ``run_combined_mode``
is still patched on ``application_sdk.main``: the runner resolves it from there at
call time."""

from __future__ import annotations

import warnings
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.main import run_dev_combined
from tests.unit.private_stdlib import private_stdlib


def _fake_app_class() -> mock.MagicMock:
    """Build a fake App class with attributes the run modes read."""
    cls = mock.MagicMock()
    cls._app_name = "fake-app"
    cls._app_version = "1.0.0"
    cls.__name__ = "FakeApp"
    cls.__module__ = "fake_pkg.fake_app"
    return cls


# --------------------------------------------------------------------------- #
# run_dev_combined                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _stub_embedded_daemons():
    """Replace the daprd + Temporal context managers with no-ops.

    ``run_dev_combined`` unconditionally wraps its body in ``embedded_dapr``
    and ``embedded_runtime``; without this stub each unit test would try to
    download daprd from the internet and spawn subprocesses. We swap in
    minimal async context managers that yield the dataclasses the body
    expects but do no actual work.
    """
    from contextlib import asynccontextmanager

    from application_sdk.dev._dapr import EmbeddedDapr
    from application_sdk.dev._embedded import EmbeddedRuntime

    captured_runtime_kwargs: list[dict[str, object]] = []

    @asynccontextmanager
    async def _fake_dapr(**_kwargs):
        yield EmbeddedDapr(
            http_port=3500, grpc_port=50001, components_dir="/tmp/fake-components"
        )

    @asynccontextmanager
    async def _fake_runtime(**_kwargs):
        captured_runtime_kwargs.append(_kwargs)
        temporal_ui = bool(_kwargs.get("temporal_ui", False))
        temporal_ui_port = _kwargs.get("temporal_ui_port")
        resolved_ui_port = temporal_ui_port if temporal_ui_port is not None else 8233
        ui_url = f"http://127.0.0.1:{resolved_ui_port}" if temporal_ui else None
        yield EmbeddedRuntime(host="127.0.0.1:7233", namespace="default", ui_url=ui_url)

    # ``run_dev_combined`` does ``from application_sdk.dev import embedded_dapr,
    # embedded_runtime`` lazily inside the function body, so patch them on the
    # source module — not on ``application_sdk.dev._runner``.
    with (
        patch("application_sdk.dev.embedded_dapr", _fake_dapr),
        patch("application_sdk.dev.embedded_runtime", _fake_runtime),
    ):
        yield captured_runtime_kwargs


class TestRunDevCombined:
    """Behavioral tests for run_dev_combined()."""

    @pytest.fixture(autouse=True)
    def _stub_daemons(self, _stub_embedded_daemons):
        """Stub the embedded daprd + Temporal context managers for every test in this class."""

    async def test_credentials_path_schedules_provision_task(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When credentials are provided, a background provisioning task is scheduled."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        captured: list[object] = []

        def _capture_task(coro):
            captured.append(coro)
            # Close it so we don't get a "never awaited" warning
            coro.close()
            return MagicMock()

        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            private_stdlib("application_sdk.dev._runner", "asyncio") as local_asyncio,
            patch.object(local_asyncio, "create_task", side_effect=_capture_task),
        ):
            await run_dev_combined(
                _fake_app_class(),
                credentials={"host": "h", "port": "5432"},
                example_input={"connection": {"connection_name": "c"}},
            )

        assert len(captured) == 1, "credentials path must schedule provisioning task"

    async def test_no_credentials_prints_dev_banner(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When no credentials, a banner with usage hints is printed to stdout."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
        ):
            await run_dev_combined(_fake_app_class(), host="127.0.0.1", port=8000)
        out = capsys.readouterr().out
        assert "Dev server running" in out
        assert "/workflows/v1/start" in out

    @pytest.mark.parametrize(
        ("kwargs", "warns"),
        [
            ({"credential_stores": {"default": MagicMock()}}, True),
            ({"credential_stores": None}, True),
            ({}, False),
        ],
        ids=["passed", "passed-none", "omitted"],
    )
    async def test_credential_stores_warns_and_is_not_used(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kwargs: dict[str, object],
        warns: bool,
    ) -> None:
        """``credential_stores`` is ignored: passing it warns, and infra never sees it."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as create_infra,
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            await run_dev_combined(_fake_app_class(), **kwargs)  # type: ignore[arg-type]

        deprecations = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "credential_stores" in str(w.message)
        ]
        assert len(deprecations) == (1 if warns else 0)
        create_infra.assert_awaited_once_with()

    async def test_example_input_is_rendered_in_banner(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """example_input shows up as JSON in the banner curl example."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
        ):
            await run_dev_combined(
                _fake_app_class(),
                example_input={"connection": {"connection_name": "abc"}},
            )
        out = capsys.readouterr().out
        assert "abc" in out

    async def test_temporal_ui_options_are_passed_to_embedded_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _stub_embedded_daemons: list[dict[str, object]],
    ) -> None:
        """Temporal UI options are forwarded and the UI URL is printed."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
        ):
            await run_dev_combined(
                _fake_app_class(),
                temporal_ui=True,
                temporal_ui_port=8233,
            )

        assert _stub_embedded_daemons[-1]["temporal_ui"] is True
        assert _stub_embedded_daemons[-1]["temporal_ui_port"] == 8233
        assert "Temporal UI running at http://127.0.0.1:8233" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# run_dev_combined — _provision_and_start inner-function                      #
# --------------------------------------------------------------------------- #


class TestRunDevCombinedProvisioning:
    """Exercise the inner _provision_and_start coroutine in the credentials path."""

    @pytest.fixture(autouse=True)
    def _stub_daemons(self, _stub_embedded_daemons):
        """Stub the embedded daprd + Temporal context managers for every test in this class."""

    async def test_provision_and_start_posts_creds_and_workflow(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The captured provisioning coroutine POSTs to local-vault then /start."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")

        captured: list[object] = []

        def _capture_task(coro):
            captured.append(coro)
            return MagicMock()

        # Build a fake httpx.AsyncClient that records calls
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None

        # Health check returns 200 immediately
        health_resp = MagicMock(status_code=200)
        fake_client.get = AsyncMock(return_value=health_resp)

        # local-vault POST
        cred_resp = MagicMock()
        cred_resp.json = MagicMock(
            return_value={"data": {"credential_guid": "cred-123"}}
        )
        # workflow start POST
        wf_resp = MagicMock()
        wf_resp.json = MagicMock(
            return_value={"data": {"workflow_id": "wf-1", "run_id": "run-1"}}
        )
        fake_client.post = AsyncMock(side_effect=[cred_resp, wf_resp])

        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            private_stdlib("application_sdk.dev._runner", "asyncio") as local_asyncio,
            patch.object(local_asyncio, "create_task", side_effect=_capture_task),
            patch("httpx.AsyncClient", return_value=fake_client),
            patch("application_sdk.dev._runner.logger"),
        ):
            await run_dev_combined(
                _fake_app_class(),
                credentials={"host": "h"},
                example_input={"connection": {"connection_name": "c"}},
                host="127.0.0.1",
                port=8765,
            )

            # The captured coroutine is _provision_and_start — drive it inside the patches
            assert len(captured) == 1
            await captured[0]

        # Cred POST and workflow POST must both have happened
        post_urls = [c.args[0] for c in fake_client.post.call_args_list if c.args]
        assert any("local-vault" in u for u in post_urls)
        assert any("/workflows/v1/start" in u for u in post_urls)

        # The credential_guid from /local-vault must be injected into the workflow body
        wf_body = fake_client.post.call_args_list[1].kwargs["json"]
        assert wf_body["credential_guid"] == "cred-123"
        assert wf_body["connection"]["connection_name"] == "c"

    async def test_provision_health_poll_swallowed_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Health-check failures are swallowed so provisioning still proceeds."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")

        captured: list[object] = []

        def _capture_task(coro):
            captured.append(coro)
            return MagicMock()

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        # Health check raises every time — provisioning must still proceed
        # (after the loop exits) and POST creds + start workflow.
        fake_client.get = AsyncMock(side_effect=Exception("connection refused"))
        cred_resp = MagicMock()
        cred_resp.json = MagicMock(return_value={"credential_guid": "g1"})
        wf_resp = MagicMock()
        wf_resp.json = MagicMock(
            return_value={"data": {"workflow_id": "w", "run_id": "r"}}
        )
        fake_client.post = AsyncMock(side_effect=[cred_resp, wf_resp])

        with (
            patch(
                "application_sdk.dev._runner._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            private_stdlib("application_sdk.dev._runner", "asyncio") as local_asyncio,
            patch.object(local_asyncio, "create_task", side_effect=_capture_task),
            patch("httpx.AsyncClient", return_value=fake_client),
            # Make asyncio.sleep instant
            patch.object(local_asyncio, "sleep", new_callable=AsyncMock),
            patch("application_sdk.dev._runner.logger"),
        ):
            await run_dev_combined(
                _fake_app_class(),
                credentials={"host": "h"},
            )
            assert len(captured) == 1
            await captured[0]

        assert fake_client.post.await_count == 2
