"""Tests for N entrypoint DAG runs in one suite, against one connection (FND-1157).

``BaseE2ETest`` used to bind a suite to exactly one DAG: ``entrypoint`` and
``manifest_path`` were ``ClassVar``\\s and ``run_full_dag()`` took no arguments.
That is fine while every entrypoint is independent, and stops being fine the
moment one entrypoint consumes an artifact another *produces* — a miner whose
lineage resolution reads an entity cache only a crawl of the same connection
writes. ``seed_prerequisites()`` cannot help there: it writes to Atlas through
pyatlan, and the artifact lives in object storage, so the only thing that
produces it is a real crawler DAG run.

Three properties carry the design and each is asserted directly rather than
left implied:

* **Inertness.** A suite that declares no ``dag_runs`` behaves exactly as
  before — same single submit, same AE workflow name, same grading. Every
  control class below omits ``dag_runs``.
* **Per-run resolution.** Identity *and* expectations resolve per run. The
  expectations are the load-bearing half: they decide which Atlas probes run at
  all, so a crawl graded with a miner's expectations would not merely be graded
  leniently — the readings its grading needs would never be taken.
* **One connection, one teardown.** However many runs, the suite mints one
  connection and purges it once, in ``teardown_method``, which pytest
  guarantees on pass, fail and error.

No tenant needed: the AE client and the Atlas seam are both replaced with
recorders, the same way ``test_non_publishing_entrypoint.py`` does it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.e2e._errors import (
    AmbiguousDAGRunError,
    DeployedManifestMismatchError,
)
from application_sdk.testing.e2e.base import (
    BaseE2ETest,
    DAGSpec,
    FullDAGOutcome,
    ResolvedDAG,
)
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
    PublishedVersion,
)
from application_sdk.testing.harness import atlas as atlas_api
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.outcome import Settled

# ---------------------------------------------------------------------------
# Fixtures for the two things a run reads from outside itself
# ---------------------------------------------------------------------------


def _node(name: str, status: DAGNodeStatus) -> DAGNodeResult:
    return DAGNodeResult(
        name=name,
        status=status,
        started_at_ms=None,
        completed_at_ms=None,
        error_message=None,
    )


def _succeeded(*names: str) -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[_node(n, DAGNodeStatus.SUCCEEDED) for n in names],
    )


def _failed(*names: str) -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.FAILED,
        nodes=[_node(n, DAGNodeStatus.FAILED) for n in names],
    )


CRAWLER_MANIFEST = "app/generated/crawler/manifest.json"
MINER_MANIFEST = "app/generated/miner/manifest.json"


def _write_manifest(root: Path, entrypoint: str, *nodes: str) -> str:
    """Write ``<root>/app/generated/<ep>/manifest.json``, return its repo path."""
    relative = Path("app") / "generated" / entrypoint / "manifest.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    dag = {
        name: {"app_name": "{app_name}", "inputs": {"task_queue": "atlan-{app_name}"}}
        for name in nodes
    }
    path.write_text(json.dumps({"dag": dag}), encoding="utf-8")
    return str(relative)


@pytest.fixture(autouse=True)
def _manifests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both entrypoints' manifests, at the paths the suites below declare.

    ``_seed_dag_from_manifest`` resolves a relative ``manifest_path`` against the
    working directory, so writing them under a chdir'd tmp_path lets the test
    classes carry the same literal paths a real connector repo carries.
    """
    _write_manifest(tmp_path, "crawler", "extract", "publish")
    _write_manifest(tmp_path, "miner", "extract")
    monkeypatch.chdir(tmp_path)


@dataclass
class _Submit:
    """One AE submit, as the fake client saw it."""

    slug: str
    entrypoint: str


class _FakeAE:
    """Records the AE writes, and answers each poll from a scripted queue."""

    def __init__(self, *results: DAGRunResult) -> None:
        self._results = list(results)
        self.created_names: list[str] = []
        self.published: list[tuple[str, int]] = []
        self.submits: list[_Submit] = []

    async def create_workflow(self, *, name: str, description: str) -> str:
        self.created_names.append(name)
        return f"slug-{len(self.created_names)}"

    async def wait_for_slug(self, slug: str) -> None:
        return None

    async def create_version(self, slug: str, body: dict[str, Any]) -> int:
        return int(body["version"])

    async def publish_version(self, slug: str, version: int) -> None:
        self.published.append((slug, version))

    async def submit_workflow(self, payload: dict[str, Any], **kwargs: Any) -> str:
        self.submits.append(
            _Submit(
                slug=str(kwargs.get("slug", "")),
                entrypoint=str(payload.get("metadata", {}).get("entrypoint", "")),
            )
        )
        return f"run-{len(self.submits)}"

    async def poll_native_status(self, run_id: str, **_kwargs: Any) -> DAGRunResult:
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

    async def get_published_version(self, slug: str) -> None:
        return None

    async def probe_run_is_listed(self, slug: str, run_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


@dataclass
class _AtlasCalls:
    """Which Atlas reads each run made, and what they were told to say."""

    connection_found: bool = True
    total: int = 1
    lineage: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    polled_connection: list[str] = field(default_factory=list)
    counted: list[Sequence[str]] = field(default_factory=list)
    counted_total: list[str] = field(default_factory=list)
    counted_lineage: list[str] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _settled(value: Any) -> Settled[Any]:
            return Settled(label="fake", attempts=1, elapsed=timedelta(0), value=value)

        async def _poll_for_connection(
            _client: object, qualified_name: str, **_kwargs: Any
        ) -> Any:
            self.polled_connection.append(qualified_name)
            return _settled(self.connection_found)

        async def _count_assets(
            _client: object, _qn: str, type_names: Sequence[str]
        ) -> Any:
            self.counted.append(tuple(type_names))
            return _settled({name: self.counts.get(name, 0) for name in type_names})

        async def _count_total(_client: object, qn: str) -> Any:
            self.counted_total.append(qn)
            return _settled(self.total)

        async def _count_lineage(
            _client: object, qn: str, _type_names: Sequence[str]
        ) -> Any:
            self.counted_lineage.append(qn)
            return _settled(dict(self.lineage))

        async def _sample(
            _client: object, _qn: str, type_names: Sequence[str], **_kwargs: Any
        ) -> Any:
            return _settled({name: [] for name in type_names})

        async def _create_connection(_client: object, **kwargs: Any) -> str:
            self.created.append(kwargs)
            return str(kwargs["qualified_name"])

        async def _purge(_client: object, qualified_name: str) -> Any:
            self.purged.append(qualified_name)
            return None

        monkeypatch.setattr(atlas_api, "poll_for_connection", _poll_for_connection)
        monkeypatch.setattr(atlas_api, "count_assets", _count_assets)
        monkeypatch.setattr(atlas_api, "count_total_assets", _count_total)
        monkeypatch.setattr(atlas_api, "count_lineage", _count_lineage)
        monkeypatch.setattr(atlas_api, "sample_qualified_names", _sample)
        monkeypatch.setattr(atlas_api, "create_connection", _create_connection)
        monkeypatch.setattr("application_sdk.testing.e2e.base.purge_connection", _purge)
        monkeypatch.setattr(
            BaseE2ETest, "_atlas_client", lambda _self: _null_atlas_client()
        )


@asynccontextmanager
async def _null_atlas_client() -> AsyncIterator[object]:
    yield object()


def _wire(
    harness: BaseE2ETest,
    ae: _FakeAE,
    monkeypatch: pytest.MonkeyPatch,
    **atlas_overrides: Any,
) -> _AtlasCalls:
    """Give *harness* everything ``setup_method`` would, minus the tenant."""
    calls = _AtlasCalls(**atlas_overrides)
    calls.install(monkeypatch)
    harness._ae = ae  # type: ignore[assignment]
    harness._minter = Minter.from_environment({})  # type: ignore[attr-defined]
    harness.run_id = 7  # type: ignore[attr-defined]
    harness.source_available = True  # type: ignore[attr-defined]
    harness.connection_qualified_name = "default/bundle/1"  # type: ignore[attr-defined]
    harness.connection_display_name = "e2e"  # type: ignore[attr-defined]
    harness._auto_admin_roles = ()  # type: ignore[attr-defined]
    harness._auto_admin_users = ()  # type: ignore[attr-defined]
    harness._active_dag = None  # type: ignore[attr-defined]
    harness._connection_seeded = False  # type: ignore[attr-defined]
    harness._seed_version = None  # type: ignore[attr-defined]
    harness._node_dispatch = {}  # type: ignore[attr-defined]
    harness._expected_node_identities = {}  # type: ignore[attr-defined]
    harness.dag_outcomes = []  # type: ignore[attr-defined]
    harness._extract_task_queue = lambda: "atlan-bundle-default"  # type: ignore[method-assign]
    return calls


# ---------------------------------------------------------------------------
# Suites under test
# ---------------------------------------------------------------------------


class _Crawler(BaseE2ETest):
    """Control: one DAG, declared the way every suite declared it before."""

    connector_short_name = "bundle"
    argo_package_name = "@atlan/bundle"
    argo_template_name = "atlan-bundle"
    manifest_path = "app/generated/crawler/manifest.json"
    expect_lineage = False
    required_dag_nodes = ("extract", "publish")

    # Keep the unit tests off the wall clock: the deployed-manifest check would
    # otherwise poll a fake AE that never publishes for its full 60s budget, per
    # run. Its own per-run behaviour is asserted in TestManifestIdentityIsPerRun.
    assert_deployed_manifest = False
    atlas_poll_interval_seconds = 0
    atlas_poll_timeout_seconds = 1
    atlas_asset_poll_interval_seconds = 0
    atlas_asset_poll_timeout_seconds = 1
    # Nothing here should drop an evidence bundle into the repo.
    evidence_dir = ""


class _Miner(BaseE2ETest):
    """A miner: publishes no inventory, graded on its DAG."""

    connector_short_name = "bundle"
    argo_package_name = "@atlan/bundle"
    argo_template_name = "atlan-bundle"
    manifest_path = "app/generated/miner/manifest.json"
    expect_connection = False
    expect_lineage = False
    require_nonempty_assets = False
    required_dag_nodes = ("extract",)

    # Keep the unit tests off the wall clock: the deployed-manifest check would
    # otherwise poll a fake AE that never publishes for its full 60s budget, per
    # run. Its own per-run behaviour is asserted in TestManifestIdentityIsPerRun.
    assert_deployed_manifest = False
    atlas_poll_interval_seconds = 0
    atlas_poll_timeout_seconds = 1
    atlas_asset_poll_interval_seconds = 0
    atlas_asset_poll_timeout_seconds = 1
    # Nothing here should drop an evidence bundle into the repo.
    evidence_dir = ""


# ---------------------------------------------------------------------------
# Inertness
# ---------------------------------------------------------------------------


class TestTheDefaultIsUnchanged:
    """A suite that declares no runs is the suite it was before FND-1157."""

    def test_dag_runs_defaults_empty(self) -> None:
        assert BaseE2ETest.dag_runs == ()
        assert _Miner.dag_runs == ()

    def test_no_spec_resolves_to_the_class(self) -> None:
        dag = _Miner().resolve_dag(None)

        assert dag.entrypoint == "miner"
        assert dag.manifest_path == _Miner.manifest_path
        assert dag.expect_connection is False
        assert dag.expect_lineage is False
        assert dag.require_nonempty_assets is False
        assert dag.required_dag_nodes == ("extract",)
        assert dag.label == "miner"

    def test_an_empty_spec_resolves_identically(self) -> None:
        """So an explicitly-spelled default run is indistinguishable from the
        implicit one — including in the AE workflow name."""
        harness = _Miner()

        assert harness.resolve_dag(DAGSpec()) == harness.resolve_dag(None)

    def test_the_default_run_keeps_the_bare_workflow_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Crawler()
        _wire(harness, _FakeAE(_succeeded("extract")), monkeypatch)

        assert harness._ae_workflow_name_suffix() == ""
        assert harness._ae_workflow_spec().name == "bundle-e2e-full-ci-7"

    def test_run_full_dag_still_takes_no_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The signature grew an optional parameter; no caller has to pass it."""
        harness = _Miner()
        ae = _FakeAE(_succeeded("extract"))
        _wire(harness, ae, monkeypatch)

        outcome = harness.run_full_dag()

        assert outcome.succeeded is True
        assert [s.entrypoint for s in ae.submits] == ["miner"]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolveDag:
    """Each ``None`` inherits; each set field overrides; nothing else moves."""

    def test_a_spec_overrides_only_what_it_sets(self) -> None:
        dag = _Miner().resolve_dag(
            DAGSpec(
                manifest_path="app/generated/crawler/manifest.json",
                expect_connection=True,
                expected_min_asset_counts={"Table": 3},
            )
        )

        assert dag.entrypoint == "crawler"
        assert dag.expect_connection is True
        assert dag.expected_min_asset_counts == {"Table": 3}
        # Untouched by the spec, so still the class's.
        assert dag.expect_lineage is False
        assert dag.required_dag_nodes == ("extract",)
        assert dag.require_nonempty_assets is False

    def test_the_entrypoint_derives_from_the_specs_own_manifest(self) -> None:
        """Not from the class's — that is the bug the per-run identity fixes."""
        dag = _Miner().resolve_dag(
            DAGSpec(manifest_path="app/generated/crawler/manifest.json")
        )

        assert dag.entrypoint == "crawler"

    def test_an_explicit_entrypoint_wins_over_the_derivation(self) -> None:
        dag = _Miner().resolve_dag(
            DAGSpec(
                entrypoint="extract-metadata",
                manifest_path="app/generated/crawler/manifest.json",
            )
        )

        assert dag.entrypoint == "extract-metadata"

    def test_a_single_entrypoint_manifest_resolves_to_no_selector(self) -> None:
        """Empty means "single-entrypoint app" — AE fetches the bare manifest."""
        dag = _Crawler().resolve_dag(
            DAGSpec(manifest_path="app/generated/manifest.json")
        )

        assert dag.entrypoint == ""
        assert dag.label == "default"

    def test_a_label_names_the_run(self) -> None:
        dag = _Crawler().resolve_dag(DAGSpec(label="incremental"))

        assert dag.label == "incremental"

    def test_resolution_copies_the_declared_maps(self) -> None:
        """A caller mutating its own dict must not retune a resolved run."""
        floors = {"Table": 1}
        dag = _Crawler().resolve_dag(DAGSpec(expected_min_asset_counts=floors))
        floors["Table"] = 99

        assert dag.expected_min_asset_counts == {"Table": 1}


# ---------------------------------------------------------------------------
# One run against a spec
# ---------------------------------------------------------------------------


class TestRunFullDagWithASpec:
    def test_the_spec_picks_the_manifest_and_the_entrypoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Miner()
        ae = _FakeAE(_succeeded("extract", "publish"))
        _wire(harness, ae, monkeypatch)

        harness.run_full_dag(
            DAGSpec(manifest_path=CRAWLER_MANIFEST, expect_connection=True)
        )

        assert [s.entrypoint for s in ae.submits] == ["crawler"]
        # The seed DAG came from the spec's manifest, not the class's.
        assert set(harness._expected_node_identities) == {"extract", "publish"}

    def test_the_specs_expectations_decide_which_probes_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing half. A crawl run inside a miner suite has to be able
        to observe the connection it landed, and the class says it never will."""
        harness = _Miner()
        calls = _wire(
            harness, _FakeAE(_succeeded("extract", "publish")), monkeypatch, total=12
        )

        outcome = harness.run_full_dag(
            DAGSpec(manifest_path=CRAWLER_MANIFEST, expect_connection=True)
        )

        assert calls.polled_connection == ["default/bundle/1"]
        assert outcome.connection_in_atlas is True
        assert outcome.connection_expected is True
        assert outcome.total_assets == 12

    def test_the_class_run_is_restored_afterwards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Miner()
        _wire(harness, _FakeAE(_succeeded("extract", "publish")), monkeypatch)

        harness.run_full_dag(
            DAGSpec(manifest_path=CRAWLER_MANIFEST, expect_connection=True)
        )

        assert harness._dag == harness.resolve_dag(None)
        assert harness._resolved_entrypoint() == "miner"

    def test_a_non_default_run_gets_its_own_ae_workflow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``create_workflow`` reuses a workflow of the same name, so without the
        suffix the second run would publish its DAG over the first's."""
        harness = _Miner()
        ae = _FakeAE(_succeeded("extract", "publish"))
        _wire(harness, ae, monkeypatch)

        harness.run_full_dag(DAGSpec(manifest_path=CRAWLER_MANIFEST))
        harness.run_full_dag()

        assert ae.created_names == [
            "bundle-e2e-full-ci-7-crawler",
            "bundle-e2e-full-ci-7",
        ]


# ---------------------------------------------------------------------------
# N runs in one suite
# ---------------------------------------------------------------------------


class _CrawlThenMine(_Miner):
    """A miner suite that crawls its own connection first — FND-1157's case."""

    dag_runs = (
        DAGSpec(
            manifest_path=CRAWLER_MANIFEST,
            expect_connection=True,
            require_nonempty_assets=True,
            required_dag_nodes=("extract", "publish"),
        ),
        DAGSpec(),
    )


class TestDeclaredRuns:
    def test_each_declared_run_is_submitted_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _CrawlThenMine()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch, total=4)

        harness.test_full_dag_runs_end_to_end()

        assert [s.entrypoint for s in ae.submits] == ["crawler", "miner"]

    def test_one_outcome_per_run_graded_on_its_own_terms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not merged into a composite: the crawl observed a connection and
        assets, the mine observed neither and passes on its DAG."""
        harness = _CrawlThenMine()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch, total=4)

        harness.test_full_dag_runs_end_to_end()

        crawl, mine = harness.dag_outcomes
        assert crawl.connection_expected is True
        assert crawl.total_assets == 4
        assert mine.connection_expected is False
        assert mine.total_assets == 0

    def test_every_run_shares_the_one_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _CrawlThenMine()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch, total=4)

        harness.test_full_dag_runs_end_to_end()

        assert {o.connection_qualified_name for o in harness.dag_outcomes} == {
            "default/bundle/1"
        }

    def test_teardown_is_still_one_purge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """N runs, one cleanup — and it stays in ``teardown_method``, which
        pytest runs on pass, fail and error alike."""
        harness = _CrawlThenMine()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        calls = _wire(harness, ae, monkeypatch, total=4)

        harness.test_full_dag_runs_end_to_end()
        harness.teardown_method(None)

        assert calls.purged == ["default/bundle/1"]

    def test_a_failing_run_stops_the_sequence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mine whose prerequisite crawl failed has nothing left to prove, and
        running it anyway would report the crawl's failure as the miner's."""
        harness = _CrawlThenMine()
        ae = _FakeAE(_failed("extract"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch)

        with pytest.raises(AssertionError):
            harness.test_full_dag_runs_end_to_end()

        assert [s.entrypoint for s in ae.submits] == ["crawler"]

    def test_a_crawl_that_lands_nothing_fails_the_leg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prerequisite is graded, not merely executed — a green DAG that
        published nothing is exactly the shape FND-1147 found."""
        harness = _CrawlThenMine()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch, total=0)

        with pytest.raises(AssertionError) as exc:
            harness.test_full_dag_runs_end_to_end()

        assert "did not meet expectations" in str(exc.value)
        assert [s.entrypoint for s in ae.submits] == ["crawler"]

    def test_each_run_gets_its_own_ae_workflow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _CrawlThenMine()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch, total=4)

        harness.test_full_dag_runs_end_to_end()

        assert ae.created_names == [
            "bundle-e2e-full-ci-7-crawler",
            "bundle-e2e-full-ci-7",
        ]

    def test_seed_prerequisites_still_runs_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        class _Seeding(_CrawlThenMine):
            def seed_prerequisites(self) -> None:
                seen.append("seed")

            def assert_dag_outcome(
                self, dag: ResolvedDAG, outcome: FullDAGOutcome
            ) -> None:
                seen.append(dag.label)
                super().assert_dag_outcome(dag, outcome)

        harness = _Seeding()
        ae = _FakeAE(_succeeded("extract", "publish"), _succeeded("extract"))
        _wire(harness, ae, monkeypatch, total=4)

        harness.test_full_dag_runs_end_to_end()

        assert seen == ["seed", "crawler", "miner"]


# ---------------------------------------------------------------------------
# assert_dag_outcome
# ---------------------------------------------------------------------------


class TestAssertDagOutcome:
    def test_the_hook_sees_the_run_that_produced_the_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graded: list[tuple[str, bool]] = []

        class _Custom(_CrawlThenMine):
            def assert_dag_outcome(
                self, dag: ResolvedDAG, outcome: FullDAGOutcome
            ) -> None:
                graded.append((dag.label, dag.expect_connection))
                super().assert_dag_outcome(dag, outcome)

        harness = _Custom()
        _wire(
            harness,
            _FakeAE(_succeeded("extract", "publish"), _succeeded("extract")),
            monkeypatch,
            total=4,
        )

        harness.test_full_dag_runs_end_to_end()

        assert graded == [("crawler", True), ("miner", False)]

    def test_an_override_can_add_a_terminal_assertion_per_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The miner's own evidence — the assertion a crawl-only ladder cannot
        make — is the suite's to add, on the run it belongs to."""

        class _LineageAsserting(_CrawlThenMine):
            def assert_dag_outcome(
                self, dag: ResolvedDAG, outcome: FullDAGOutcome
            ) -> None:
                super().assert_dag_outcome(dag, outcome)
                if dag.label == "miner":
                    raise AssertionError("no lineage under the mined connection")

        harness = _LineageAsserting()
        _wire(
            harness,
            _FakeAE(_succeeded("extract", "publish"), _succeeded("extract")),
            monkeypatch,
            total=4,
        )

        with pytest.raises(AssertionError, match="no lineage"):
            harness.test_full_dag_runs_end_to_end()

    def test_the_single_run_path_goes_through_the_same_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a suite that overrides it is honoured whether or not it declares
        ``dag_runs`` — one grading path, not two."""
        graded: list[str] = []

        class _Custom(_Miner):
            def assert_dag_outcome(
                self, dag: ResolvedDAG, outcome: FullDAGOutcome
            ) -> None:
                graded.append(dag.label)

        harness = _Custom()
        _wire(harness, _FakeAE(_succeeded("extract")), monkeypatch)

        harness.test_full_dag_runs_end_to_end()

        assert graded == ["miner"]


# ---------------------------------------------------------------------------
# Connection ownership
# ---------------------------------------------------------------------------


class TestConnectionIsMintedOnce:
    def test_a_second_seed_does_not_create_a_second_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A duplicate would survive the single purge teardown performs."""
        harness = _Miner()
        calls = _wire(harness, _FakeAE(_succeeded("extract")), monkeypatch)

        first = harness.seed_connection()
        second = harness.seed_connection()

        assert first == second == "default/bundle/1"
        assert [c["qualified_name"] for c in calls.created] == ["default/bundle/1"]

    def test_the_probe_still_runs_on_a_reused_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is a wait, not a create: a caller that passed one is owed it."""
        harness = _Miner()
        _wire(harness, _FakeAE(_succeeded("extract")), monkeypatch)
        probes = 0

        def _probe() -> None:
            nonlocal probes
            probes += 1

        harness.seed_connection(probe=_probe)
        harness.seed_connection(probe=_probe)

        assert probes == 2


# ---------------------------------------------------------------------------
# Static validation
# ---------------------------------------------------------------------------


class TestDagRunsValidation:
    def test_two_runs_resolving_to_one_label_are_rejected(self) -> None:
        class _Colliding(_Miner):
            dag_runs = (
                DAGSpec(expect_connection=True),
                DAGSpec(expect_connection=False),
            )

        with pytest.raises(AmbiguousDAGRunError, match="resolve to the label"):
            _Colliding()._validate_dag_runs()

    def test_distinct_labels_resolve_the_collision(self) -> None:
        class _Labelled(_Miner):
            dag_runs = (
                DAGSpec(expect_connection=True, label="first-pass"),
                DAGSpec(expect_connection=False),
            )

        _Labelled()._validate_dag_runs()

    def test_identical_repeated_runs_are_allowed(self) -> None:
        """Two runs of the same DAG — a crawl, then the same crawl again to
        exercise the incremental path — are one workflow by intent."""

        class _Twice(_Miner):
            dag_runs = (DAGSpec(), DAGSpec())

        _Twice()._validate_dag_runs()

    def test_a_pinned_slug_cannot_carry_several_runs(self) -> None:
        class _Pinned(_Miner):
            ae_workflow_slug = "someone-elses-workflow"
            dag_runs = (DAGSpec(label="a"), DAGSpec(label="b"))

        with pytest.raises(AmbiguousDAGRunError, match="ae_workflow_slug"):
            _Pinned()._validate_dag_runs()

    def test_a_pinned_slug_is_fine_for_one_run(self) -> None:
        class _Pinned(_Miner):
            ae_workflow_slug = "someone-elses-workflow"

        _Pinned()._validate_dag_runs()


# ---------------------------------------------------------------------------
# The deployed-manifest identity check
# ---------------------------------------------------------------------------


class _PublishingAE(_FakeAE):
    """An AE that supersedes the seed with a DAG the caller chooses."""

    def __init__(self, published_dag: dict[str, Any], *results: DAGRunResult) -> None:
        super().__init__(*results)
        self._published_dag = published_dag
        self._seed = 0

    async def create_version(self, slug: str, body: dict[str, Any]) -> int:
        self._seed = int(body["version"])
        return self._seed

    async def get_published_version(self, slug: str) -> PublishedVersion:
        return PublishedVersion(version=self._seed + 1, dag=dict(self._published_dag))


def _published_dag(*nodes: str) -> dict[str, Any]:
    """What AE serves after Heracles re-fetches the tenant pod's manifest."""
    return {
        name: {"app_name": "bundle", "inputs": {"task_queue": "atlan-bundle"}}
        for name in nodes
    }


class TestManifestIdentityIsPerRun:
    """The check compares the *run's* manifest, not the class's.

    Keyed off the class, a crawl run inside a miner suite would compare the
    crawler DAG AE published against the miner DAG the class declares and fail
    every time — the check would have to be turned off to run two DAGs at all,
    which is the opposite of what it is for.
    """

    def test_a_matching_published_dag_passes_for_the_specs_manifest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Miner()
        harness.assert_deployed_manifest = True  # type: ignore[misc]
        harness.deployed_manifest_timeout_seconds = 1  # type: ignore[misc]
        harness.deployed_manifest_poll_interval_seconds = 0  # type: ignore[misc]
        ae = _PublishingAE(
            _published_dag("extract", "publish"), _succeeded("extract", "publish")
        )
        _wire(harness, ae, monkeypatch)

        harness.run_full_dag(
            DAGSpec(manifest_path=CRAWLER_MANIFEST, expect_connection=True)
        )

        assert [s.entrypoint for s in ae.submits] == ["crawler"]

    def test_the_classs_manifest_is_not_what_is_compared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: the same published DAG against the class's own run is a
        mismatch, so the test above is not passing by accident."""
        harness = _Miner()
        harness.assert_deployed_manifest = True  # type: ignore[misc]
        harness.deployed_manifest_timeout_seconds = 1  # type: ignore[misc]
        harness.deployed_manifest_poll_interval_seconds = 0  # type: ignore[misc]
        ae = _PublishingAE(
            _published_dag("extract", "publish"), _succeeded("extract", "publish")
        )
        _wire(harness, ae, monkeypatch)

        with pytest.raises(DeployedManifestMismatchError) as exc:
            harness.run_full_dag()

        # And the message names the manifest THIS run was built from.
        assert MINER_MANIFEST in str(exc.value)


# ---------------------------------------------------------------------------
# Failure evidence
# ---------------------------------------------------------------------------


class TestEvidenceNamesTheRun:
    def test_the_bundle_says_which_run_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Collected inside the failing run's own block. Outside it, the active
        run is already restored and the bundle would name the class's default —
        on a suite running several DAGs, the one field that says which run
        failed would name the wrong one."""
        readings: list[dict[str, object]] = []

        class _Recording(_CrawlThenMine):
            def _collect_failure_evidence(
                self, failure: BaseException, outcome: FullDAGOutcome | None
            ) -> None:
                readings.append(dict(self._failure_evidence(failure, outcome).readings))

        harness = _Recording()
        _wire(harness, _FakeAE(_failed("extract")), monkeypatch)

        with pytest.raises(AssertionError):
            harness.test_full_dag_runs_end_to_end()

        assert readings[0]["dag"] == "crawler"
        assert readings[0]["entrypoint"] == "crawler"
