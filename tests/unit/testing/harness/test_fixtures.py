"""Unit tests for the harness's async fixtures (child I on FND-224).

Two kinds of test, for two kinds of claim.

The fixture contracts are checked by **running pytest inside pytest**
(``pytester``), because that is the only way to check what a fixture actually
does: that requesting it is what runs the gate, that an unconfigured override
point errors rather than guessing, that teardown fires on a failing test, and —
the issue's own acceptance criterion — that a suite composing these fixtures
*collects zero tests it did not write*. Asserting that last one by reading the
module's namespace would only prove this module is tidy today; asserting it
through a real collection proves the property the runtime suite needs.

The plain helpers — the evidence accumulator, the node-id slug, the quiet purge —
are tested directly.

One seam is stubbed rather than driven: ``evidence.write_bundle`` and
``evidence.secrets_from_environment`` land in child G (FND-243) and do not exist
on this tree yet, so the tests that cover the write path assert the *call* this
module makes against the signature child G published. That is a claim, not a
differential, and it agrees with a wrong signature by construction — the reason
it is written this way is that the alternative is no coverage of the write path
at all. Once child G lands, these become assertions against a real writer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.harness import evidence as evidence_api
from application_sdk.testing.harness import fixtures as fixtures_api
from application_sdk.testing.harness import teardown as teardown_api
from application_sdk.testing.harness.evidence import EvidenceBundle
from application_sdk.testing.harness.expectations import Finding
from application_sdk.testing.harness.fixtures import (
    DEFAULT_EVIDENCE_DIR,
    EvidenceLog,
    _failed_in_any_phase,
    _phase_failed,
    _purge_quietly,
    _slug,
    _write_if_failed,
)
from application_sdk.testing.harness.teardown import PurgeReport

pytest_plugins = ["pytester"]

# These tests run pytest inside pytest (``pytest.Pytester``). Under xdist, the
# unit lane's ``--disable-socket`` leaves the inner run unable to emit any output
# at all, so ``assert_outcomes`` reads an empty summary and all of them fail. It
# is the three-way combination that breaks: the file is green under xdist without
# the flag, and green with the flag when run serially. The generated suites are
# hermetic — they never open a real connection. See FND-961.
pytestmark = pytest.mark.enable_socket

#: Written into every generated project: the fixtures reach pytest's hooks only
#: when the module is registered as a plugin, and ``asyncio_mode`` is what lets
#: an ``async def`` fixture work with no decorator.
CONFTEST = 'pytest_plugins = ["application_sdk.testing.harness.fixtures"]\n'
INI = "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"


class RecordingLogger:
    """Captures this module's own log calls, rendered.

    ``caplog`` cannot see them: the SDK's logger is loguru-backed and does not
    propagate to stdlib logging. Substituting the module's logger object is what
    keeps the message assertable — which matters for the unregistered-plugin
    path, where the warning *is* the behaviour rather than a note about it.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, message: str, *args: object, **kwargs: object) -> None:
        self.messages.append(message % args if args else message)

    warning = _record
    info = _record
    error = _record
    debug = _record

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


@pytest.fixture
def recorded_logs(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    """Substitute the fixtures module's logger, and hand back what it recorded."""
    recorder = RecordingLogger()
    monkeypatch.setattr(fixtures_api, "logger", recorder)
    return recorder


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    """A generated pytest project with the harness fixtures registered."""
    pytester.makeini(INI)
    pytester.makeconftest(CONFTEST)
    return pytester


# ---------------------------------------------------------------------------
# The acceptance criterion: a composing suite collects only its own tests
# ---------------------------------------------------------------------------


def test_a_composing_suite_collects_only_the_tests_it_wrote(
    suite: pytest.Pytester,
) -> None:
    """The whole reason fixtures exist alongside the base class. Inheriting
    ``BaseE2ETest`` to reach setup and teardown also collects its concrete
    ``test_full_dag_runs_end_to_end``; composing fixtures collects nothing."""
    suite.makepyfile(
        test_composed="""
        def test_only_mine(harness_run_id, harness_budget_profile):
            assert isinstance(harness_run_id, int)
        """
    )
    collected = suite.runpytest("--collect-only", "-q")
    ids = [line for line in collected.outlines if "::" in line]
    assert ids == ["test_composed.py::test_only_mine"]
    collected.assert_outcomes()


def test_the_module_contributes_no_collectible_names(suite: pytest.Pytester) -> None:
    """Registering the plugin must not add a test to the composer's suite, so a
    ``test_``-prefixed helper can never be introduced here by accident."""
    suite.makepyfile(test_empty="")
    result = suite.runpytest("--collect-only", "-q")
    assert not [line for line in result.outlines if "::" in line]


# ---------------------------------------------------------------------------
# Override points
# ---------------------------------------------------------------------------


def test_an_unconfigured_connection_type_errors_and_names_the_fixture(
    suite: pytest.Pytester,
) -> None:
    """No defensible default: this string is the prefix teardown purges under."""
    suite.makepyfile(
        test_identity="""
        def test_needs_a_connection_type(harness_connection_identity):
            raise AssertionError("the body must never run")
        """
    )
    result = suite.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*harness_connection_type*"])


def test_an_unconfigured_app_under_test_errors(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        test_app="""
        def test_needs_an_app(harness_app_under_test):
            raise AssertionError("the body must never run")
        """
    )
    result = suite.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*harness_app_under_test*"])


def test_a_suite_that_declares_nothing_still_runs(suite: pytest.Pytester) -> None:
    """The raising defaults fire only when the dependent fixture is requested:
    declaring a connection type is not a tax on a suite with no connection."""
    suite.makepyfile(
        test_plain="""
        def test_no_declarations_needed(harness_environ, harness_substrate):
            assert harness_substrate == "local"
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


def test_an_overridden_minter_makes_the_purged_name_assertable(
    suite: pytest.Pytester,
) -> None:
    """The identity module puts the clock behind a seam so a test can predict the
    qualified name teardown will purge. This is that promise, through fixtures."""
    suite.makepyfile(
        test_minted="""
        import pytest
        from application_sdk.testing.harness.identity import Minter

        @pytest.fixture
        def harness_connection_type():
            return "postgres"

        @pytest.fixture
        def harness_minter():
            return Minter(clock=lambda: 1700000000, randbelow=lambda _: 42)

        def test_exact_qualified_name(harness_connection_identity):
            assert harness_connection_identity.qualified_name == (
                "default/postgres/1700000000000042"
            )
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


def test_the_local_substrate_refuses_a_cluster_read(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        test_cluster="""
        def test_no_cluster_here(harness_cluster_reader):
            raise AssertionError("the body must never run")
        """
    )
    result = suite.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*has no Kubernetes API*"])


def test_a_declared_kubeconfig_substrate_builds_a_reader(
    suite: pytest.Pytester,
) -> None:
    """Selection follows the declaration, and building the reader connects
    nothing — an unusable kubeconfig surfaces at the read, not at setup."""
    suite.makepyfile(
        test_cluster_ok="""
        import pytest
        from application_sdk.testing.harness.cluster import ClusterReader
        from application_sdk.testing.harness.substrate import Substrate

        @pytest.fixture
        def harness_substrate():
            return Substrate.KUBECONFIG

        @pytest.fixture
        def harness_kube_context():
            return "e2e-gcp"

        def test_reader_is_built(harness_cluster_reader):
            assert isinstance(harness_cluster_reader, ClusterReader)
            assert harness_cluster_reader.kube_context == "e2e-gcp"
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


def test_a_missing_tenant_raises_rather_than_skipping(suite: pytest.Pytester) -> None:
    """A tenant-facing suite that runs green against no tenant is the failure
    mode this harness exists to remove."""
    suite.makepyfile(
        test_tenant="""
        import pytest

        @pytest.fixture
        def harness_environ():
            return {}

        def test_needs_a_tenant(harness_tenant_auth):
            raise AssertionError("the body must never run")
        """
    )
    result = suite.runpytest()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*ATLAN_BASE_URL*"])


def test_tenant_auth_is_read_from_the_environ_snapshot(
    suite: pytest.Pytester,
) -> None:
    suite.makepyfile(
        test_tenant_ok="""
        import pytest

        @pytest.fixture
        def harness_environ():
            return {
                "ATLAN_BASE_URL": "https://tenant.example.com/",
                "ATLAN_API_KEY": "  token  ",
            }

        def test_auth(harness_tenant_auth):
            assert harness_tenant_auth.base_url == "https://tenant.example.com"
            assert harness_tenant_auth.api_key == "token"
            assert harness_tenant_auth.oauth_client_id is None
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


# ---------------------------------------------------------------------------
# The precondition gate, as a fixture
# ---------------------------------------------------------------------------


def test_requesting_the_gate_is_what_runs_it(suite: pytest.Pytester) -> None:
    """So a scenario cannot forget to assert its starting state."""
    suite.makepyfile(
        test_gate="""
        import pytest
        from datetime import timedelta

        from application_sdk.testing.harness import Settled
        from application_sdk.testing.harness.preconditions import PreconditionCheck

        ran = []

        @pytest.fixture
        def harness_precondition_checks():
            async def _run():
                ran.append("checked")
                return Settled(
                    label="mine", attempts=1, elapsed=timedelta(0), value=True
                )
            return (PreconditionCheck(label="mine", run=_run),)

        def test_gate_ran_before_me(harness_preconditions):
            assert ran == ["checked"]
            assert harness_preconditions.verdict == "passed"
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


def test_an_unmet_precondition_errors_before_the_body_runs(
    suite: pytest.Pytester,
) -> None:
    """An error, not a failure: the environment was never fit to test, so the red
    must not read as a regression in the thing under test."""
    suite.makepyfile(
        test_gate_fails="""
        import pytest
        from datetime import timedelta

        from application_sdk.testing.harness import Expired
        from application_sdk.testing.harness.preconditions import PreconditionCheck

        @pytest.fixture
        def harness_precondition_checks():
            async def _run():
                return Expired(
                    label="worker health",
                    attempts=3,
                    elapsed=timedelta(seconds=9),
                    budget=timedelta(seconds=9),
                )
            return (PreconditionCheck(label="worker health", run=_run),)

        def test_never_runs(harness_preconditions):
            raise AssertionError("the body must never run")
        """
    )
    result = suite.runpytest()
    result.assert_outcomes(errors=1, failed=0)
    result.stdout.fnmatch_lines(["*PreconditionsFailedError*"])


def test_the_default_gate_polls_the_ambient_health_url(
    suite: pytest.Pytester,
) -> None:
    """The connector side gets its precondition from the variable the shared CI
    action already exports, without declaring anything."""
    suite.makepyfile(
        test_default_gate="""
        import pytest

        @pytest.fixture
        def harness_environ():
            return {"E2E_WORKER_HEALTH_URL": "http://127.0.0.1:1/server/health"}

        def test_checks_are_declared(harness_precondition_checks):
            assert len(harness_precondition_checks) == 1
            assert "127.0.0.1:1" in harness_precondition_checks[0].label

        def test_no_url_means_no_checks(harness_precondition_checks, harness_environ):
            pass
        """
    )
    result = suite.runpytest("-k", "checks_are_declared")
    result.assert_outcomes(passed=1)


def test_no_health_url_declares_no_checks(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        test_no_gate="""
        import pytest

        @pytest.fixture
        def harness_environ():
            return {"E2E_WORKER_HEALTH_URL": "   "}

        def test_empty(harness_precondition_checks, harness_preconditions):
            assert harness_precondition_checks == ()
            assert harness_preconditions.verdict == "passed"
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_the_connection_is_purged_even_when_the_test_fails(
    suite: pytest.Pytester,
) -> None:
    """Assets a failed run leaves behind are what green a later run that should
    have failed."""
    suite.makepyfile(
        test_purge="""
        import pytest

        from application_sdk.testing.harness import teardown as teardown_api
        from application_sdk.testing.harness.teardown import PurgeReport

        purged = []

        @pytest.fixture
        def harness_connection_type():
            return "postgres"

        @pytest.fixture
        async def harness_atlas_client():
            yield object()

        @pytest.fixture(autouse=True)
        def _capture_purges(monkeypatch):
            async def _fake(client, qualified_name):
                purged.append(qualified_name)
                return PurgeReport(purged=3)
            monkeypatch.setattr(teardown_api, "purge_connection", _fake)

        async def test_fails_on_purpose(harness_connection_teardown):
            assert harness_connection_teardown.qualified_name.startswith(
                "default/postgres/"
            )
            raise AssertionError("deliberate")

        def test_the_purge_happened():
            assert len(purged) == 1
        """
    )
    result = suite.runpytest()
    result.assert_outcomes(passed=1, failed=1)


async def test_a_purge_failure_is_warned_not_raised(
    recorded_logs: RecordingLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown runs after the assertions decided the verdict: raising here
    replaces a real failure with a cleanup error and loses the diagnosis."""

    async def _boom(client: object, qualified_name: str) -> PurgeReport:
        raise RuntimeError("tenant said no")

    monkeypatch.setattr(teardown_api, "purge_connection", _boom)
    await _purge_quietly(object(), "default/postgres/1")  # type: ignore[arg-type]
    assert "manual purge may be needed" in recorded_logs.text


async def test_an_incomplete_purge_names_what_it_left_behind(
    recorded_logs: RecordingLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator cleaning up by hand needs the qualified names; a count tells
    them only that they have a problem."""

    async def _partial(client: object, qualified_name: str) -> PurgeReport:
        return PurgeReport(
            purged=2, orphaned=("default/postgres/1/db",), errors=("batch 2 failed",)
        )

    monkeypatch.setattr(teardown_api, "purge_connection", _partial)
    await _purge_quietly(object(), "default/postgres/1")  # type: ignore[arg-type]
    assert "1 orphaned" in recorded_logs.text
    assert "batch 2 failed" in recorded_logs.text


async def test_a_clean_purge_says_nothing(
    recorded_logs: RecordingLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _clean(client: object, qualified_name: str) -> PurgeReport:
        return PurgeReport(purged=7)

    monkeypatch.setattr(teardown_api, "purge_connection", _clean)
    await _purge_quietly(object(), "default/postgres/1")  # type: ignore[arg-type]
    assert recorded_logs.text == ""


# ---------------------------------------------------------------------------
# The sync bridge's loop
# ---------------------------------------------------------------------------


def test_the_bridge_fixture_closes_the_loop_per_test(suite: pytest.Pytester) -> None:
    """The bridge keeps one loop per thread for the life of the thread; this is
    the teardown its own documentation says belongs to the caller."""
    suite.makepyfile(
        test_bridge_fixture="""
        import asyncio

        from application_sdk.testing.harness import run_sync

        loops = []

        async def _current_loop():
            return asyncio.get_running_loop()

        def test_first(harness_sync_bridge):
            loops.append(run_sync(_current_loop()))

        def test_second(harness_sync_bridge):
            loops.append(run_sync(_current_loop()))

        def test_each_test_had_its_own_closed_loop():
            assert loops[0] is not loops[1]
            assert loops[0].is_closed()
            assert loops[1].is_closed()
        """
    )
    suite.runpytest().assert_outcomes(passed=3)


# ---------------------------------------------------------------------------
# The evidence accumulator
# ---------------------------------------------------------------------------


def _finding(label: str = "table count") -> Finding:
    return Finding(
        subject=label, detail="expected at least 1, saw 0", expectation="floor"
    )


def test_an_empty_log_is_empty() -> None:
    assert EvidenceLog("run").is_empty


def test_the_log_accumulates_rather_than_truncating() -> None:
    log = EvidenceLog("run")
    log.add_finding(_finding("first"))
    log.add_finding(_finding("second"))
    bundle = log.bundle()
    assert [finding.subject for finding in bundle.findings] == ["first", "second"]


def test_the_log_carries_logs_readings_and_artifacts() -> None:
    log = EvidenceLog("run")
    log.add_logs("worker-0", ["line one", "line two"])
    log.record("table_count", 12)
    log.add_artifact("manifest.json", "{}")
    bundle = log.bundle()
    assert bundle.logs == {"worker-0": ("line one", "line two")}
    assert bundle.readings == {"table_count": 12}
    assert bundle.artifacts == {"manifest.json": "{}"}
    assert bundle.label == "run"


def test_a_frozen_bundle_does_not_change_underneath_its_holder() -> None:
    """A caller that logs locally and uploads remotely holds two bundles; a
    builder that handed out live views would make the first one lie."""
    log = EvidenceLog("run")
    log.record("first", 1)
    bundle = log.bundle()
    log.record("second", 2)
    assert dict(bundle.readings) == {"first": 1}


def test_merge_folds_in_evidence_the_scenario_collected_itself() -> None:
    """The seam for pod logs and anything else a composer collects, so the
    accumulator does not have to know how to collect anything."""
    log = EvidenceLog("run")
    log.record("mine", 1)
    log.merge(
        EvidenceBundle(
            label="ignored",
            findings=(_finding("theirs"),),
            logs={"pod-a": ("log",)},
            readings={"theirs": 2},
            artifacts={"describe.txt": "..."},
        )
    )
    bundle = log.bundle()
    assert bundle.label == "run"
    assert dict(bundle.readings) == {"mine": 1, "theirs": 2}
    assert [finding.subject for finding in bundle.findings] == ["theirs"]
    assert "pod-a" in bundle.logs


# ---------------------------------------------------------------------------
# Writing evidence on failure
# ---------------------------------------------------------------------------


class FakeReport:
    """Stands in for a ``pytest.TestReport`` the hook stashed."""

    def __init__(self, *, failed: bool) -> None:
        self.failed = failed


class FakeNode:
    """A test item carrying whichever phase reports a scenario needs."""

    NODEID = "tests/e2e/test_x.py::TestSuite::test_something[gcp]"

    def __init__(self, **phases: bool) -> None:
        self.name = "test_something"
        self.nodeid = self.NODEID
        for phase, failed in phases.items():
            setattr(self, f"_harness_phase_report_{phase}", FakeReport(failed=failed))


def _log_with_content() -> EvidenceLog:
    log = EvidenceLog("test_something")
    log.record("table_count", 0)
    return log


def _install_fake_writer(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    """Stub child G's persist surface. See this module's docstring on why."""

    def _write_bundle(
        bundle: EvidenceBundle, output_dir: Path, *, secrets: Sequence[str] = ()
    ) -> Sequence[Path]:
        calls.append({"bundle": bundle, "output_dir": output_dir, "secrets": secrets})
        return (output_dir / "report.md",)

    def _secrets_from_environment(
        environ: Mapping[str, str], *, also: Sequence[str] = ()
    ) -> tuple[str, ...]:
        return tuple(environ[name] for name in also if name in environ)

    monkeypatch.setattr(evidence_api, "write_bundle", _write_bundle, raising=False)
    monkeypatch.setattr(
        evidence_api,
        "secrets_from_environment",
        _secrets_from_environment,
        raising=False,
    )


def test_evidence_is_written_when_the_call_phase_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=True)),  # type: ignore[arg-type]
        environ={"ATLAN_BASE_URL": "https://tenant.example.com"},
        evidence_dir=tmp_path,
    )

    assert len(calls) == 1
    assert calls[0]["output_dir"].parent == tmp_path
    # One path segment, whatever the node id contained.
    assert calls[0]["output_dir"].name.count("_") >= 1
    assert "/" not in calls[0]["output_dir"].name


def test_the_tenant_url_is_passed_as_a_value_to_redact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Key-name matching cannot see a hostname a driver echoed back with no key
    beside it, and a tenant hostname identifies a customer environment."""
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=True)),  # type: ignore[arg-type]
        environ={"ATLAN_BASE_URL": "https://tenant.example.com"},
        evidence_dir=tmp_path,
    )

    assert calls[0]["secrets"] == ("https://tenant.example.com",)


def test_a_setup_failure_also_produces_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A precondition that could not be read is exactly when evidence matters."""
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=False, setup=True)),  # type: ignore[arg-type]
        environ={},
        evidence_dir=tmp_path,
    )

    assert len(calls) == 1


def test_a_passing_test_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=False, setup=False)),  # type: ignore[arg-type]
        environ={},
        evidence_dir=tmp_path,
    )

    assert calls == []


def test_an_empty_log_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        EvidenceLog("test_something"),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=True)),  # type: ignore[arg-type]
        environ={},
        evidence_dir=tmp_path,
    )

    assert calls == []


def test_opting_out_of_the_directory_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=True)),  # type: ignore[arg-type]
        environ={},
        evidence_dir=None,
    )

    assert calls == []


def test_an_unregistered_plugin_warns_and_names_the_fix(
    recorded_logs: RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the hook a fixture cannot know the verdict. Writing on every test
    would fill the artifact with passing runs, so it writes nothing — but the
    reason is a one-line config fix, and the warning has to say so."""
    calls: list[dict[str, Any]] = []
    _install_fake_writer(monkeypatch, calls)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode()),  # type: ignore[arg-type]
        environ={},
        evidence_dir=tmp_path,
    )

    assert calls == []
    assert "pytest_plugins" in recorded_logs.text


def test_a_writer_failure_never_masks_the_test_result(
    recorded_logs: RecordingLogger, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Including the window before child G lands the writer at all, where the
    attribute does not exist."""
    monkeypatch.delattr(evidence_api, "write_bundle", raising=False)

    _write_if_failed(
        _log_with_content(),
        name="test_something",
        nodeid=FakeNode.NODEID,
        failed=_failed_in_any_phase(FakeNode(call=True)),  # type: ignore[arg-type]
        environ={},
        evidence_dir=tmp_path,
    )

    assert "only the evidence is missing" in recorded_logs.text


def test_the_default_evidence_dir_is_under_the_uploaded_results_root() -> None:
    """``results/`` is what the shared CI action uploads, so a red leg keeps its
    bundle with no workflow change."""
    assert DEFAULT_EVIDENCE_DIR.parts[0] == "results"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/e2e/test_x.py::test_y",
        "tests/e2e/test_x.py::TestSuite::test_y[gcp-3]",
        "a/b/c.py::t",
    ],
)
def test_a_slug_is_always_one_path_segment(nodeid: str) -> None:
    """A parametrised node id carries ``/`` and ``::``; a bundle path must not
    fan out into directories named after them."""
    slug = _slug(nodeid)
    assert "/" not in slug
    assert ":" not in slug
    assert slug


def test_a_slug_never_ends_up_empty() -> None:
    assert _slug("///") == "test"


def test_a_missing_phase_report_reads_as_unknown_not_as_passing() -> None:
    assert _phase_failed(FakeNode(), "call") is None  # type: ignore[arg-type]
    assert _phase_failed(FakeNode(call=True), "call") is True  # type: ignore[arg-type]
    assert _phase_failed(FakeNode(call=False), "call") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The remaining fixtures, exercised as fixtures
# ---------------------------------------------------------------------------


def test_the_evidence_log_is_composable_and_silent_on_a_pass(
    suite: pytest.Pytester,
) -> None:
    """A passing test must leave no bundle behind: the artifact is for red legs,
    and one file per green test is how an artifact stops being read."""
    suite.makepyfile(
        test_evidence="""
        from pathlib import Path

        def test_records_into_the_log(harness_evidence, harness_evidence_dir):
            harness_evidence.record("table_count", 12)
            harness_evidence.add_logs("worker-0", ["started"])
            assert harness_evidence_dir == Path("results/harness-evidence")

        def test_nothing_was_written():
            assert not Path("results").exists()
        """
    )
    suite.runpytest().assert_outcomes(passed=2)


def test_the_tenant_clients_are_built_and_closed_around_the_test(
    suite: pytest.Pytester,
) -> None:
    """Both wirings are one client per *test* — not per call, which is the
    fresh-loop-and-handshake cost D1 found, and not per session, which would bind
    a pooled connection to a loop later tests do not run on."""
    suite.makepyfile(
        test_clients="""
        import pytest

        from application_sdk.testing.harness.automation_engine import AEClient

        @pytest.fixture
        def harness_environ():
            return {
                "ATLAN_BASE_URL": "https://tenant.example.com",
                "ATLAN_API_KEY": "not-a-real-key",
                "GITHUB_RUN_ID": "4242",
            }

        async def test_clients_are_wired(
            harness_atlas_client, harness_ae_client, harness_run_id
        ):
            assert isinstance(harness_ae_client, AEClient)
            assert harness_ae_client.tenant_url == "https://tenant.example.com"
            assert harness_atlas_client is not None
            assert harness_run_id == 4242
        """
    )
    suite.runpytest().assert_outcomes(passed=1)


def test_every_fixture_is_exported() -> None:
    """``__all__`` is what the capability manifest renders, so a fixture missing
    from it is one a composer cannot discover without reading the source.

    An invariant rather than a one-off fix, because pytest discovers a fixture
    whether or not it is exported — so nothing else in the tree notices. The two
    that were missing were ``harness_connection_type`` and
    ``harness_app_under_test``: the only two a composer *must* declare, and so
    exactly the two worst to leave out of the manifest.
    """
    exported = set(fixtures_api.__all__)
    defined = {name for name in dir(fixtures_api) if name.startswith("harness_")}
    assert defined - exported == set()
    assert all(hasattr(fixtures_api, name) for name in exported)
