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


async def test_every_remaining_stub_names_its_child_issue() -> None:
    from application_sdk.testing.harness import atlas, automation_engine, starters
    from application_sdk.testing.harness.cluster import HttpRequest, HttpResponse
    from application_sdk.testing.harness.evidence import EvidenceBundle, redact
    from application_sdk.testing.harness.teardown import purge_connection

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

    for call in (lambda: redact(EvidenceBundle(label="x")),):
        with pytest.raises(HarnessNotBuiltError) as caught:
            call()
        assert caught.value.issue == "FND-224"
        assert caught.value.operation

    for coro in (
        atlas.count_assets("default/x/1", ["Table"]),
        atlas.sample_qualified_names("default/x/1", ["Table"], per_type=3),
        automation_engine.submit_run({}, budget=_budget()),
        purge_connection("default/x/1"),
        starters.start_via_automation_engine({}),
        starters.start_on_task_queue(
            starters.QueueWorkflowSpec(workflow_type="w", task_queue="q")
        ),
        starters.start_via_app_handler(
            starters.HttpWorkflowSpec(
                target=ServiceTarget(namespace="ns", service="svc", port=8000),
                workflow_name="metadata_extraction",
            ),
            reader=_Reader(),
        ),
        automation_engine.poll_native_status(
            automation_engine.AERunHandle(workflow_slug="w", run_id="1"),
            budget=_budget(),
        ),
    ):
        with pytest.raises(HarnessNotBuiltError) as caught:
            await coro
        assert caught.value.issue == "FND-224"
        assert caught.value.operation


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
