"""Unit tests for the harness scaffold's concrete surface.

Covers what FND-238 actually lands: the outcome vocabulary, the ``Budget`` type,
the ``AppConfig`` -> ``AppUnderTest`` rename with its deprecation, and the
wiring claims the issue makes about coverage and the capability manifest.

Every stub is asserted to raise ``NotImplementedError`` naming its child issue,
so a later child cannot quietly ship a half-implementation that returns ``None``.
"""

from __future__ import annotations

import warnings
from datetime import timedelta

import pytest

from application_sdk.testing.harness import (
    AppUnderTest,
    Budget,
    BudgetProfile,
    Expired,
    HarnessNotBuiltError,
    Indeterminate,
    NeverStarted,
    Settled,
    Stalled,
    assert_settled,
    hold_stable,
    poll_until,
)
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.cluster import (
    ClusterReader,
    CustomResourceReader,
    DeploymentState,
    PodPhase,
    PodState,
    ResourceRef,
    ServiceTarget,
)
from application_sdk.testing.harness.temporal import (
    TemporalReader,
    WorkflowExecutionStatus,
)

# ---------------------------------------------------------------------------
# Outcome vocabulary
# ---------------------------------------------------------------------------


def _budget() -> Budget:
    return Budget(timeout=timedelta(seconds=60), poll_interval=timedelta(seconds=5))


def test_settled_carries_the_value_and_the_wait_shape() -> None:
    outcome = Settled(
        label="AE run native status",
        attempts=3,
        elapsed=timedelta(seconds=12),
        value={"extract": "Succeeded"},
    )
    assert outcome.value == {"extract": "Succeeded"}
    assert outcome.label == "AE run native status"


def test_stalled_carries_the_fingerprint_that_froze() -> None:
    """The single most useful line in the report: what stopped changing."""
    outcome = Stalled(
        label="AE run native status",
        attempts=40,
        elapsed=timedelta(minutes=7),
        stall_window=timedelta(minutes=5),
        fingerprint="OK OK RUN -",
        last={"publish": "Running"},
    )
    assert outcome.fingerprint == "OK OK RUN -"


def test_never_started_is_distinct_from_expired() -> None:
    """Different diagnoses: dispatch failed, versus work was slow."""
    never = NeverStarted(
        label="AE run",
        attempts=18,
        elapsed=timedelta(minutes=3),
        grace=timedelta(minutes=3),
    )
    expired = Expired(
        label="AE run",
        attempts=60,
        elapsed=timedelta(minutes=10),
        budget=timedelta(minutes=10),
    )
    assert type(never) is not type(expired)


def test_indeterminate_retains_the_cause_object() -> None:
    """Retained rather than stringified so a caller can classify it without
    re-parsing a message."""
    cause = TimeoutError("vcluster token expired")
    outcome = Indeterminate(
        label="cluster deployments",
        attempts=4,
        elapsed=timedelta(seconds=20),
        cause=cause,
        transient_failures=3,
    )
    assert outcome.cause is cause
    assert outcome.transient_failures == 3


def test_outcomes_are_immutable() -> None:
    outcome = Settled(label="x", attempts=1, elapsed=timedelta(0), value=1)
    with pytest.raises((AttributeError, TypeError)):
        outcome.value = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_budget_defaults_leave_every_guard_off_except_the_heartbeat() -> None:
    """A guard that is on by default is a guard a caller did not ask for."""
    budget = _budget()
    assert budget.start_grace is None
    assert budget.stall_timeout is None
    assert budget.max_transient_failures == 0
    assert budget.retry_after_budget is None
    assert budget.heartbeat == timedelta(seconds=30)


def test_budget_has_no_clock_mode() -> None:
    """D3: the `elapsed += interval` accumulator is already gone from the tree,
    so there is no behaviour to preserve and no mode to select. Shipping one
    would be reintroducing the bug as a supported option."""
    assert not hasattr(_budget(), "clock")


def test_budget_profile_names_a_tier() -> None:
    profile = BudgetProfile(name="connector_ci", budgets={"ae_poll": _budget()})
    assert profile.budgets["ae_poll"].timeout == timedelta(seconds=60)


# ---------------------------------------------------------------------------
# The app-under-test rename
# ---------------------------------------------------------------------------


def test_app_under_test_keeps_only_the_fields_anything_reads() -> None:
    app = AppUnderTest(app_name="my-app", namespace="app-my-app")
    assert app.handler_port == 8000
    assert not hasattr(app, "app_module")
    assert not hasattr(app, "image")
    assert not hasattr(app, "timeout")
    assert not hasattr(app, "worker_health_port")


def test_deprecated_app_config_still_accepts_the_dropped_fields() -> None:
    """An existing keyword AppConfig(...) call site keeps working."""
    from application_sdk.testing.e2e import AppConfig

    with pytest.warns(DeprecationWarning, match="AppUnderTest"):
        config = AppConfig(
            app_name="my-app",
            namespace="default",
            app_module="my_app.main:App",
            image="ghcr.io/org/my-app:latest",
            timeout=600,
        )
    assert config.app_name == "my-app"
    assert config.namespace == "default"
    assert config.handler_port == 8000
    assert config.app_module == "my_app.main:App"
    assert config.timeout == 600
    assert isinstance(config, AppUnderTest)


def test_deprecated_app_config_preserves_the_original_positional_order() -> None:
    """The whole reason AppConfig declares an explicit __init__.

    AppUnderTest's field order is (app_name, namespace, handler_port), so a
    dataclass-generated subclass __init__ would bind these four positionals as
    namespace="my_app.main:App" and handler_port="default" — accepted silently,
    wrong at every read. A shim that mis-binds is a silent break.
    """
    from application_sdk.testing.e2e import AppConfig

    with pytest.warns(DeprecationWarning):
        config = AppConfig(
            "my-app",
            "my_app.main:App",
            "default",
            "ghcr.io/org/my-app:latest",
        )
    assert config.app_name == "my-app"
    assert config.app_module == "my_app.main:App"
    assert config.namespace == "default"
    assert config.image == "ghcr.io/org/my-app:latest"
    assert config.handler_port == 8000


def test_deprecated_app_config_accepts_all_seven_positionally() -> None:
    from application_sdk.testing.e2e import AppConfig

    with pytest.warns(DeprecationWarning):
        config = AppConfig("a", "m", "ns", "img", 9000, 9001, 600)
    assert (config.app_name, config.namespace, config.handler_port) == ("a", "ns", 9000)
    assert (config.worker_health_port, config.timeout) == (9001, 600)


def test_deprecated_app_config_stays_mutable() -> None:
    """AppUnderTest is frozen; the original AppConfig was not. Freezing the shim
    would be a second silent break in the same class."""
    from application_sdk.testing.e2e import AppConfig

    with pytest.warns(DeprecationWarning):
        config = AppConfig(app_name="a", namespace="b")
    config.timeout = 600
    config.namespace = "c"
    assert config.timeout == 600
    assert config.namespace == "c"


def test_app_under_test_stays_frozen() -> None:
    """The shim's mutability must not leak onto the replacement."""
    app = AppUnderTest(app_name="a", namespace="b")
    with pytest.raises((AttributeError, TypeError)):
        app.namespace = "c"  # type: ignore[misc]


def test_deprecated_app_config_carries_no_instance_dict() -> None:
    """Both classes declare __slots__, so a typo'd field is a failure rather
    than a silently-ignored attribute."""
    from application_sdk.testing.e2e import AppConfig

    with pytest.warns(DeprecationWarning):
        config = AppConfig(app_name="a", namespace="b")
    assert not hasattr(config, "__dict__")


def test_deprecation_names_a_removal_version() -> None:
    from application_sdk.testing.e2e.config import APP_CONFIG_REMOVAL_VERSION, AppConfig

    with pytest.warns(DeprecationWarning) as caught:
        AppConfig(app_name="a", namespace="b")
    assert f"v{APP_CONFIG_REMOVAL_VERSION}" in str(caught[0].message)


def test_app_under_test_itself_warns_about_nothing() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        AppUnderTest(app_name="a", namespace="b")  # should not raise


# ---------------------------------------------------------------------------
# Protocols and value types
# ---------------------------------------------------------------------------


def test_deployment_state_exposes_intent_and_actual_separately() -> None:
    """`.spec.replicas` is the scaling metric; `.status.readyReplicas` lags, so
    asserting on it turns a scaling assertion into a startup race."""
    state = DeploymentState(
        name="my-app-worker",
        namespace="app-my-app",
        desired_replicas=3,
        ready_replicas=1,
        updated_replicas=3,
    )
    assert state.desired_replicas != state.ready_replicas


def test_pod_state_separates_running_from_ready() -> None:
    """A Running pod with a failing readiness probe is exactly what a
    "worker is up" assertion must not accept."""
    pod = PodState(
        name="my-app-worker-abc",
        namespace="app-my-app",
        phase=PodPhase.RUNNING,
        ready=False,
        restarts=2,
    )
    assert pod.phase is PodPhase.RUNNING
    assert pod.ready is False


def test_resource_ref_takes_the_plural_not_the_kind() -> None:
    """The plural is what the API path needs; deriving it from a Kind is the
    map that ResourceRef exists to avoid."""
    ref = ResourceRef(
        group="helm.toolkit.fluxcd.io", version="v2", plural="helmreleases"
    )
    assert ref.plural == "helmreleases"


def test_the_reader_protocols_are_structural() -> None:
    """No consumer has to inherit from these to satisfy them."""
    for protocol in (ClusterReader, CustomResourceReader, TemporalReader):
        assert getattr(protocol, "_is_protocol", False)
        assert getattr(protocol, "_is_runtime_protocol", False)


def test_a_plain_object_satisfies_cluster_reader_by_shape() -> None:
    class Fake:
        async def deployments(self, namespace, selector): ...
        async def pods(self, namespace, selector): ...
        def logs(self, namespace, selector, *, since=None): ...
        async def http(self, target, request): ...

    assert isinstance(Fake(), ClusterReader)


def test_workflow_status_vocabulary_mirrors_temporal() -> None:
    assert WorkflowExecutionStatus.RUNNING.value == "Running"
    assert WorkflowExecutionStatus.TIMED_OUT.value == "TimedOut"


# ---------------------------------------------------------------------------
# Stubs: every one names its child issue and refuses to half-work
# ---------------------------------------------------------------------------


def test_the_stub_leaf_is_also_a_notimplementederror() -> None:
    """It is a typed SDK leaf with a category and an audience, and it is still
    what Python's convention — and any reader's `except` — expects."""
    error = HarnessNotBuiltError(message="x", issue="FND-224")
    assert isinstance(error, NotImplementedError)
    assert error.code == "UNIMPLEMENTED_HARNESS_NOT_BUILT"
    assert error.issue == "FND-224"


async def test_the_three_child_c_stubs_are_gone() -> None:
    """``poll_until`` / ``hold_stable`` / ``assert_settled`` were the scaffold's
    FND-227 stubs. They are implemented now, and their own tests live in
    ``test_waiting.py`` / ``test_outcome.py``; what belongs *here* is the proof
    that the scaffold no longer holds a stub under those names, so a revert
    cannot quietly restore one."""

    async def _probe() -> int:
        return 1

    with fake_clock():
        settled = await poll_until(_probe, settled=bool, budget=_budget(), label="x")
        # A hold always spends its whole budget on the happy path, so it needs
        # the fake clock even here: the real one would sleep the full minute.
        held = await hold_stable(_probe, invariant=bool, budget=_budget(), label="x")
    assert assert_settled(settled) == 1
    assert isinstance(held, Settled)


async def test_the_child_f_stubs_are_gone() -> None:
    """``atlas`` and ``automation_engine`` were typed stubs; child F filled both.

    Their own tests live in ``harness/atlas/`` and
    ``harness/automation_engine/``; what belongs *here* is the proof the
    scaffold no longer holds a stub under those names.

    The two sketched types the move deliberately did **not** build —
    ``AERunHandle`` and ``NativeStatus`` — are asserted absent rather than
    quietly forgotten: FND-242 assigns the existing wire vocabulary to this
    half, and a second one reappearing is the regression worth catching.
    """
    from application_sdk.testing.harness import atlas, automation_engine

    assert not hasattr(automation_engine, "AERunHandle")
    assert not hasattr(automation_engine, "NativeStatus")
    # The fingerprint the sketch wanted, on the reading the move kept.
    assert "fingerprint" in dir(automation_engine.DAGRunResult)

    for module, names in (
        (atlas, ("count_assets", "sample_qualified_names")),
        (automation_engine, ("AEClient",)),
    ):
        for name in names:
            assert not isinstance(
                getattr(module, name, None), type(None)
            ), f"{module.__name__}.{name} is gone, not implemented"

    # A stub raised on call; these do not.
    assert callable(atlas.count_assets)
    assert callable(automation_engine.AEClient)


async def test_the_child_e_kubectl_reads_are_gone() -> None:
    """Child E retired the ``kubectl``-shelling reads rather than wrapping them.

    Asserted rather than eyeballed for the same reason the sibling child asserts
    that no ``asyncio.run`` is left under ``testing/``: a reintroduced
    ``kubectl get pods`` is a subprocess, a JSON parse and a ``return []`` on
    failure, and that last part is the fail-open shape the typed reader exists to
    remove. A revert would restore all three silently.
    """
    import importlib
    from pathlib import Path

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("application_sdk.testing.e2e.pods")

    from application_sdk.testing.harness.cluster import KubernetesReader

    assert isinstance(KubernetesReader(apis=lambda: _unused()), ClusterReader)
    assert isinstance(KubernetesReader(apis=lambda: _unused()), CustomResourceReader)

    # The harness's only `kubectl` *invocation* is the port-forward transport,
    # which is not a read: `kubernetes.stream`'s equivalent is a socket API, not
    # a drop-in. Matched on the quoted argv literal, so the prose that explains
    # all of this does not count as a call.
    import application_sdk.testing.e2e as e2e_pkg
    import application_sdk.testing.harness as harness_pkg

    def _shells_out(package: object) -> list[str]:
        root = Path(str(getattr(package, "__file__"))).parent
        return sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            # encoding pinned: these sources are UTF-8 and full of em-dashes, and
            # `read_text()` would decode them as cp1252 on Windows
            if '"kubectl"' in path.read_text(encoding="utf-8")
        )

    # Exactly one place in either package builds a `kubectl` argv, and it is the
    # helper that pins `--context`. `LogCollector`'s three remaining artefacts
    # (`describe`, `get pods -o wide`, `get events`) go through it rather than
    # assembling their own list, which is what keeps the evidence bundle and the
    # typed reads pointed at the same cluster.
    assert _shells_out(harness_pkg) == ["cluster/_portforward.py"]
    assert _shells_out(e2e_pkg) == []


def _unused():  # pragma: no cover — the factory is never called by these asserts
    raise AssertionError("the protocol checks are structural, not behavioural")


async def test_the_temporal_readers_are_real() -> None:
    """FND-247 filled the ``temporal`` stub: the Protocol had no backend at all,
    and ``NoWorkerOnTaskQueueError`` was inferred from three minutes of silence
    rather than read.

    Asserted here for the reason child E's deletion is: what belongs in the
    scaffold's own test file is the proof that the package no longer holds a stub
    under these names, so a revert cannot quietly restore one.
    """
    from application_sdk.testing.harness import temporal

    assert isinstance(
        temporal.TemporalServiceReader(connect=_unused), temporal.TemporalReader
    )
    for name in ("frontend_connection", "port_forwarded_connection"):
        assert callable(getattr(temporal, name))

    # The nullable twin is deliberately *not* on the Protocol: a caller waiting
    # for an AE-dispatched execution to appear needs absence as a value, and a
    # caller that already has an id wants the raise.
    assert not hasattr(temporal.TemporalReader, "find_workflow_status")
    assert callable(temporal.TemporalServiceReader.find_workflow_status)


async def test_the_queue_starter_is_real() -> None:
    """FND-246 filled one of ``starters``' three stubs. Asserted here for the
    reason the temporal and cluster landings are: what belongs in the scaffold's
    own test file is the proof that the package no longer holds a stub under this
    name, so a revert cannot quietly restore one.

    The queue name is checked here too, because the spec is where the check
    lives and a stub restored *behind* a still-validating spec would be the
    confusing shape — the construction would fail the same way while nothing
    dispatched.
    """
    from application_sdk.testing.harness import starters

    with pytest.raises(starters.UnusableTaskQueueError):
        starters.QueueWorkflowSpec(
            workflow_type="w", task_queue="atlan-{app_name}-{deployment_name}"
        )

    spec = starters.QueueWorkflowSpec.for_deployment(
        workflow_type="w", app_name="hello-world", deployment_name="default"
    )
    assert spec.task_queue == "atlan-hello-world-default"

    # Reaches a real dispatch rather than a `HarnessNotBuiltError`: the
    # connection's client is deliberately not one, so the failure proves the stub
    # is gone without standing up a frontend. `HarnessNotBuiltError` is a
    # `NotImplementedError`, which `AttributeError` is not.
    from application_sdk.testing.harness.temporal import TemporalConnection

    with pytest.raises(AttributeError):
        await starters.start_on_task_queue(
            spec,
            connection=TemporalConnection(
                client=object(),  # type: ignore[arg-type]
                namespace="default",
                address="127.0.0.1:7233",
            ),
        )


def test_the_temporal_readers_need_no_extra() -> None:
    """FND-247's amendment expected ``temporalio`` behind the ``[workflows]``
    extra, with the readers import-guarded. It is a *core* dependency since v3.1
    and ``[workflows]`` is an alias resolving to it — so unlike ``cluster``'s
    ``harness`` extra there is nothing to guard, and this pins the correction
    rather than leaving it in a PR description."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert any(dep.startswith("temporalio") for dep in project["dependencies"])
    assert any(
        dep.startswith("temporalio")
        for dep in project["optional-dependencies"]["workflows"]
    )


async def test_every_remaining_stub_names_its_child_issue() -> None:
    """One stub left, and it still names the child that fills it in.

    Child G (FND-243) landed ``evidence.redact``, ``teardown.purge_connection``
    and ``starters.start_via_automation_engine``, so those three moved out of
    this list and into their own tests. ``start_via_app_handler`` moves with the
    cluster reader in child E, and until then this is what keeps its
    ``HarnessNotBuiltError`` carrying an issue rather than a bare
    ``NotImplementedError`` an auditor would have to grep prose for.
    """
    from application_sdk.testing.harness import starters
    from application_sdk.testing.harness.cluster import HttpRequest, HttpResponse

    class _Reader:
        """Shaped like a ClusterReader so the call is typed, not bypassed."""

        async def deployments(self, namespace: str, selector: str):
            return ()

        async def pods(self, namespace: str, selector: str):
            return ()

        def logs(self, namespace: str, selector: str, *, since=None):
            raise AssertionError("the stub must raise before reading logs")

        async def http(self, target, request: HttpRequest) -> HttpResponse:
            raise AssertionError("the stub must raise before calling out")

    for coro in (
        starters.start_via_app_handler(
            starters.HttpWorkflowSpec(
                target=ServiceTarget(namespace="ns", service="svc", port=8000),
                workflow_name="metadata_extraction",
            ),
            reader=_Reader(),
        ),
    ):
        with pytest.raises(HarnessNotBuiltError) as caught:
            await coro
        assert caught.value.issue == "FND-224"
        assert caught.value.operation


async def test_child_g_left_no_stub_behind() -> None:
    """The three child-G surfaces reach real work, not a ``HarnessNotBuiltError``.

    Same shape as the queue starter's own check: each is driven with an argument
    that cannot possibly work, so the failure proves the stub is gone without
    standing up a tenant. ``HarnessNotBuiltError`` is a ``NotImplementedError``,
    which none of these are — so a regression that reinstated a stub fails here
    rather than passing as "it raised something".
    """
    from application_sdk.testing.harness import starters
    from application_sdk.testing.harness.evidence import EvidenceBundle, redact
    from application_sdk.testing.harness.teardown import PurgeReport, purge_connection

    assert redact(EvidenceBundle(label="x")) == EvidenceBundle(label="x")

    # `purge_connection` reports rather than raises, on every path — so "the stub
    # is gone" is a *returned report*, not an exception. A reinstated stub would
    # raise `HarnessNotBuiltError` out of this call and fail here.
    report = await purge_connection(object(), "default/x/1")  # type: ignore[arg-type]
    assert isinstance(report, PurgeReport)
    assert report.errors and not report.complete

    with pytest.raises(AttributeError):
        await starters.start_via_automation_engine(
            starters.AEWorkflowSpec(name="x"),
            client=object(),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Wiring claims the issue makes
# ---------------------------------------------------------------------------


def test_harness_is_not_omitted_from_unit_coverage() -> None:
    """FND-238: harness/** is new code with a tenant-free unit surface, so it
    counts toward the 85% gate. testing/e2e/** keeps its exemption."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        omit = tomllib.load(handle)["tool"]["coverage"]["run"]["omit"]

    assert "application_sdk/testing/e2e/**" in omit
    assert not [entry for entry in omit if "testing/harness" in entry]


def test_the_harness_extra_is_separate_from_tests() -> None:
    """A connector installing test extras must not pull a Kubernetes client."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        extras = tomllib.load(handle)["project"]["optional-dependencies"]

    assert any(dep.startswith("kubernetes") for dep in extras["harness"])
    assert not any("kubernetes" in dep for dep in extras["tests"])


def test_the_package_declares_all_so_the_manifest_extractor_finds_it() -> None:
    """The capability-manifest extractor discovers every public module that
    declares __all__ (it walks the tree since FND-439), so no extractor change
    is needed for a nested testing.* subpackage — only a regeneration."""
    import application_sdk.testing.harness as harness

    assert "run_sync" in harness.__all__
    assert all(hasattr(harness, name) for name in harness.__all__)


def test_importing_the_harness_does_not_require_pytest() -> None:
    """``fixtures`` is child I's pytest layer, and the package must not import it.

    Static, not a runtime probe, because a runtime one cannot see the difference
    once this test module has already imported ``fixtures`` itself. The property
    is worth pinning rather than trusting: pytest is not a runtime dependency of
    this SDK, so a convenience re-export from the package ``__init__`` would make
    ``import application_sdk.testing.harness`` fail in a production process — and
    the failure would surface as an ``ImportError`` naming a module nobody asked
    for.
    """
    import ast
    from pathlib import Path

    import application_sdk.testing.harness as harness_pkg

    source = Path(str(harness_pkg.__file__)).read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "application_sdk.testing.harness.fixtures" not in imported
    # The one entry point a composer registers, named in the module map so the
    # import path is discoverable without reading this test.
    assert "harness.fixtures" in (harness_pkg.__doc__ or "")
