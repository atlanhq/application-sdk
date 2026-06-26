"""Contract: the SDR transformed-data EGRESS path must equal the path PUBLISH reads.

Recurring SDR incident (teradata/Highmark, redshift, mssql/Kaizen): a connector's
extract+transform succeed and the transformed parquet IS uploaded to the Atlan
bucket, but at a run prefix that DROPS the ``{workflow_id}`` segment — while publish
reads ``transformed_data_prefix = <output_path>/transformed`` derived from
``WORKFLOW_OUTPUT_PATH_TEMPLATE`` (which INCLUDES ``{workflow_id}``). The globs miss
→ "No entities in the transformed data" → 0 assets published, on an otherwise
"successful" run. SDR-only: non-SDR writes land in one shared store at the same key,
so it is invisible there. Fixed in #2364 (App.upload run prefix now includes
``{workflow_id}``).

These tests pin the two INDEPENDENT derivations together so they cannot drift apart
again — both must anchor to ``WORKFLOW_OUTPUT_PATH_TEMPLATE``:
  * EGRESS — ``App.upload()`` (RETAINED) lands under
             ``artifacts/apps/{app}/workflows/{workflow_id}/{run_id}/…``
  * READ   — ``PublishInputMixin.transformed_data_prefix`` resolves to
             ``artifacts/apps/{app}/workflows/{workflow_id}/{run_id}/transformed``
"""

from __future__ import annotations

import posixpath
from typing import ClassVar

from application_sdk.app import App
from application_sdk.app.context import AppContext
from application_sdk.constants import WORKFLOW_OUTPUT_PATH_TEMPLATE
from application_sdk.contracts.base import PublishInputMixin
from application_sdk.contracts.storage import UploadInput
from application_sdk.contracts.types import StorageTier
from application_sdk.execution import get_object_store_prefix
from application_sdk.storage.factory import create_local_store

_APP = "egress-contract-test"
_WID = "wf-egress-123"
_RID = "run-egress-456"


def _run_prefix() -> str:
    """The canonical run-scoped prefix both sides must agree on."""
    return f"artifacts/apps/{_APP}/workflows/{_WID}/{_RID}"


def _output_path() -> str:
    return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name=_APP, workflow_id=_WID, run_id=_RID
    )


class _EgressApp(App):
    """Minimal registered App for exercising the real ``App.upload`` egress path."""

    _app_registered: ClassVar[bool] = True
    _app_name: ClassVar[str] = _APP

    async def run(self, input):  # type: ignore[override]
        pass  # not exercised


def test_publish_read_prefix_anchors_to_workflow_output_template() -> None:
    """READ side: ``transformed_data_prefix`` == ``<run_prefix>/transformed``.

    Also pins the egress *base* derivation (``get_object_store_prefix``) to the
    same prefix, so the two helpers can't diverge.
    """
    expected = f"{_run_prefix()}/transformed"

    pub = PublishInputMixin(output_path=_output_path())
    assert pub.transformed_data_prefix == expected

    egress_transformed = posixpath.normpath(
        f"{get_object_store_prefix(_output_path())}/transformed"
    )
    assert egress_transformed == expected
    assert pub.transformed_data_prefix == egress_transformed


async def test_app_upload_egress_lands_under_publish_run_prefix(tmp_path) -> None:
    """EGRESS side: ``App.upload()`` (RETAINED) lands under the SAME run prefix
    publish reads from — i.e. it includes ``{workflow_id}`` (the #2364 fix).

    A regression that drops ``{workflow_id}`` again (the original incident) makes
    this assertion fail instead of silently shipping a 0-asset publish.
    """
    store = create_local_store(tmp_path / "objectstore")
    app = _EgressApp()
    app._context = AppContext(
        app_name=_APP,
        app_version="1",
        run_id=_RID,
        workflow_id=_WID,
        _storage=store,
    )

    src = tmp_path / "transformed.json"
    src.write_text('{"typeName": "Table", "attributes": {"name": "t"}}')

    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    landed = result.ref.storage_path
    assert landed is not None
    # Anchored to the exact prefix publish derives from WORKFLOW_OUTPUT_PATH_TEMPLATE.
    template_prefix = get_object_store_prefix(_output_path())
    assert template_prefix == _run_prefix()
    assert landed.startswith(f"{_run_prefix()}/"), (
        f"RETAINED egress {landed!r} is not under the publish run prefix "
        f"{_run_prefix()!r} — the SDR upload↔publish path contract is broken "
        f"(workflow_id likely dropped; see #2364)."
    )
