"""DAG layer of :class:`~application_sdk.testing.e2e.base.BaseE2ETest`.

The active DAG run, the seed DAG and the AE payload. Part of
the linear mixin chain described in
:mod:`application_sdk.testing.e2e._base_config` (FND-4267).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    HarnessMethodNotImplementedError,
)
from application_sdk.testing.e2e._manifest_identity import (
    node_identities,
)
from application_sdk.testing.e2e.payload import (
    build_ae_payload,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; both are lazy imports
    pass

from application_sdk.testing.e2e._base_seed import _E2ESeedMixin
from application_sdk.testing.e2e._base_types import (
    _CONNECTION_SELECTOR_SUBSTITUTION,
    DAGSpec,
    NodeDispatch,
    ResolvedDAG,
)

logger = get_logger(__name__)


class _E2EDagMixin(_E2ESeedMixin):
    """The active DAG run, the seed DAG and the AE payload."""

    def _submit_may_create_connection(self) -> bool:
        """Whether the DAG about to be submitted could create the active connection.

        Answers ``False`` only on positive evidence that the run is about some
        *other* connection, and both halves of that evidence are required:

        * the run declares ``expect_connection = False`` — it is not graded on
          landing a Connection, so nothing says it publishes one; and
        * its ``{{connection-qualified-name}}`` substitution (the toolkit's
          ``ConnectionSelector`` input) names connections, none of them this
          one.

        Either half alone is not enough. A crawler expects its connection, and
        mints it from the ``{{connection}}`` substitution whatever else it is
        handed. A miner may not expect one, but with no selector pointing
        elsewhere the harness cannot tell which connection it enriches. Every
        doubtful case answers ``True``: an extra delete costs a minute of a
        runner, a missed one leaks a connection onto a shared tenant.

        The selector is read from the substitutions :meth:`_build_ae_payload`
        recorded for this run, not by calling the hook again: a suite that
        overrides ``_build_ae_payload`` records none, and is treated as giving
        no evidence.

        Returns:
            Whether a submit now may bring ``self.connection_qualified_name``
            into being.
        """
        if self._dag.expect_connection:
            return True
        substitutions = getattr(self, "_payload_substitutions", None)
        if substitutions is None:
            return True
        selected = substitutions.model_dump(by_alias=True, mode="json").get(
            _CONNECTION_SELECTOR_SUBSTITUTION
        )
        if isinstance(selected, str):
            selected = [selected]
        if not isinstance(selected, list):
            return True
        named = {qn for qn in selected if isinstance(qn, str) and qn}
        return not named or self.connection_qualified_name in named

    @property
    def _dag(self) -> ResolvedDAG:
        """The DAG this run is submitting and grading — the class's by default.

        ``getattr`` rather than an attribute read: a good deal of this class is
        unit-tested on an instance that never ran ``setup_method``, and the
        default has to be the class's own run there too.
        """
        active = getattr(self, "_active_dag", None)
        return active if active is not None else self.resolve_dag(None)

    @contextmanager
    def _dag_run(self, spec: DAGSpec | None) -> Iterator[ResolvedDAG]:
        """Make *spec* the active run for the duration of the block.

        ``None`` is a no-op that yields whatever is already active, so
        ``run_full_dag()`` called with no spec from inside an outer block runs
        that block's DAG rather than resetting to the class's.

        A run that names its own ``connection_qualified_name`` also *rebinds*
        ``self.connection_qualified_name`` for the block, and restores it after.
        Rebinding the attribute rather than threading the resolved value through
        every reader is deliberate: the connection is read at two dozen sites
        (the submit payload, every Atlas probe, the outcome, the evidence
        bundle), they all mean "the connection this run is about", and a
        parameter added to each is a parameter the next site can forget to
        forward. The QN is registered for teardown on the way in, so a run that
        prepares a different connection cannot leave one behind.

        Args:
            spec: The run to activate, or ``None`` to leave the active one
                alone.

        Yields:
            The resolved run in force inside the block.
        """
        if spec is None:
            yield self._dag
            return
        previous = getattr(self, "_active_dag", None)
        previous_qn = getattr(self, "connection_qualified_name", "")
        self._active_dag = self.resolve_dag(spec)
        run_qn = self._active_dag.connection_qualified_name
        if run_qn and run_qn != previous_qn:
            self.connection_qualified_name = run_qn
            if run_qn not in self._seeded_connection_qns:
                self._seeded_connection_qns.append(run_qn)
        try:
            yield self._active_dag
        finally:
            self._active_dag = previous
            self.connection_qualified_name = previous_qn

    def _resolved_entrypoint(self) -> str:
        """App-entrypoint for AE's manifest fetch: explicit ``entrypoint`` if set,
        else derived from ``manifest_path`` (``.../generated/<ep>/manifest.json`` ->
        ``<ep>``). Empty means single-entrypoint (no selector sent).

        Both halves are per-run since FND-1157: a suite running several
        entrypoint DAGs resolves this against the run in flight, not against the
        class.
        """
        return self._dag.entrypoint

    def _build_ae_payload(self, slug: str) -> dict[str, Any]:
        """Compose the AE submit payload from typed hook results.

        Subclasses never override this method — they override the two
        typed hooks above.

        The substitutions are kept on ``_payload_substitutions`` so the submit
        can read what it is about to send without calling the hook again — see
        :meth:`_submit_may_create_connection`.
        """
        self._payload_substitutions = self._mustache_substitutions()
        return build_ae_payload(
            run_id=self.run_id,
            mode=self.mode,
            connector_short_name=self.connector_short_name,
            argo_package_name=self.argo_package_name,
            argo_template_name=self.argo_template_name,
            app_service_url=self.app_service_url,
            connection=self.connection_spec(),
            mustache_subs=self._payload_substitutions,
            credential_body=self._credential_body(),
            ae_workflow_slug=slug,
            entrypoint=self._resolved_entrypoint(),
            agent_json=self.agent_json(),
            credential_type=self.resolved_connector_config_name(),
        )

    def _build_legacy_seed_dag(self, extract_queue: str) -> dict[str, Any]:
        """Build a hand-crafted seed DAG when ``manifest_path`` is unset.

        Base class always raises — only :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`
        overrides this (it has the SQL-specific task-queue and DAG-shape
        knowledge needed). Non-SQL connectors must always ship a
        manifest.json and set ``manifest_path``.
        """
        raise HarnessMethodNotImplementedError(
            message="non-SQL connectors must set manifest_path; SQL connectors must override _build_legacy_seed_dag()",
            operation="_build_legacy_seed_dag",
        )

    def _capture_node_dispatch(self, seed_dag: dict[str, Any]) -> None:
        """Record node name -> (app_name, task_queue) from the seed DAG.

        Read back only by :meth:`_describe_dag_nodes`, to name the queue a
        never-dispatched node was waiting on. Reads the *resolved*
        ``inputs.task_queue`` in preference to the node-level
        ``app_task_queue``: only the former has its ``{deployment_name}``
        placeholder substituted by :meth:`_seed_dag_from_manifest`.
        """
        dispatch: dict[str, NodeDispatch] = {}
        for name, node in seed_dag.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            queue = inputs.get("task_queue") if isinstance(inputs, dict) else None
            if not isinstance(queue, str) or not queue:
                queue = node.get("app_task_queue")
            app_name = node.get("app_name")
            dispatch[name] = NodeDispatch(
                app_name=app_name if isinstance(app_name, str) else "",
                task_queue=queue if isinstance(queue, str) else "",
            )
        self._node_dispatch = dispatch

    def _capture_expected_node_identities(self, seed_dag: dict[str, Any]) -> None:
        """Record the node identities the app under test declares.

        Read back only by :meth:`_assert_deployed_manifest_matches`.

        Captured from the seed DAG rather than by re-reading the file: the seed
        DAG *is* the manifest's ``dag``, already carrying this harness's
        ``{app_name}`` substitution, so the two sides of the later comparison
        are normalised the same way. A connector with no ``manifest_path`` built
        its seed DAG by hand (``_build_legacy_seed_dag``) — that is an
        approximation of the app's graph, not a copy of it, so there is nothing
        to compare and the identities stay empty.
        """
        if not self._dag.manifest_path:
            logger.info(
                "manifest_path empty — the deployed-manifest identity check has "
                "no committed DAG to compare against and will self-skip"
            )
            self._expected_node_identities = {}
            return
        self._expected_node_identities = node_identities(
            seed_dag, app_name=self.connector_short_name
        )

    def _ae_workflow_name_suffix(self) -> str:
        """``-<label>`` for a run that is not this suite's own, else ``""``.

        ``create_workflow`` reuses an existing workflow of the same name, so
        without this every run in a multi-DAG suite would publish its seed over
        the previous run's — one AE workflow carrying two different graphs, and
        an AE run list in which a crawl and a mine are indistinguishable.

        The suffix is keyed off the *resolved* run rather than off "was a spec
        passed", so a suite that spells its own run out as an explicit
        ``DAGSpec()`` gets the same workflow name it would have got implicitly.
        That is what keeps every pre-existing suite's AE workflow name byte-identical.

        Returns:
            The suffix, or ``""`` when this run resolves to the class's own.
        """
        dag = self._dag
        if dag == self.resolve_dag(None):
            return ""
        return f"-{dag.label}"
