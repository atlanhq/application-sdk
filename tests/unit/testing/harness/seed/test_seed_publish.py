"""Seeding *through* publish: the node that runs, and the order of the steps.

Two halves, both tenant-free. The first pins the ``PublishWorkflow`` node — the
prefixes it reads, the two flags that make publish own both the entities and the
connection cache, and the fact that every argument is a literal rather than a
``$.``-reference to a node that does not exist here. The second pins the
*sequence*: validate before uploading, upload before submitting, and refuse a
publish run that did not succeed on every node.

The sequence is the part worth a test. A seed that uploads a batch it has not
checked publishes partially, and a seed that reports success on a failed publish
hands the connector under test a connection with no cache — the exact shape that
greens a leg while dropping lineage.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from application_sdk.testing.harness.outcome import Indeterminate, Outcome, Settled
from application_sdk.testing.harness.seed import (
    SEED_PUBLISH_NODE_ID,
    DatabaseSpec,
    SchemaSpec,
    SeedPrefixes,
    SeedPublishEmptyError,
    SeedPublishFailedError,
    SeedPublishPlan,
    SeedSpec,
    SeedTreeInvalidError,
    TableSpec,
    build_seed_publish_dag,
    build_seed_submit_payload,
    seed_assets,
    seed_prefix_root,
)
from application_sdk.validation.assets import AssetValidationReport, ReferentialFailure

_CONNECTION_QN = "default/snowflake/1787587123106596"
_PREFIX_ROOT = "artifacts/apps/coalesce/e2e-seed/1787587123106596"


def _resolved():
    return SeedSpec(
        connector_type="snowflake",
        qualified_name=_CONNECTION_QN,
        display_name="snowflake-seed",
        admin_roles=("role-guid",),
        databases=(
            DatabaseSpec(
                name="ANALYTICS",
                schemas=(
                    SchemaSpec(
                        name="PUBLIC",
                        tables=(TableSpec(name="ORDERS", columns=("ID",)),),
                    ),
                ),
            ),
        ),
    ).resolve(qualified_name=_CONNECTION_QN, display_name="snowflake-seed")


def _plan() -> SeedPublishPlan:
    return SeedPublishPlan(
        app_name="coalesce",
        publish_task_queue="atlan-publish-production",
        ae_workflow_name="coalesce-e2e-42-seed-snowflake",
        app_service_url="http://coalesce.svc",
        run_id=42,
        poll_interval_seconds=1,
        poll_timeout_seconds=5,
    )


class TestPrefixes:
    """One root, three siblings — never three aliases of one path."""

    def test_the_root_is_keyed_on_the_seeded_connections_suffix(self) -> None:
        assert (
            seed_prefix_root(app_name="coalesce", qualified_name=_CONNECTION_QN)
            == _PREFIX_ROOT
        )

    def test_current_state_never_equals_transformed(self) -> None:
        """``atlan-publish-app`` fails its own config validation when they are
        equal (DBBI-566), minutes into the run."""
        prefixes = SeedPrefixes(root=_PREFIX_ROOT)
        assert prefixes.current_state != prefixes.transformed
        assert prefixes.publish_state != prefixes.transformed
        assert all(
            p.startswith(f"{_PREFIX_ROOT}/")
            for p in (
                prefixes.transformed,
                prefixes.publish_state,
                prefixes.current_state,
            )
        )


class TestSeedPublishNode:
    """The one node a seed runs."""

    def test_the_node_addresses_the_tenants_publish_app(self) -> None:
        dag = build_seed_publish_dag(
            spec=_resolved(),
            prefixes=SeedPrefixes(root=_PREFIX_ROOT),
            publish_task_queue="atlan-publish-production",
        )
        node = dag[SEED_PUBLISH_NODE_ID]
        assert node["app_name"] == "publish"
        assert node["inputs"]["workflow_type"] == "PublishWorkflow"
        assert node["inputs"]["task_queue"] == "atlan-publish-production"

    def test_both_ownership_flags_are_set(self) -> None:
        """``connection_creation_enabled`` is what makes publish create the
        connection and wait out its own policy sync; ``connection_cache_via_app
        _enabled`` is the half a direct pyatlan write could never produce."""
        args = build_seed_publish_dag(
            spec=_resolved(),
            prefixes=SeedPrefixes(root=_PREFIX_ROOT),
            publish_task_queue="q",
        )[SEED_PUBLISH_NODE_ID]["inputs"]["args"]
        assert args["connection_creation_enabled"] is True
        assert args["connection_cache_via_app_enabled"] is True
        assert args["connection_entity"]["attributes"]["qualifiedName"] == (
            _CONNECTION_QN
        )

    def test_every_argument_is_a_literal(self) -> None:
        """A seed has no producing node, so a ``$.``-reference would be an input
        the harness cannot state."""
        args = build_seed_publish_dag(
            spec=_resolved(),
            prefixes=SeedPrefixes(root=_PREFIX_ROOT),
            publish_task_queue="q",
        )[SEED_PUBLISH_NODE_ID]["inputs"]["args"]
        assert args["transformed_data_prefix"] == f"{_PREFIX_ROOT}/transformed"
        assert not any(isinstance(v, str) and v.startswith("$.") for v in args.values())

    def test_the_node_id_is_not_the_connectors_own(self) -> None:
        """A seed run and the run under test must be distinguishable in an AE
        run list and in a required-node assertion."""
        assert SEED_PUBLISH_NODE_ID != "publish"


class TestSubmitPayload:
    """The submit body carries the slug and no token nothing will substitute."""

    def test_the_slug_is_carried_and_no_credential_is_created(self) -> None:
        payload = build_seed_submit_payload(
            spec=_resolved(),
            run_id=42,
            ae_workflow_slug="slug-1",
            app_service_url="http://x",
        )
        assert payload["metadata"]["ae_workflow_slug"] == "slug-1"
        assert "payload" not in payload

    def test_no_unsubstituted_credential_token_rides_the_submit(self) -> None:
        """With no ``payload[]`` there is nothing to substitute
        ``{{credentialGuid}}`` with, and an unsubstituted token is what
        ``submit_workflow`` warns about."""
        from application_sdk.testing.harness.automation_engine.retry import (
            unsubstituted_parameter_tokens,
        )

        payload = build_seed_submit_payload(
            spec=_resolved(),
            run_id=42,
            ae_workflow_slug="slug-1",
            app_service_url="http://x",
        )
        assert unsubstituted_parameter_tokens(payload) == {}


class _FakeAE:
    """Records the submit and answers one scripted ``native-status``."""

    def __init__(self, *, all_succeeded: bool = True) -> None:
        self.submitted: list[tuple[dict[str, Any], str]] = []
        self._all_succeeded = all_succeeded

    async def submit_workflow(
        self, payload: dict[str, Any], *, slug: str, **_: Any
    ) -> str:
        self.submitted.append((payload, slug))
        return "ae-run-1"

    async def poll_native_status(self, run_id: str, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=run_id,
            all_nodes_succeeded=self._all_succeeded,
            status=SimpleNamespace(
                value="Success" if self._all_succeeded else "Failed"
            ),
        )


def _verifier(outcome: Outcome[int]) -> Callable[[str], Awaitable[Outcome[int]]]:
    """A `SeedVerifier` that answers one scripted read-back."""

    async def _verify(_qualified_name: str) -> Outcome[int]:
        return outcome

    return _verify


def _landed(count: int) -> Outcome[int]:
    return Settled(
        label="seeded assets", attempts=1, elapsed=timedelta(seconds=1), value=count
    )


def _unreadable() -> Outcome[int]:
    return Indeterminate(
        label="seeded assets",
        attempts=3,
        elapsed=timedelta(seconds=3),
        cause=RuntimeError("Atlas search unavailable"),
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: AssetValidationReport | None = None,
    all_succeeded: bool = True,
) -> tuple[_FakeAE, list[tuple[str, str]]]:
    """Patch the three collaborators a seed reaches for, recording each."""
    uploaded: list[tuple[str, str]] = []

    async def _record_upload(key: str, local_path: str, _store: object) -> str:
        uploaded.append((key, Path(local_path).name))
        return "sha"

    async def _seeded(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(slug="slug-1", seed_version=1)

    monkeypatch.setattr(
        "application_sdk.testing.harness.seed.upload_file", _record_upload
    )
    monkeypatch.setattr(
        "application_sdk.testing.harness.seed.publish_seed_version", _seeded
    )
    if report is not None:
        monkeypatch.setattr(
            "application_sdk.testing.harness.seed.validate_transformed_dir",
            lambda *_a, **_k: report,
        )
    return _FakeAE(all_succeeded=all_succeeded), uploaded


class TestSeedAssetsSequence:
    """Validate, upload, publish, verify — in that order, or not at all."""

    @pytest.mark.asyncio
    async def test_a_successful_seed_reports_what_it_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ae, uploaded = _wire(monkeypatch)
        seeded = await seed_assets(
            _resolved(),
            store=object(),
            ae=ae,
            plan=_plan(),
            verify=_verifier(_landed(4)),
        )
        assert seeded.qualified_name == _CONNECTION_QN
        assert seeded.created == {"Database": 1, "Schema": 1, "Table": 1, "Column": 1}
        assert seeded.prefix == _PREFIX_ROOT
        assert seeded.transformed_data_prefix == f"{_PREFIX_ROOT}/transformed"
        assert seeded.ae_workflow_slug == "slug-1"
        assert seeded.ae_run_id == "ae-run-1"

    @pytest.mark.asyncio
    async def test_the_ndjson_lands_under_the_transformed_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ae, uploaded = _wire(monkeypatch)
        await seed_assets(
            _resolved(),
            store=object(),
            ae=ae,
            plan=_plan(),
            verify=_verifier(_landed(4)),
        )
        assert uploaded == [(f"{_PREFIX_ROOT}/transformed/assets.json", "assets.json")]

    @pytest.mark.asyncio
    async def test_an_invalid_batch_is_neither_uploaded_nor_submitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check is offline and cheap; running it before the upload is what
        keeps a batch that would publish partially off the tenant entirely."""
        failing = AssetValidationReport(total=4, passed=3)
        failing.orphans.append(
            ReferentialFailure(
                missing_type_name="Schema",
                missing_qualified_name=f"{_CONNECTION_QN}/ANALYTICS/PUBLIC",
                reference_count=1,
                file="assets.json",
                line=3,
                type_name="Table",
                qualified_name=f"{_CONNECTION_QN}/ANALYTICS/PUBLIC/ORDERS",
                relationship="atlanSchema",
            )
        )
        ae, uploaded = _wire(monkeypatch, report=failing)
        with pytest.raises(SeedTreeInvalidError) as caught:
            await seed_assets(
                _resolved(),
                store=object(),
                ae=ae,
                plan=_plan(),
                verify=_verifier(_landed(4)),
            )
        assert uploaded == []
        assert ae.submitted == []
        assert "PUBLIC" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_publish_that_did_not_succeed_is_a_failure_not_a_seed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting success here would hand the connector a connection with no
        cache — a green leg with silently dropped lineage."""
        ae, _uploaded = _wire(monkeypatch, all_succeeded=False)
        with pytest.raises(SeedPublishFailedError) as caught:
            await seed_assets(
                _resolved(),
                store=object(),
                ae=ae,
                plan=_plan(),
                verify=_verifier(_landed(4)),
            )
        assert "slug-1" in str(caught.value)
        assert "ae-run-1" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_succeeded_publish_that_landed_nothing_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure a node-status check cannot see. Publish is handed a
        *prefix*; one it cannot read is an empty batch rather than an error, so
        the node reports success having published nothing — and that resurfaces
        minutes later as the connector's own ATLAS-404 cascade, in another repo."""
        ae, _uploaded = _wire(monkeypatch)
        with pytest.raises(SeedPublishEmptyError) as caught:
            await seed_assets(
                _resolved(),
                store=object(),
                ae=ae,
                plan=_plan(),
                verify=_verifier(_landed(0)),
            )
        assert f"{_PREFIX_ROOT}/transformed" in str(caught.value)
        assert "ae-run-1" in str(caught.value)

    @pytest.mark.asyncio
    async def test_an_unreadable_count_is_unverified_not_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ungraded is not unmet: a search that could not be read is a harness
        read failure, and failing the seed on one would report an Atlas outage as
        a seed defect."""
        ae, _uploaded = _wire(monkeypatch)
        seeded = await seed_assets(
            _resolved(),
            store=object(),
            ae=ae,
            plan=_plan(),
            verify=_verifier(_unreadable()),
        )
        assert seeded.qualified_name == _CONNECTION_QN

    @pytest.mark.asyncio
    async def test_the_read_back_runs_only_after_the_node_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed publish must surface as itself, not as an empty seed — the
        two have different remediations and only one of them is about storage."""
        ae, _uploaded = _wire(monkeypatch, all_succeeded=False)
        calls: list[str] = []

        async def _record(qualified_name: str) -> Outcome[int]:
            calls.append(qualified_name)
            return _landed(0)

        with pytest.raises(SeedPublishFailedError):
            await seed_assets(
                _resolved(), store=object(), ae=ae, plan=_plan(), verify=_record
            )
        assert calls == []
