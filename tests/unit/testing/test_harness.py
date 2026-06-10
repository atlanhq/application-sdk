"""Tests for AppTestHarness — the in-process app scenario harness.

Demonstrates the intended app-developer workflow: define an App with
``@task`` methods that use state, secrets, credentials, storage, and
heartbeats through ``self.context`` / the public storage helpers, then
execute it end-to-end with one harness and zero infrastructure.
"""

from __future__ import annotations

import pytest

from application_sdk.app.base import App, AppContextError
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.credentials.ref import CredentialRef
from application_sdk.credentials.types import ApiKeyCredential
from application_sdk.infrastructure.context import get_infrastructure
from application_sdk.storage import download_file, list_keys, upload_file_from_bytes
from application_sdk.testing import AppTestHarness

pytestmark = pytest.mark.usefixtures("clean_app_registry", "clean_task_registry")


# =============================================================================
# Sample App under test — uses every harness-provided capability
# =============================================================================


class EtlInput(Input):
    name: str = ""
    payload: str = ""


class EtlOutput(Output):
    message: str = ""
    stored_key: str = ""


class SampleEtlApp(App):
    """Toy ETL app exercising state, secrets, storage, and heartbeats."""

    @task
    async def extract(self, input: EtlInput) -> EtlOutput:
        # State store via context.
        await self.context.save_state("seen", {"name": input.name})
        # Heartbeats via the task context.
        self.task_context.heartbeat("extract", input.name)
        # Storage via the public helpers + context store.
        key = f"raw/{input.name}.txt"
        await upload_file_from_bytes(key, input.payload.encode(), self.context.storage)
        return EtlOutput(message="extracted", stored_key=key)

    @task
    async def publish(self, input: EtlInput) -> EtlOutput:
        token = await self.context.get_secret("api-token")
        # Storage helpers also resolve the harness store implicitly
        # (module-level infrastructure context).
        key = f"published/{input.name}.txt"
        await upload_file_from_bytes(key, f"by:{token}".encode())
        return EtlOutput(message="published", stored_key=key)

    async def run(self, input: EtlInput) -> EtlOutput:
        extracted = await self.extract(input)
        published = await self.publish(input)
        return EtlOutput(
            message=f"{extracted.message}+{published.message}",
            stored_key=published.stored_key,
        )


# =============================================================================
# End-to-end execution
# =============================================================================


class TestEndToEndExecution:
    async def test_run_executes_tasks_inline_and_returns_output(self) -> None:
        with AppTestHarness(SampleEtlApp, secrets={"api-token": "t0k3n"}) as harness:
            out = await harness.execute("run", EtlInput(name="ada", payload="hi"))

        assert isinstance(out, EtlOutput)
        assert out.message == "extracted+published"
        assert out.stored_key == "published/ada.txt"

    async def test_task_outputs_are_captured_in_order(self) -> None:
        with AppTestHarness(SampleEtlApp, secrets={"api-token": "t"}) as harness:
            await harness.execute("run", EtlInput(name="ada", payload="hi"))

            assert [o.message for o in harness.outputs] == ["extracted", "published"]

    async def test_state_secrets_storage_and_heartbeats_are_observable(self) -> None:
        with AppTestHarness(SampleEtlApp, secrets={"api-token": "t0k3n"}) as harness:
            await harness.execute("run", EtlInput(name="ada", payload="hello"))

            # State writes are namespaced app:run:key and capture the value.
            (saved_key, saved_value) = harness.state_store.get_save_calls()[0]
            assert saved_key.startswith("sample-etl-app:")
            assert saved_key.endswith(":seen")
            assert saved_value == {"name": "ada"}

            # Secret reads went through the mock secret store.
            assert harness.secret_store.get_get_calls() == ["api-token"]

            # Both uploads landed in the harness objectstore.
            assert await list_keys("", harness.store) == [
                "published/ada.txt",
                "raw/ada.txt",
            ]

            # Heartbeats were recorded with their details.
            assert harness.heartbeats.get_heartbeat_calls() == [("extract", "ada")]

    async def test_single_task_can_be_executed_directly(self, tmp_path) -> None:
        with AppTestHarness(SampleEtlApp) as harness:
            out = await harness.execute(
                SampleEtlApp.extract, EtlInput(name="solo", payload="data")
            )

            assert out.message == "extracted"
            dest = tmp_path / "roundtrip.txt"
            await download_file("raw/solo.txt", dest, harness.store)
            assert dest.read_text() == "data"

    async def test_local_store_root_persists_objects_on_disk(self, tmp_path) -> None:
        store_root = tmp_path / "objectstore"
        with AppTestHarness(SampleEtlApp, store_root=store_root) as harness:
            await harness.execute("extract", EtlInput(name="disk", payload="bytes"))

        assert (store_root / "raw" / "disk.txt").read_text() == "bytes"


# =============================================================================
# Credentials
# =============================================================================


class CredInput(Input):
    cred_ref: CredentialRef | None = None


class CredApp(App):
    @task
    async def whoami(self, input: CredInput) -> EtlOutput:
        assert input.cred_ref is not None
        cred = await self.context.resolve_credential(input.cred_ref)
        assert isinstance(cred, ApiKeyCredential)
        return EtlOutput(message=cred.api_key)

    async def run(self, input: CredInput) -> EtlOutput:
        return await self.whoami(input)


class TestCredentialResolution:
    async def test_credentials_added_to_harness_resolve_in_tasks(self) -> None:
        with AppTestHarness(CredApp) as harness:
            # Production pattern: mint a ref, pass it through the Input
            # contract, resolve it inside the task via self.context.
            ref = harness.credentials.add_api_key("svc", api_key="k-123")

            out = await harness.execute("run", CredInput(cred_ref=ref))
            assert out.message == "k-123"


# =============================================================================
# Lifecycle and isolation
# =============================================================================


class TestLifecycle:
    async def test_app_property_requires_entered_harness(self) -> None:
        harness = AppTestHarness(SampleEtlApp)
        with pytest.raises(RuntimeError, match="must be entered"):
            _ = harness.app

    async def test_infrastructure_context_is_scoped_and_restored(self) -> None:
        before = get_infrastructure()
        with AppTestHarness(SampleEtlApp) as harness:
            infra = get_infrastructure()
            assert infra is not None
            assert infra.storage is harness.store
            assert infra.state_store is harness.state_store
        after = get_infrastructure()
        assert after is None or after.storage is not before

    async def test_context_is_detached_after_exit(self) -> None:
        with AppTestHarness(SampleEtlApp) as harness:
            app = harness.app
        with pytest.raises(AppContextError):
            _ = app.context

    async def test_two_harnesses_are_fully_isolated(self) -> None:
        with AppTestHarness(SampleEtlApp, secrets={"api-token": "a"}) as h1:
            await h1.execute("run", EtlInput(name="one", payload="1"))
        with AppTestHarness(SampleEtlApp, secrets={"api-token": "b"}) as h2:
            assert h2.outputs == []
            assert h2.state_store.get_save_calls() == []
            assert await list_keys("", h2.store) == []

    async def test_task_context_parity_outside_tasks(self) -> None:
        """run() must NOT see a task context — same contract as production."""

        class NoTaskContextApp(App):
            async def run(self, input: EtlInput) -> EtlOutput:
                _ = self.task_context  # must raise, as it would under Temporal
                return EtlOutput()

        with AppTestHarness(NoTaskContextApp) as harness:
            with pytest.raises(AppContextError):
                await harness.execute("run", EtlInput())
