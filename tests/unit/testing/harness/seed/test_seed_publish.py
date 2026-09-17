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

The third thing pinned here is *which graph ran*. The seed submits straight to
AE precisely so no app's manifest can be published over its node (FND-1766), and
both readings that could catch a substitution — the version AE serves after the
submit, and the node names on the run itself — must refuse rather than proceed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from application_sdk.testing.harness.seed import (
    SEED_PUBLISH_NODE_ID,
    TRANSFORMED_FILE_NAME,
    DatabaseSpec,
    SchemaSpec,
    SeedDagSupersededError,
    SeedPrefixes,
    SeedPublishEmptyError,
    SeedPublishFailedError,
    SeedPublishPlan,
    SeedSpec,
    SeedTreeInvalidError,
    TableSpec,
    build_seed_publish_dag,
    seed_assets,
    seed_object_keys,
    seed_prefix_root,
)
from application_sdk.validation.assets import AssetValidationReport, ReferentialFailure

_CONNECTION_QN = "default/snowflake/1787587123106596"
_PREFIX_ROOT = "artifacts/apps/coalesce/e2e-seed/default%2Fsnowflake%2F1787587123106596"


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
        run_id=42,
        poll_interval_seconds=1,
        poll_timeout_seconds=5,
    )


class TestPrefixes:
    """One root, three siblings — never three aliases of one path."""

    def test_the_root_is_keyed_on_the_whole_qualified_name(self) -> None:
        assert (
            seed_prefix_root(app_name="coalesce", qualified_name=_CONNECTION_QN)
            == _PREFIX_ROOT
        )

    def test_two_connections_sharing_a_suffix_do_not_share_a_prefix(self) -> None:
        """``SeedSpec.qualified_name`` is caller-supplied, so the per-instance
        uniqueness the minter guarantees is a property of the default, not of
        the input. Keyed on the last segment, these two would write over each
        other's NDJSON and either one's teardown would delete both."""
        snowflake = seed_prefix_root(
            app_name="coalesce", qualified_name="default/snowflake/123"
        )
        postgres = seed_prefix_root(
            app_name="coalesce", qualified_name="default/postgres/123"
        )
        assert snowflake != postgres

    def test_no_root_can_be_a_path_prefix_of_another(self) -> None:
        """Encoded into one segment rather than nested, so a longer QN's root
        never sits *inside* a shorter one's — which is what would put one seed's
        keys in another seed's tree, and one seed's teardown on a sibling's
        bytes."""
        short = seed_prefix_root(
            app_name="coalesce", qualified_name="default/snowflake/123"
        )
        longer = seed_prefix_root(
            app_name="coalesce", qualified_name="default/snowflake/123/extra"
        )
        assert not longer.startswith(f"{short}/")
        assert "/" not in longer.rsplit("/", 1)[-1]

    def test_the_encoding_is_injective(self) -> None:
        """``quote(safe="")`` encodes ``%`` too, so the mapping holds for any
        input rather than only for the QN shapes we expect."""
        literal = seed_prefix_root(app_name="c", qualified_name="a%2Fb")
        slashed = seed_prefix_root(app_name="c", qualified_name="a/b")
        assert literal != slashed

    def test_the_root_still_reads_as_the_qualified_name(self) -> None:
        """A stray prefix has to stay attributable in a bucket listing."""
        root = seed_prefix_root(app_name="coalesce", qualified_name=_CONNECTION_QN)
        assert root.endswith("default%2Fsnowflake%2F1787587123106596")
        assert "/e2e-seed/" in root
        assert "/coalesce/" in root

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


class TestSeedObjectKeys:
    """What teardown deletes by key, because it cannot delete by prefix.

    ``delete_prefix`` is a LIST plus a bulk ``POST ?delete`` — both bucket-level
    URLs, which the tenant's s3proxy answers ``403 code 1009`` however
    allowlisted the prefix is. Deleting by key works only while the set is
    knowable without a listing, and this is the one place that states it.
    """

    def test_the_key_is_the_one_the_seed_uploads(self) -> None:
        """``seed_assets`` unpacks this same tuple for its upload, so a drift
        between what is written and what is deleted cannot happen silently."""
        assert seed_object_keys(root=_PREFIX_ROOT) == (
            f"{SeedPrefixes(root=_PREFIX_ROOT).transformed}/{TRANSFORMED_FILE_NAME}",
        )

    def test_every_key_sits_under_the_seeds_own_root(self) -> None:
        """The keys are handed to a single-object DELETE with no listing behind
        it, so nothing downstream would notice one pointing outside the seed."""
        assert all(
            key.startswith(f"{_PREFIX_ROOT}/")
            for key in seed_object_keys(root=_PREFIX_ROOT)
        )

    def test_publishs_own_state_is_not_claimed(self) -> None:
        """The harness did not write ``publish-state/`` or ``current-state/``
        and cannot enumerate them from a runner. Listing them here would be a
        claim to delete keys whose names nobody knows."""
        keys = seed_object_keys(root=_PREFIX_ROOT)
        prefixes = SeedPrefixes(root=_PREFIX_ROOT)
        assert not any(
            key.startswith((prefixes.publish_state, prefixes.current_state))
            for key in keys
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
        connection and wait out its own policy sync; the three cache flags are
        the half a direct pyatlan write could never produce.

        All four default to **false** in atlan-publish-app, and the cache needs
        all three of its own: ``connection_cache_enabled`` gates construction
        outright, ``executor_enabled`` (with the via-app flag) decides
        ``connection_cache_dry_run``, and ``connection_cache_via_app_enabled``
        makes publish the owner. Set only the last and the seed publishes
        entities and no usable cache — which the Atlas read-back cannot see,
        because entities are exactly what it checks.
        """
        args = build_seed_publish_dag(
            spec=_resolved(),
            prefixes=SeedPrefixes(root=_PREFIX_ROOT),
            publish_task_queue="q",
        )[SEED_PUBLISH_NODE_ID]["inputs"]["args"]
        assert args["connection_creation_enabled"] is True
        assert args["connection_cache_enabled"] is True
        assert args["connection_cache_via_app_enabled"] is True
        assert args["executor_enabled"] is True
        assert args["connection_entity"]["attributes"]["qualifiedName"] == (
            _CONNECTION_QN
        )

    def test_no_cache_flag_is_left_to_the_tenants_default(self) -> None:
        """Each is named explicitly rather than inherited: a seed whose
        behaviour depends on a deployment default means something different on
        each tenant, and the difference is invisible until a connector finds no
        cache."""
        args = build_seed_publish_dag(
            spec=_resolved(),
            prefixes=SeedPrefixes(root=_PREFIX_ROOT),
            publish_task_queue="q",
        )[SEED_PUBLISH_NODE_ID]["inputs"]["args"]
        for flag in (
            "connection_cache_enabled",
            "connection_cache_via_app_enabled",
            "executor_enabled",
        ):
            assert flag in args, f"{flag} would fall back to the tenant's default"

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


class _FakeAE:
    """Records the submit and answers one scripted ``native-status``.

    ``submit_published_version`` rather than ``submit_workflow``: the seed's
    submit takes no payload at all, because there is no envelope to name an app
    with (FND-1766). A fake that still accepted one would let a regression back
    onto the Heracles path unnoticed.
    """

    def __init__(
        self,
        *,
        all_succeeded: bool = True,
        ran_nodes: tuple[str, ...] = (SEED_PUBLISH_NODE_ID,),
        foreign: str = "",
    ) -> None:
        self.submitted: list[str] = []
        self.guarded: list[tuple[str, tuple[str, ...]]] = []
        self._all_succeeded = all_succeeded
        self._ran_nodes = ran_nodes
        self._foreign = foreign

    async def submit_published_version(self, slug: str, **_: Any) -> str:
        self.submitted.append(slug)
        return "ae-run-1"

    async def foreign_published_dag(
        self, slug: str, *, expected: tuple[str, ...]
    ) -> str:
        self.guarded.append((slug, tuple(expected)))
        return self._foreign

    async def poll_native_status(self, run_id: str, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=run_id,
            all_nodes_succeeded=self._all_succeeded,
            nodes=[SimpleNamespace(name=name) for name in self._ran_nodes],
            status=SimpleNamespace(
                value="Success" if self._all_succeeded else "Failed"
            ),
        )


def _verifier(count: int | None) -> Callable[[str, int], Awaitable[int | None]]:
    """A `SeedVerifier` that answers one scripted read-back.

    ``None`` means the search could not be read; an int is a count the verifier
    has already waited out on its own budget, which is what lets ``0`` be graded
    as "still nothing" rather than "not indexed yet" (see the protocol).
    """

    async def _verify(_qualified_name: str, _expected: int) -> int | None:
        return count

    return _verify


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: AssetValidationReport | None = None,
    all_succeeded: bool = True,
    ran_nodes: tuple[str, ...] = (SEED_PUBLISH_NODE_ID,),
    foreign: str = "",
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
    fake = _FakeAE(all_succeeded=all_succeeded, ran_nodes=ran_nodes, foreign=foreign)
    return fake, uploaded


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
            verify=_verifier(4),
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
            verify=_verifier(4),
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
                verify=_verifier(4),
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
                verify=_verifier(4),
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
                verify=_verifier(0),
            )
        assert f"{_PREFIX_ROOT}/transformed" in str(caught.value)
        assert "ae-run-1" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_short_count_is_reported_but_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Short is not the same failure as zero. The prefix was readable and
        publish wrote from it, so this is indexing lag or a partial publish —
        and whether a short seed is survivable is the consuming run's call, not
        the seed's. Failing on a count that is still climbing would red a leg
        for being slow, which is exactly what the old 15s budget did."""
        ae, _uploaded = _wire(monkeypatch)
        seeded = await seed_assets(
            _resolved(),
            store=object(),
            ae=ae,
            plan=_plan(),
            verify=_verifier(2),
        )
        assert seeded.qualified_name == _CONNECTION_QN

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
            verify=_verifier(None),
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

        async def _record(qualified_name: str, _expected: int) -> int | None:
            calls.append(qualified_name)
            return 0

        with pytest.raises(SeedPublishFailedError):
            await seed_assets(
                _resolved(), store=object(), ae=ae, plan=_plan(), verify=_record
            )
        assert calls == []


class TestTheGraphThatRuns:
    """The seed's own node, or nothing — never some app's crawl."""

    @pytest.mark.asyncio
    async def test_the_submit_names_only_the_slug(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No envelope, so no app identity, so no manifest for Heracles to
        publish over the seed. That absence is the whole fix: naming the app
        under test ran its ``extract`` / ``publish`` instead of the seed, and
        naming publish instead is an unconditional 500 (publish serves no
        manifest)."""
        ae, _uploaded = _wire(monkeypatch)
        await seed_assets(
            _resolved(), store=object(), ae=ae, plan=_plan(), verify=_verifier(4)
        )
        assert ae.submitted == ["slug-1"]

    @pytest.mark.asyncio
    async def test_the_guard_asks_for_the_seeds_own_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seed's node id is unique to the seed, so — unlike teardown's,
        which is the delete app's own — any other name at all is foreign."""
        ae, _uploaded = _wire(monkeypatch)
        await seed_assets(
            _resolved(), store=object(), ae=ae, plan=_plan(), verify=_verifier(4)
        )
        assert ae.guarded == [("slug-1", (SEED_PUBLISH_NODE_ID,))]

    @pytest.mark.asyncio
    async def test_a_substituted_version_is_refused_before_the_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The poll is the expensive half: a connector's crawl spends minutes
        failing — or succeeding against the connection under test — while
        nothing seeds."""
        ae, _uploaded = _wire(
            monkeypatch, foreign="AE serves version 7 with node(s) extract, publish"
        )
        with pytest.raises(SeedDagSupersededError) as caught:
            await seed_assets(
                _resolved(), store=object(), ae=ae, plan=_plan(), verify=_verifier(4)
            )
        assert "extract, publish" in str(caught.value)
        assert SEED_PUBLISH_NODE_ID in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_foreign_run_is_refused_even_when_it_went_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-poll read can be too early; the run's own node names cannot
        be. Checked *before* success, because a substituted run that happens to
        succeed is the shape that greens a leg while seeding nothing — and
        before FND-1766 the only signal was ``AE status=Failed``, which reads as
        "publish failed" rather than "the wrong graph ran"."""
        ae, _uploaded = _wire(monkeypatch, ran_nodes=("extract", "publish"))
        with pytest.raises(SeedDagSupersededError) as caught:
            await seed_assets(
                _resolved(), store=object(), ae=ae, plan=_plan(), verify=_verifier(4)
            )
        assert "extract, publish" in str(caught.value)
        assert "ae-run-1" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_run_reporting_no_nodes_is_not_treated_as_foreign(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty node list is an unanswered read, not a substitution. Failing
        on one would turn a status blip into a seed defect."""
        ae, _uploaded = _wire(monkeypatch, ran_nodes=())
        seeded = await seed_assets(
            _resolved(), store=object(), ae=ae, plan=_plan(), verify=_verifier(4)
        )
        assert seeded.ae_run_id == "ae-run-1"
