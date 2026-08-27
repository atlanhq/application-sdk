"""The harness lifecycle as async pytest fixtures, for suites that inherit nothing.

The second of the harness's three entry shapes (FND-244). All three drive the
same functions:

1. **xunit hooks** — ``setup_method`` / ``teardown_method`` on ``BaseE2ETest``,
   one-line ``run_sync`` shims. A connector subclass that overrides
   ``setup_method`` keeps working, because its public surface does not move.
2. **async fixtures** — this module. ``asyncio_mode = "auto"`` is set for this
   repo, so an ``async def`` fixture here needs no decorator beyond
   ``@pytest.fixture``, and a composing suite's ``async def test_...`` is awaited
   normally.
3. :func:`~application_sdk.testing.harness.bridge.run_sync` — a synchronous
   composer with no loop of its own.

**Why fixtures dissolve the coupling.** ``BaseE2ETest`` carries a concrete test
method, ``test_full_dag_runs_end_to_end``. Anything that inherits it to reach
setup and teardown also *collects that test* — which is why the runtime scenario
suite could not reuse the plumbing without also running a connector's full-DAG
assertion. A suite that composes fixtures inherits nothing, so it collects
exactly the tests it wrote. Nothing in this module defines a ``test_``-prefixed
name or a ``Test``-prefixed class, and
``tests/unit/testing/harness/test_fixtures.py`` asserts that as a property
rather than trusting it.

**Three variances stay declared, not assumed.** FND-224 was explicit that tenant
wiring and app wiring are not shared, and building this found a third that
neither source design document names: the execution substrate (see
:mod:`application_sdk.testing.harness.substrate`). Each is an override-point
fixture. Where no default is defensible the default *raises*
:class:`~application_sdk.testing.harness._errors.FixtureNotConfiguredError`
naming the fixture to override — and only when a suite actually requests it, so
declaring a connection type is not a tax on a suite that never creates a
connection.

**Every fixture is function-scoped, deliberately.** A session-scoped async
fixture binds its clients to one event loop while ``pytest-asyncio`` gives each
test its own, which is the loop-mismatch class
:class:`~application_sdk.testing.harness.temporal.TemporalReaderLoopMismatchError`
exists to name; and a session-scoped connection identity would let one test's
teardown purge the next test's assets. A composer that genuinely wants a
session-scoped client overrides the fixture and owns the loop question.

**Registration.** Fixtures work if you import them into a ``conftest.py``.
Evidence-on-failure needs the module loaded as a *plugin*, because the only
supported way to learn a test's verdict from a fixture is the
``pytest_runtest_makereport`` hook below. Put this in the **root** ``conftest.py``
(pytest 8 refuses ``pytest_plugins`` in a non-root one)::

    pytest_plugins = ["application_sdk.testing.harness.fixtures"]

Then compose only what a suite needs::

    async def test_queue_has_only_current_pollers(
        harness_preconditions, harness_cluster_reader
    ) -> None:
        assert harness_preconditions.verdict is Verdict.PASSED

This module imports ``pytest``, which is why
:mod:`application_sdk.testing.harness` does not import it: the harness package
is importable in a process that has no test framework installed, and pytest is
not one of this SDK's runtime dependencies.
"""

from __future__ import annotations

import os
from collections.abc import (
    AsyncIterator,
    Generator,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

import pytest

from application_sdk.observability.logger_adaptor import get_logger

# ``teardown`` is aliased to ``teardown_api`` rather than the obvious
# ``teardown_module``: that exact name is pytest's xunit module-teardown hook, so
# any module holding it — this one, or a test module that imports from it — hands
# pytest a module object to call at teardown, which fails with a baffling
# ``AttributeError: ... has no attribute '__code__'``.
from application_sdk.testing.harness import evidence as evidence_api
from application_sdk.testing.harness import teardown as teardown_api
from application_sdk.testing.harness._errors import FixtureNotConfiguredError
from application_sdk.testing.harness.atlas import atlas_client
from application_sdk.testing.harness.automation_engine import AEClient
from application_sdk.testing.harness.bridge import close_loop
from application_sdk.testing.harness.budgets import CONNECTOR_CI, BudgetProfile, Wait
from application_sdk.testing.harness.cluster import ClusterReader
from application_sdk.testing.harness.evidence import EvidenceBundle
from application_sdk.testing.harness.expectations import Finding
from application_sdk.testing.harness.identity import (
    ConnectionIdentity,
    Minter,
    TenantAuth,
    read_tenant_auth,
)
from application_sdk.testing.harness.preconditions import (
    GateReport,
    PreconditionCheck,
    assert_gate,
    check_worker_health,
    run_preconditions,
)
from application_sdk.testing.harness.spec import AppUnderTest
from application_sdk.testing.harness.substrate import Substrate, cluster_reader_for
from application_sdk.testing.harness.teardown import PurgeReport

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan is a lazy import
    from pyatlan.client.aio import AsyncAtlanClient

logger = get_logger(__name__)

__all__ = [
    "EvidenceLog",
    "harness_ae_client",
    "harness_app_under_test",
    "harness_atlas_client",
    "harness_budget_profile",
    "harness_cluster_reader",
    "harness_connection_identity",
    "harness_connection_type",
    "harness_connection_teardown",
    "harness_environ",
    "harness_evidence",
    "harness_evidence_dir",
    "harness_kube_context",
    "harness_minter",
    "harness_precondition_checks",
    "harness_preconditions",
    "harness_run_id",
    "harness_substrate",
    "harness_sync_bridge",
    "harness_tenant_auth",
    "harness_worker_health_url",
]

#: Prefix of the attribute the report hook leaves on the test item. One per
#: phase, so a teardown-time reader can tell "the call failed" from "setup
#: failed" without pytest's internals.
_REPORT_ATTR = "_harness_phase_report_"

#: Where an evidence bundle lands unless a composer says otherwise. ``results/``
#: is already what the shared ``sdr-e2e`` CI action uploads as an artifact, and
#: pytest runs with the repo root as its working directory, so the default needs
#: no workflow change to survive a red leg.
DEFAULT_EVIDENCE_DIR = Path("results/harness-evidence")


# ---------------------------------------------------------------------------
# The parallel-child seam
# ---------------------------------------------------------------------------
#
# Child G (FND-243) owns the purge and the evidence writer; this child owns the
# fixtures that call them, and the two are being built at the same time. So these
# three functions are resolved **by name at call time** against the signatures
# child G published, rather than imported at module scope:
#
# * whichever of the two lands first, this module still imports and every fixture
#   that does not touch the other's surface still works;
# * a call site that is one line and one Protocol is a cheap thing to correct if
#   a signature moves, where a scattered set of direct calls is not.
#
# Each Protocol states the signature this module is coded against, so what has to
# be re-checked when child G lands is three declarations in one place. Delete this
# section then, and import the three names directly.
#
# The parameters are positional-*or*-keyword, matching child G's real signatures
# rather than narrowing them: these are new public surface, and a composer calling
# `purge_connection(client=..., connection_qualified_name=...)` is a legitimate
# thing to want. Which makes the parameter *names* below part of the contract, not
# just their order.


class _PurgeConnection(Protocol):
    """``teardown.purge_connection``, as this module calls it."""

    async def __call__(
        self, client: AsyncAtlanClient, connection_qualified_name: str
    ) -> PurgeReport: ...


class _BundleWriter(Protocol):
    """``evidence.write_bundle``, as this module calls it. Redacts internally."""

    def __call__(
        self, bundle: EvidenceBundle, output_dir: Path, *, secrets: Sequence[str] = ()
    ) -> Sequence[Path]: ...


class _SecretsReader(Protocol):
    """``evidence.secrets_from_environment``, as this module calls it."""

    def __call__(
        self, environ: Mapping[str, str], *, also: Sequence[str] = ()
    ) -> tuple[str, ...]: ...


_Resolved = TypeVar("_Resolved")


def _child_g(name: str, signature: type[_Resolved]) -> _Resolved:
    """Resolve one of child G's functions, or raise ``AttributeError``.

    Args:
        name: Attribute name on :mod:`application_sdk.testing.harness.evidence`.
        signature: The Protocol declaring how this module calls it.

    Returns:
        The function. Both call sites are inside a broad ``except`` that reports
        rather than raises, so a miss before child G lands costs a warning, not a
        test result.
    """
    return cast(_Resolved, getattr(evidence_api, name))


# ---------------------------------------------------------------------------
# The report hook
# ---------------------------------------------------------------------------


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Stash each phase's report on the item so a fixture can read the verdict.

    A fixture teardown cannot otherwise know whether its test passed — pytest
    exposes no supported API for it — and evidence collection that fires on every
    test rather than on failures is a different feature, one that fills a CI
    artifact with the runs nobody needs.

    Args:
        item: The test item being reported on.
        call: The phase pytest is reporting.

    Yields:
        Nothing of its own; forwards the report the rest of the hook chain built.

    Returns:
        The report, unchanged. This hook observes and never rewrites: a wrapper
        that altered a report would make the harness able to change a suite's
        verdict, which is exactly the authority a test harness must not hold.
    """
    report = yield
    setattr(item, f"{_REPORT_ATTR}{report.when}", report)
    return report


def _phase_failed(item: pytest.Item, phase: str) -> bool | None:
    """Whether *phase* failed, or ``None`` when the hook never ran.

    Args:
        item: The test item.
        phase: ``"setup"`` or ``"call"``.

    Returns:
        True or False from the stashed report; ``None`` when there is none,
        which means this module was imported for its fixtures but never
        registered as a plugin.
    """
    report: pytest.TestReport | None = getattr(item, f"{_REPORT_ATTR}{phase}", None)
    if report is None:
        return None
    return report.failed


def _failed_in_any_phase(item: pytest.Item) -> bool | None:
    """Whether the test failed in setup or in its body, or ``None`` if unknown.

    Setup counts: a precondition that could not be read is exactly when evidence
    is worth keeping, and it never reaches the body.

    Args:
        item: The test item.

    Returns:
        True, False, or ``None`` when neither phase left a report — which means
        the hook never ran.
    """
    phases = [_phase_failed(item, phase) for phase in ("call", "setup")]
    if all(phase is None for phase in phases):
        return None
    return any(phase for phase in phases)


# ---------------------------------------------------------------------------
# Override points: the variances the harness does not assume are shared
# ---------------------------------------------------------------------------


@pytest.fixture
def harness_environ() -> Mapping[str, str]:
    """The environment this run reads, as one immutable snapshot.

    A snapshot rather than :data:`os.environ` itself so every fixture in one test
    reads the same values: identity minting, tenant auth and evidence redaction
    each read the environment, and a test that mutates it half way through would
    otherwise produce a connection whose name and whose purge disagree.

    Returns:
        A copy of the process environment. Override to drive a suite from a
        config file, or to run two tenants in one session.
    """
    return dict(os.environ)


@pytest.fixture
def harness_budget_profile() -> BudgetProfile:
    """Which timing tier this suite's waits run on.

    Returns:
        :data:`~application_sdk.testing.harness.budgets.CONNECTOR_CI`, whose
        numbers are today's connector defaults verbatim. Override with another
        profile for a local cluster or a shared tenant, rather than re-deriving
        each budget at its call site.
    """
    return CONNECTOR_CI


@pytest.fixture
def harness_substrate() -> Substrate:
    """Where this suite is running, relative to the app under test.

    Returns:
        :attr:`~application_sdk.testing.harness.substrate.Substrate.LOCAL` — the
        substrate that needs no credentials. Defaulting to ``KUBECONFIG`` would
        make a suite that forgot to declare its substrate reach for the ambient
        kubeconfig, which on a developer's machine is whichever cluster
        ``kubectl`` last pointed at.
    """
    return Substrate.LOCAL


@pytest.fixture
def harness_kube_context() -> str | None:
    """Kubeconfig context cluster reads go through.

    Returns:
        ``None`` — the kubeconfig's current context. Override to pin a context so
        a local run cannot silently follow ``kubectl config use-context``.
    """
    return None


@pytest.fixture
def harness_connection_type() -> str:
    """Atlan catalog type segment for the connection this run creates.

    The connector's ``connection_type`` where it differs from its short name
    (OpenAPI publishes as ``api``), else the short name.

    Returns:
        Never returns by default.

    Raises:
        FixtureNotConfiguredError: Always, unless overridden. There is no
            defensible default: this string is the second segment of the
            qualified name teardown purges under, so a guessed value is a purge
            aimed at the wrong prefix.
    """
    raise FixtureNotConfiguredError(
        message=(
            "harness_connection_type is not configured. Override it in your "
            "conftest with the Atlan catalog type segment this suite publishes "
            "under (e.g. 'postgres', or 'api' for OpenAPI) — it is the prefix "
            "teardown purges, so the harness will not guess it."
        ),
        fixture="harness_connection_type",
    )


@pytest.fixture
def harness_app_under_test() -> AppUnderTest:
    """Where to find the app under test inside a cluster.

    Returns:
        Never returns by default.

    Raises:
        FixtureNotConfiguredError: Always, unless overridden. App wiring is one
            of the variances FND-224 records as explicitly not shared: the
            connector harness talks to a docker-compose worker on ``localhost``
            and the runtime suite to a named Deployment in a namespace, so there
            is no value that is right for both.
    """
    raise FixtureNotConfiguredError(
        message=(
            "harness_app_under_test is not configured. Override it in your "
            "conftest with AppUnderTest(app_name=..., namespace=...) for the app "
            "this suite drives."
        ),
        fixture="harness_app_under_test",
    )


@pytest.fixture
def harness_worker_health_url(harness_environ: Mapping[str, str]) -> str | None:
    """Health endpoint the default precondition polls, if there is one.

    Args:
        harness_environ: The environment snapshot.

    Returns:
        ``E2E_WORKER_HEALTH_URL`` when the ambient environment sets it — the
        variable the shared ``sdr-e2e`` CI action already exports and
        ``BaseE2ETest.assert_worker_up`` already reads, so the connector side
        gets its precondition without declaring anything. ``None`` when it is
        unset or blank, which is how a suite on another substrate opts out.
    """
    return harness_environ.get("E2E_WORKER_HEALTH_URL", "").strip() or None


@pytest.fixture
def harness_precondition_checks(
    harness_worker_health_url: str | None, harness_budget_profile: BudgetProfile
) -> Sequence[PreconditionCheck]:
    """What must be true before this suite dispatches any work.

    Args:
        harness_worker_health_url: Endpoint to require, or ``None``.
        harness_budget_profile: Supplies the
            :attr:`~application_sdk.testing.harness.budgets.Wait.WORKER_HEALTH`
            budget.

    Returns:
        The worker-health check when a URL is configured, else an empty tuple.
        Override to declare the runtime side's gate — the poller check needs a
        Temporal reader, a queue and a build id, none of which the harness can
        derive, so it is composed by whoever knows them:

        .. code-block:: python

            @pytest.fixture
            def harness_precondition_checks(temporal_reader, harness_budget_profile):
                return (
                    check_no_stale_pollers(
                        reader=temporal_reader,
                        queue="atlan-my-app",
                        namespace="default",
                        current_build_id="abc123",
                        budget=harness_budget_profile.budgets[Wait.WORKER_HEALTH],
                    ),
                )
    """
    if harness_worker_health_url is None:
        return ()
    return (
        check_worker_health(
            harness_worker_health_url,
            budget=harness_budget_profile.budgets[Wait.WORKER_HEALTH],
        ),
    )


@pytest.fixture
def harness_evidence_dir() -> Path | None:
    """Directory a failed test's evidence bundle is written under.

    Returns:
        :data:`DEFAULT_EVIDENCE_DIR`. ``None`` opts out of writing entirely, for
        a suite that ships its evidence somewhere else.
    """
    return DEFAULT_EVIDENCE_DIR


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------


@pytest.fixture
def harness_minter(harness_environ: Mapping[str, str]) -> Minter:
    """The per-run identifier minter, wired to the real clock and CI run id.

    Args:
        harness_environ: The environment snapshot, read for ``GITHUB_RUN_ID``.

    Returns:
        A :class:`~application_sdk.testing.harness.identity.Minter`. Override
        with an injected clock to make a suite's minted names assertable.
    """
    return Minter.from_environment(harness_environ)


@pytest.fixture
def harness_run_id(harness_minter: Minter) -> int:
    """Identifier scoping every name this run mints.

    Args:
        harness_minter: The minter.

    Returns:
        The ambient CI run id when there is a numeric one, else a clock reading.
    """
    return harness_minter.run_id()


@pytest.fixture
def harness_connection_identity(
    harness_minter: Minter, harness_connection_type: str
) -> ConnectionIdentity:
    """The ephemeral connection this test creates, and teardown purges.

    Args:
        harness_minter: The minter.
        harness_connection_type: Atlan catalog type segment.

    Returns:
        A qualified name and display name built from one suffix. Function-scoped
        so two tests in one suite cannot share a connection — a shared qualified
        name would let one test's teardown purge the other's assets and mix its
        Atlas counts.
    """
    return harness_minter.connection_identity(harness_connection_type)


# ---------------------------------------------------------------------------
# Tenant and AE wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def harness_tenant_auth(harness_environ: Mapping[str, str]) -> TenantAuth:
    """How this run authenticates against the tenant under test.

    Args:
        harness_environ: The environment snapshot.

    Returns:
        The resolved credentials.

    Raises:
        MissingTenantEnvError: When ``ATLAN_BASE_URL`` or ``ATLAN_API_KEY`` is
            absent. Raised rather than skipped: a tenant-facing suite that runs
            green against no tenant is the failure mode this harness exists to
            remove, and a suite that legitimately needs no tenant does not
            request this fixture.
    """
    return read_tenant_auth(harness_environ)


@pytest.fixture
async def harness_atlas_client(
    harness_tenant_auth: TenantAuth,
) -> AsyncIterator[AsyncAtlanClient]:
    """One ``AsyncAtlanClient`` for the whole test, closed at teardown.

    One client per test rather than per call is the point: the five
    ``asyncio.run`` sites this harness replaced stood up a fresh client, and so a
    fresh TLS handshake, on every poll iteration.

    Args:
        harness_tenant_auth: Tenant credentials. OAuth identity is preferred when
            configured, per
            :func:`~application_sdk.testing.harness.atlas.atlas_client`.

    Yields:
        The open client.
    """
    async with atlas_client(
        harness_tenant_auth.base_url,
        harness_tenant_auth.api_key,
        oauth_client_id=harness_tenant_auth.oauth_client_id,
        oauth_client_secret=harness_tenant_auth.oauth_client_secret,
    ) as client:
        yield client


@pytest.fixture
async def harness_ae_client(harness_tenant_auth: TenantAuth) -> AsyncIterator[AEClient]:
    """An Automation Engine client over one pooled connection, closed at teardown.

    Args:
        harness_tenant_auth: Tenant credentials. The API key rather than the
            OAuth pair, because ``/automation/api/v1/*`` needs the realm-admin
            ``resource_access`` role only that service account carries.

    Yields:
        The client. Its pool is bound to the loop that first used it, which is
        the other reason this fixture is function-scoped.
    """
    client = AEClient(harness_tenant_auth.base_url, harness_tenant_auth.api_key)
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Cluster reader selection
# ---------------------------------------------------------------------------


@pytest.fixture
def harness_cluster_reader(
    harness_substrate: Substrate, harness_kube_context: str | None
) -> ClusterReader:
    """A read-only cluster reader for the declared substrate.

    Args:
        harness_substrate: Where this suite is running.
        harness_kube_context: Context to pin, or ``None``.

    Returns:
        The reader.

    Raises:
        SubstrateHasNoClusterError: On the local substrate, which has no
            Kubernetes API to read.
        HarnessNotBuiltError: On the in-cluster substrate, until FND-248.
    """
    return cluster_reader_for(harness_substrate, kube_context=harness_kube_context)


# ---------------------------------------------------------------------------
# The precondition gate
# ---------------------------------------------------------------------------


@pytest.fixture
async def harness_preconditions(
    harness_precondition_checks: Sequence[PreconditionCheck],
) -> GateReport:
    """Assert the starting state before the test dispatches any work.

    Requesting this fixture is what runs the gate, so a scenario cannot forget
    to: the checks run during setup, and an unmet one fails the test before its
    body executes.

    Args:
        harness_precondition_checks: The checks to run.

    Returns:
        The passing report, so a test can put the readings into its own
        evidence.

    Raises:
        PreconditionsFailedError: A readable precondition was not met.
        PreconditionsIndeterminateError: A precondition could not be read.
            Neither is an ``AssertionError``, so pytest reports an unfit
            environment as an *error* rather than as a failure of the thing under
            test.
    """
    report = await run_preconditions(harness_precondition_checks)
    assert_gate(report)
    return report


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class EvidenceLog:
    """A mutable builder for one test's :class:`EvidenceBundle`.

    The bundle is frozen, which is right for the thing that ships and wrong for
    the thing a running scenario accumulates into. This is the accumulating half:
    a scenario records readings and findings as it makes them, and the fixture
    freezes it into a bundle if the test fails.

    Accumulate-don't-truncate is the same rule the outcome vocabulary follows: a
    red leg should carry every finding the run produced, not the first one.

    Args:
        label: What the run was, for the report title.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._findings: list[Finding] = []
        self._logs: MutableMapping[str, Sequence[str]] = {}
        self._readings: MutableMapping[str, object] = {}
        self._artifacts: MutableMapping[str, str] = {}

    def add_finding(self, finding: Finding) -> None:
        """Record an unmet expectation.

        Args:
            finding: The finding to keep.
        """
        self._findings.append(finding)

    def add_logs(self, source: str, lines: Sequence[str]) -> None:
        """Record captured log lines.

        Args:
            source: Pod name, container name, or a synthetic name for a non-pod
                source.
            lines: The captured lines, in order.
        """
        self._logs[source] = tuple(lines)

    def record(self, name: str, value: object) -> None:
        """Record one named observation.

        Args:
            name: What was observed ("table_count", "poller_identities").
            value: The observation.
        """
        self._readings[name] = value

    def add_artifact(self, relative_path: str, contents: str) -> None:
        """Record a file to write alongside the report.

        Args:
            relative_path: Path relative to the bundle's output directory.
            contents: File contents.
        """
        self._artifacts[relative_path] = contents

    def merge(self, bundle: EvidenceBundle) -> None:
        """Fold another bundle's contents into this one.

        The seam for evidence a scenario collects itself — pod logs from a
        cluster reader, for instance — so this builder does not have to know how
        to collect anything.

        Args:
            bundle: The bundle to fold in. Its ``label`` is not adopted; keys it
                shares with this log overwrite.
        """
        self._findings.extend(bundle.findings)
        self._logs.update(bundle.logs)
        self._readings.update(bundle.readings)
        self._artifacts.update(bundle.artifacts)

    def bundle(self) -> EvidenceBundle:
        """Freeze what has been accumulated.

        Returns:
            An :class:`EvidenceBundle` holding copies, so later additions to this
            log cannot mutate a bundle already handed out.
        """
        return EvidenceBundle(
            label=self.label,
            findings=tuple(self._findings),
            logs=dict(self._logs),
            readings=dict(self._readings),
            artifacts=dict(self._artifacts),
        )

    @property
    def is_empty(self) -> bool:
        """Whether anything at all has been accumulated.

        Returns:
            True when there is nothing worth writing.
        """
        return not (self._findings or self._logs or self._readings or self._artifacts)


@pytest.fixture
def harness_evidence(
    request: pytest.FixtureRequest,
    harness_environ: Mapping[str, str],
    harness_evidence_dir: Path | None,
) -> Iterator[EvidenceLog]:
    """Accumulate evidence, and write it — redacted — if the test fails.

    Args:
        request: The test item, for its name and its verdict.
        harness_environ: Environment snapshot, read for the credential-shaped
            values that must not reach an artifact.
        harness_evidence_dir: Where to write, or ``None`` to opt out.

    Yields:
        The :class:`EvidenceLog` to record into.
    """
    item: pytest.Item = request.node
    log = EvidenceLog(item.name)
    try:
        yield log
    finally:
        _write_if_failed(
            log,
            name=item.name,
            nodeid=item.nodeid,
            failed=_failed_in_any_phase(item),
            environ=harness_environ,
            evidence_dir=harness_evidence_dir,
        )


def _write_if_failed(
    log: EvidenceLog,
    *,
    name: str,
    nodeid: str,
    failed: bool | None,
    environ: Mapping[str, str],
    evidence_dir: Path | None,
) -> None:
    """Write *log*'s bundle when the test it belongs to failed.

    Every failure here is swallowed with a warning, for the reason teardown's
    are: this runs after the assertions have decided the verdict, so raising
    would replace a real failure with a bookkeeping error and lose the
    diagnosis. That covers the window before child G (FND-243) lands the writer
    too — the persist functions are resolved by name at call time rather than
    imported at module scope, so this module loads and every other fixture works
    on a tree where they do not exist yet.

    Args:
        log: The accumulated evidence.
        name: Test name, for the log lines.
        nodeid: Test node id, which becomes the bundle's directory name.
        failed: Whether the test failed, or ``None`` for "cannot tell".
        environ: Environment snapshot, for the secret values to redact.
        evidence_dir: Root to write under, or ``None``.
    """
    if evidence_dir is None or log.is_empty:
        return
    if failed is None:
        logger.warning(
            "harness_evidence cannot tell whether %s failed, so it wrote "
            "nothing: add pytest_plugins = "
            '["application_sdk.testing.harness.fixtures"] to your ROOT '
            "conftest.py, which is what registers the report hook",
            name,
        )
        return
    if not failed:
        return
    try:
        secrets = _child_g("secrets_from_environment", _SecretsReader)(
            environ, also=("ATLAN_BASE_URL",)
        )
        written = _child_g("write_bundle", _BundleWriter)(
            log.bundle(), evidence_dir / _slug(nodeid), secrets=secrets
        )
    except Exception:
        logger.warning(
            "could not write the evidence bundle for %s — the test result "
            "stands; only the evidence is missing",
            name,
            exc_info=True,
        )
        return
    logger.info("wrote %d evidence file(s) for failed test %s", len(written), name)


def _slug(nodeid: str) -> str:
    """Turn a pytest node id into one path segment.

    Args:
        nodeid: The node id, which carries ``/``, ``::`` and parametrisation
            brackets.

    Returns:
        A single filesystem-safe segment. Every separator is replaced rather
        than only ``/``, so a parametrised id cannot produce a nested path.
    """
    safe = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in nodeid
    )
    return safe.strip("_") or "test"


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


@pytest.fixture
async def harness_connection_teardown(
    harness_connection_identity: ConnectionIdentity,
    harness_atlas_client: AsyncAtlanClient,
) -> AsyncIterator[ConnectionIdentity]:
    """Yield this test's connection identity, and purge it afterwards.

    Requesting this fixture is what guarantees the purge, so a scenario cannot
    create a connection and forget to clean it up. It runs after every test —
    pass, fail or error — because assets a failed run leaves behind accumulate in
    a shared tenant, and a leftover half-set-up connection is exactly what greens
    a later run that should have failed.

    Args:
        harness_connection_identity: The connection this test creates.
        harness_atlas_client: The already-open client the purge reuses, so
            teardown costs no second TLS handshake.

    Yields:
        The identity, so the test does not need both fixtures.
    """
    try:
        yield harness_connection_identity
    finally:
        await _purge_quietly(
            harness_atlas_client, harness_connection_identity.qualified_name
        )


async def _purge_quietly(client: AsyncAtlanClient, qualified_name: str) -> None:
    """Purge *qualified_name*, reporting any failure rather than raising.

    A failed purge is reported and never raised — teardown runs after the
    assertions have decided the verdict, and raising here replaces a real failure
    with a cleanup error. The broad ``except`` is that property, not laziness: it
    also covers the window before child G (FND-243) lands the implementation,
    where the scaffold raises
    :class:`~application_sdk.testing.harness._errors.HarnessNotBuiltError` and a
    suite composing these fixtures should still run.

    Args:
        client: Open tenant client.
        qualified_name: The ephemeral connection to purge.
    """
    try:
        purge = cast(_PurgeConnection, teardown_api.purge_connection)
        report = await purge(client, qualified_name)
    except Exception:
        logger.warning(
            "e2e cleanup: purge of %s did not run — manual purge may be needed",
            qualified_name,
            exc_info=True,
        )
        return
    if report.orphaned or report.errors:
        logger.warning(
            "e2e cleanup: purged %d asset(s) under %s but left %d orphaned; "
            "%d batch error(s): %s",
            report.purged,
            qualified_name,
            len(report.orphaned),
            len(report.errors),
            "; ".join(report.errors),
        )


# ---------------------------------------------------------------------------
# The sync bridge's loop
# ---------------------------------------------------------------------------


@pytest.fixture
def harness_sync_bridge() -> Iterator[None]:
    """Close the sync bridge's event loop for this thread at teardown.

    For a *synchronous* composer — the third entry shape — which reaches the
    harness through :func:`~application_sdk.testing.harness.bridge.run_sync`
    rather than through the async fixtures here. The bridge keeps one loop per
    thread for the life of the thread; this is the fixture the bridge's own
    documentation means when it says teardown belongs to the caller.

    Yields:
        Nothing. Request it for its teardown.
    """
    try:
        yield
    finally:
        close_loop()
