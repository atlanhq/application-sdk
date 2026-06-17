"""Hermetic integration test: ``create_store_from_binding`` → GCS via storage-testbench.

GCS is the one cloud with no faithful, credential-free emulator obstore can use
out of the box:
  * fake-gcs-server rejects obstore's XML-API uploads ("invalid uploadType");
  * Google's storage-testbench stores them but omits the ``ETag`` response
    header obstore requires — so a tiny nginx proxy re-injects ``ETag``;
  * obstore's GCS client still demands a credential token, which the emulator
    has no real OAuth/metadata endpoint for.

Per review feedback, **none of this leaks into production** ``binding.py``: the
emulator base_url + an anonymous credential are injected purely at the **test
layer** by monkeypatching ``obstore.store.GCSStore`` (the same shape the OpenAPI
app uses to inject ``allow_http``). The production binding is exercised
unchanged via ``create_store_from_binding``; only the final obstore store
construction is redirected at the emulator.

Scope caveat: because the emulator base_url + credential are injected at the
test layer, the GCS credential/endpoint *construction* path in ``binding.py`` is
NOT the thing under test here — this proves obstore can round-trip against a GCS
backend, not GCS-binding parity with the S3/Azure tests.

Marked ``storage_emulator``. Local (storage-testbench + nginx ETag proxy):

    docker network create gcs-net
    docker run -d --name gcs-testbench --network gcs-net -e PORT=9000 \\
        gcr.io/cloud-devrel-public-resources/storage-testbench:latest
    docker run -d --name gcs-proxy --network gcs-net -p 9095:9000 nginx:1.27-alpine \\
        sh -c 'printf "server{listen 9000;location/{proxy_pass http://gcs-testbench:9000;proxy_hide_header ETag;add_header ETag \\"x\\" always;}}" > /etc/nginx/conf.d/default.conf; exec nginx -g "daemon off;"'
    curl -X POST "http://localhost:9095/storage/v1/b?project=test" -d '{"name":"sdk-emulator-test"}'
    GCS_EMULATOR_ENDPOINT=http://localhost:9095 \\
        uv run pytest tests/integration/storage/test_emulator_gcs.py -m storage_emulator -v
"""

from __future__ import annotations

import os

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import write_dapr_component

pytestmark = pytest.mark.storage_emulator

_ENDPOINT = os.environ.get("GCS_EMULATOR_ENDPOINT", "http://localhost:9095").rstrip("/")
_BUCKET = os.environ.get("GCS_EMULATOR_BUCKET", "sdk-emulator-test")


def _anonymous_credential():
    """A static no-op GCS credential — test-only, for the unauthenticated emulator."""
    from datetime import datetime, timezone

    def _provider():
        return {
            "token": "emulator-anonymous",
            "expires_at": datetime(2999, 1, 1, tzinfo=timezone.utc),
        }

    return _provider


@pytest.fixture(scope="module", autouse=True)
def require_testbench() -> None:
    """Skip the module when the GCS emulator (proxy) isn't reachable or unhealthy."""
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{_ENDPOINT}/storage/v1/b?project=test")
    except Exception as exc:  # pragma: no cover — env guard
        pytest.skip(f"GCS emulator not reachable at {_ENDPOINT}: {exc}")
    # Reachable but 5xx means the emulator/proxy is up yet unhealthy — skip with a
    # clear signal rather than letting the round-trip fail with an opaque error.
    if resp.status_code >= 500:  # pragma: no cover — env guard
        pytest.skip(f"GCS emulator unhealthy at {_ENDPOINT}: HTTP {resp.status_code}")


@pytest.fixture
def _redirect_gcs_to_emulator(monkeypatch):
    """Redirect obstore's GCSStore at the emulator — test layer only, no prod change.

    ``make_gcs_store`` imports ``GCSStore`` lazily from ``obstore.store`` on each
    call, so patching the attribute there is picked up by the real binding path.
    """
    import obstore.store as obstore_store

    real_gcs_store = obstore_store.GCSStore
    provider = _anonymous_credential()

    def _patched(**kwargs):
        config = dict(kwargs.pop("config", None) or {})
        config["base_url"] = _ENDPOINT
        client_options = dict(kwargs.pop("client_options", None) or {})
        client_options["allow_http"] = True
        return real_gcs_store(
            config=config,
            client_options=client_options,
            credential_provider=provider,
            **kwargs,
        )

    monkeypatch.setattr(obstore_store, "GCSStore", _patched)


async def test_gcs_binding_write_read(tmp_path, _redirect_gcs_to_emulator):
    """A gcp.bucket component writes + reads back through the SDK binding.

    Scope is put + get: storage-testbench serves obstore's XML-API PUT/GET but
    not its S3-style ``list-type=2`` LIST or object DELETE, so those ops aren't
    exercised here (an emulator gap, not an SDK one). Write+read is what proves
    the GCS binding builds a working store + does real I/O.
    """
    write_dapr_component(
        tmp_path / "components",
        name="gcs-emulator",
        binding_type="bindings.gcp.bucket",
        metadata={"bucket": _BUCKET, "project_id": "test"},
    )
    store = create_store_from_binding(
        "gcs-emulator", components_dir=tmp_path / "components"
    )

    key = "sdk-emulator/roundtrip.txt"
    payload = b"hello-from-sdk-gcs-emulator-test"
    await obstore.put_async(store, key, payload)

    try:
        result = await obstore.get_async(store, key)
        assert bytes(await result.bytes_async()) == payload
    finally:
        # obstore's S3-style DELETE isn't served by testbench, but its JSON API is.
        # Keep a long-lived local testbench clean across reruns (CI is fresh).
        import httpx

        with httpx.Client(timeout=3.0) as client:
            client.delete(
                f"{_ENDPOINT}/storage/v1/b/{_BUCKET}/o/{key.replace('/', '%2F')}"
            )
