"""ContextVar-scoped tracker access + the create_tracker factory wiring."""

from application_sdk.observability.lineage import (
    LineageObservabilityTracker,
    NoOpLineageObservabilityTracker,
    ObservabilityConfig,
    create_tracker,
    get_lineage_tracker,
    reset_lineage_tracker,
    set_lineage_tracker,
)


def test_get_returns_noop_when_unset():
    reset_lineage_tracker()
    tracker = get_lineage_tracker()
    assert isinstance(tracker, NoOpLineageObservabilityTracker)
    # never None — call sites can use it unconditionally
    tracker.record_missing_reason("Ds", "1", reason="X")


def test_set_then_get_returns_same_instance():
    t = LineageObservabilityTracker(connector_type="t")
    set_lineage_tracker(t)
    assert get_lineage_tracker() is t
    reset_lineage_tracker()
    assert isinstance(get_lineage_tracker(), NoOpLineageObservabilityTracker)


def test_create_tracker_registers_in_context():
    reset_lineage_tracker()
    t = create_tracker("tableau", ObservabilityConfig(enabled=True))
    assert get_lineage_tracker() is t
    assert isinstance(t, LineageObservabilityTracker)
    reset_lineage_tracker()


def test_create_tracker_disabled_registers_noop():
    reset_lineage_tracker()
    t = create_tracker("tableau", ObservabilityConfig(enabled=False))
    assert isinstance(t, NoOpLineageObservabilityTracker)
    assert get_lineage_tracker() is t
    reset_lineage_tracker()


def test_create_tracker_attaches_run_context_and_registry():
    from application_sdk.observability.lineage import (
        ReasonCategory,
        ReasonCode,
        ReasonCodeRegistry,
        RunContext,
    )

    rc = RunContext(connector_type="tableau", workflow_id="wf-1", tenant="acme")
    reg = ReasonCodeRegistry({"X": ReasonCode("X", ReasonCategory.CACHE_MISS, "x")})
    t = create_tracker(
        "tableau", ObservabilityConfig(enabled=True), run_context=rc, registry=reg
    )
    assert t.run_context is rc
    assert t.registry is reg
    reset_lineage_tracker()
