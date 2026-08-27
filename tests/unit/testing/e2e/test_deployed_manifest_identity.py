"""Tests for the post-submit deployed-manifest identity check (FND-129).

Three layers, all tenant-free:

* :mod:`application_sdk.testing.e2e._manifest_identity` — pure identity
  reduction and diffing.
* ``AEWorkflowClient.get_published_version`` — envelope tolerance, and the
  contract that an unreadable read answers ``None`` rather than raising.
* ``BaseE2ETest._assert_deployed_manifest_matches`` — which outcomes fail the
  leg (exactly one: a positive finding) and which only log.

The last group is the point of the ticket: a leg that asserts against the wrong
app must go red, and every *unanswerable* outcome must not.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.errors.base import AppError
from application_sdk.testing.e2e import BaseE2ETest
from application_sdk.testing.e2e._errors import DeployedManifestMismatchError
from application_sdk.testing.e2e._manifest_identity import (
    DagNodeIdentity,
    compare_node_identities,
    node_identities,
)
from application_sdk.testing.e2e.base import _supersedes
from application_sdk.testing.e2e.client import AEWorkflowClient, PublishedVersion
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.automation_engine import AEClient
from application_sdk.testing.harness.automation_engine.wire import (
    first_version_row as _first_version_row,
)
from application_sdk.testing.harness.bridge import run_sync


def _node(app_name: str, workflow_type: str) -> dict[str, Any]:
    """A manifest-shaped node declaring both identity fields, as the real ones do."""
    return {
        "app_name": app_name,
        "inputs": {"app_name": app_name, "workflow_type": workflow_type},
    }


_LOCAL_DAG: dict[str, Any] = {
    "extract": _node("{app_name}", "MySQLWorkflow"),
    "publish": _node("publish", "PublishWorkflow"),
}


def _make_client() -> AEWorkflowClient:
    return AEWorkflowClient(
        tenant_url="https://tenant.example.invalid",
        api_token="tok-test",
    )


class _Harness(BaseE2ETest):
    connector_short_name = "mysql"
    argo_package_name = "@atlan/mysql"
    argo_template_name = "atlan-mysql"


def _harness(**overrides: Any) -> _Harness:
    """A harness with the check's inputs set directly, bypassing bootstrap.

    ``_bootstrap_workflow`` needs a tenant; the check under test only needs the
    two pieces of state bootstrap leaves behind.
    """
    harness = _Harness()
    harness._ae = AEClient("https://tenant.example.invalid", "tok-test")
    harness._expected_node_identities = node_identities(_LOCAL_DAG, app_name="mysql")
    harness._seed_version = 1000
    for key, value in overrides.items():
        setattr(harness, key, value)
    return harness


def _reads(harness: _Harness, *, returns: Any = None, side_effect: Any = None) -> Any:
    """Patch the AE reader the check polls, as the async method it now is."""
    return patch.object(
        harness._ae,
        "get_published_version",
        new=AsyncMock(return_value=returns, side_effect=side_effect),
    )


def _assert_matches(harness: _Harness, slug: str) -> None:
    """Drive the check from a synchronous test, through the harness' own bridge.

    Under ``fake_clock`` so the poll gaps cost nothing. The wait runs on
    ``poll_until`` -> ``until_deadline_async``, which sleeps through ``_poll``'s
    own swappable default — the reason the ``patch("time.sleep")`` these tests
    used to carry was inert, and why they still spent five real seconds between
    them separating two mocked reads (FND-962). Applied here rather than per
    test so a new waiting test cannot forget it; it is a no-op for the ones that
    settle on the first read.

    The fake advances only ``_poll``'s clock, never ``time.monotonic`` — the
    bridge's event loop reads that for its own timers.
    """
    with fake_clock():
        run_sync(harness._assert_deployed_manifest_matches(slug))


# ---------------------------------------------------------------------------
# node_identities
# ---------------------------------------------------------------------------


class TestNodeIdentities:
    def test_reduces_a_manifest_dag_to_name_app_and_workflow_type(self) -> None:
        ids = node_identities(_LOCAL_DAG, app_name="mysql")
        assert ids["extract"] == DagNodeIdentity(
            name="extract", app_name="mysql", workflow_type="MySQLWorkflow"
        )
        assert ids["publish"] == DagNodeIdentity(
            name="publish", app_name="publish", workflow_type="PublishWorkflow"
        )

    def test_app_name_placeholder_resolves_to_the_connector(self) -> None:
        """The whole point of ``app_name=``: a placeholder and its substituted
        form must compare equal, or every leg reports a changed extract node."""
        raw = node_identities({"extract": _node("{app_name}", "W")}, app_name="mysql")
        substituted = node_identities(
            {"extract": _node("mysql", "W")}, app_name="mysql"
        )
        assert compare_node_identities(raw, substituted).matches

    def test_node_level_app_name_wins_over_inputs(self) -> None:
        dag = {
            "extract": {
                "app_name": "outer",
                "inputs": {"app_name": "inner", "workflow_type": "W"},
            }
        }
        assert node_identities(dag)["extract"].app_name == "outer"

    def test_inputs_app_name_is_the_fallback(self) -> None:
        dag = {"extract": {"inputs": {"app_name": "inner", "workflow_type": "W"}}}
        assert node_identities(dag)["extract"].app_name == "inner"

    def test_a_node_that_is_not_an_object_still_appears_in_the_node_set(self) -> None:
        """A malformed node must not silently vanish — its absence from the set
        would read as a node-set match that isn't one."""
        ids = node_identities({"extract": "not-an-object"})
        assert ids["extract"] == DagNodeIdentity(name="extract")

    def test_non_string_identity_fields_read_as_undeclared(self) -> None:
        dag = {"extract": {"app_name": 7, "inputs": {"workflow_type": None}}}
        assert node_identities(dag)["extract"] == DagNodeIdentity(name="extract")

    def test_empty_dag_yields_no_identities(self) -> None:
        assert node_identities({}) == {}


# ---------------------------------------------------------------------------
# compare_node_identities
# ---------------------------------------------------------------------------


class TestCompareNodeIdentities:
    def test_identical_dags_match(self) -> None:
        ids = node_identities(_LOCAL_DAG, app_name="mysql")
        assert compare_node_identities(ids, ids).matches

    def test_a_node_the_published_dag_lacks_is_reported_missing(self) -> None:
        expected = node_identities(_LOCAL_DAG, app_name="mysql")
        actual = node_identities({"extract": _node("mysql", "MySQLWorkflow")})
        diff = compare_node_identities(expected, actual)
        assert not diff.matches
        assert [n.name for n in diff.missing] == ["publish"]
        assert diff.unexpected == ()
        assert "declared locally but absent" in diff.render()

    def test_an_extra_published_node_is_reported_unexpected(self) -> None:
        expected = node_identities({"extract": _node("mysql", "MySQLWorkflow")})
        actual = node_identities(_LOCAL_DAG, app_name="mysql")
        diff = compare_node_identities(expected, actual)
        assert [n.name for n in diff.unexpected] == ["publish"]
        assert "present in the published DAG" in diff.render()

    def test_a_changed_workflow_type_is_a_mismatch(self) -> None:
        expected = node_identities(_LOCAL_DAG, app_name="mysql")
        actual = node_identities(
            {**_LOCAL_DAG, "extract": _node("mysql", "OldMySQLWorkflow")},
            app_name="mysql",
        )
        diff = compare_node_identities(expected, actual)
        assert [c.name for c in diff.changed] == ["extract"]
        assert "OldMySQLWorkflow" in diff.render()

    def test_a_changed_app_name_is_a_mismatch(self) -> None:
        expected = node_identities(_LOCAL_DAG, app_name="mysql")
        actual = node_identities(
            {**_LOCAL_DAG, "publish": _node("some-other-app", "PublishWorkflow")},
            app_name="mysql",
        )
        assert [c.name for c in compare_node_identities(expected, actual).changed] == [
            "publish"
        ]

    def test_a_field_the_published_dag_omits_is_not_a_mismatch(self) -> None:
        """Tolerance is deliberate: the published envelope is Heracles' to
        change, and an omission is not evidence about the app under test."""
        expected = node_identities(_LOCAL_DAG, app_name="mysql")
        actual = node_identities(
            {"extract": {"app_name": "mysql"}, "publish": {"app_name": "publish"}}
        )
        assert compare_node_identities(expected, actual).matches

    def test_render_of_a_match_says_so(self) -> None:
        ids = node_identities(_LOCAL_DAG, app_name="mysql")
        assert compare_node_identities(ids, ids).render() == "no difference"


# ---------------------------------------------------------------------------
# _first_version_row
# ---------------------------------------------------------------------------


class TestFirstVersionRow:
    _ROW = {"version": 7, "dag": {"extract": {}}}

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"data": [_ROW]}, id="data-list"),
            pytest.param({"data": {"records": [_ROW]}}, id="data-records"),
            pytest.param({"data": {"versions": [_ROW]}}, id="data-versions"),
            pytest.param({"data": {"items": [_ROW]}}, id="data-items"),
            pytest.param({"data": _ROW}, id="data-object"),
            pytest.param([_ROW], id="bare-list"),
            pytest.param(_ROW, id="bare-object"),
        ],
    )
    def test_accepts_every_plausible_envelope(self, body: Any) -> None:
        assert _first_version_row(body) == self._ROW

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"data": []}, id="empty-list"),
            pytest.param({"data": {"total": 0}}, id="no-rows-and-no-record"),
            pytest.param("not json", id="text"),
            pytest.param(None, id="none"),
        ],
    )
    def test_returns_none_when_no_record_is_present(self, body: Any) -> None:
        assert _first_version_row(body) is None


# ---------------------------------------------------------------------------
# AEWorkflowClient.get_published_version
# ---------------------------------------------------------------------------


class TestGetPublishedVersion:
    def test_reads_the_published_version_and_its_dag(self) -> None:
        client = _make_client()
        body = {"data": [{"version": 42, "dag": _LOCAL_DAG}]}
        with patch.object(client._ae, "_request", return_value=(200, body)) as request:
            published = client.get_published_version("mysql-abc")
        assert published == PublishedVersion(version=42, dag=_LOCAL_DAG)
        _, path = request.call_args.args
        assert path == (
            "/automation/api/v1/workflows/mysql-abc/versions"
            "?is_published=true&page=0&page_size=1"
        )

    def test_the_slug_is_url_encoded(self) -> None:
        client = _make_client()
        with patch.object(
            client._ae, "_request", return_value=(200, {"data": []})
        ) as req:
            client.get_published_version("a/b?c")
        assert "workflows/a%2Fb%3Fc/versions" in req.call_args.args[1]

    def test_a_version_row_without_a_dag_yields_an_empty_dag(self) -> None:
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(200, {"data": [{}]})):
            published = client.get_published_version("s")
        assert published == PublishedVersion(version=None, dag={})

    @pytest.mark.parametrize("status", [404, 500, 503])
    def test_a_non_2xx_answers_none_rather_than_raising(self, status: int) -> None:
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(status, {"err": "x"})):
            assert client.get_published_version("s") is None

    def test_a_transport_failure_answers_none_rather_than_raising(self) -> None:
        """The check is an enhancement on top of the run; a read that cannot get
        through must never be the thing that fails the leg."""
        client = _make_client()
        with patch.object(client._ae, "_request", side_effect=AppError(message="down")):
            assert client.get_published_version("s") is None

    def test_an_unparseable_envelope_answers_none(self) -> None:
        client = _make_client()
        with patch.object(client._ae, "_request", return_value=(200, "not json")):
            assert client.get_published_version("s") is None


# ---------------------------------------------------------------------------
# BaseE2ETest._assert_deployed_manifest_matches
# ---------------------------------------------------------------------------


def _published(dag: dict[str, Any], version: int | None = 2000) -> PublishedVersion:
    return PublishedVersion(version=version, dag=dag)


class TestAssertDeployedManifestMatches:
    def test_a_matching_published_dag_passes(self) -> None:
        harness = _harness()
        with _reads(harness, returns=_published(_LOCAL_DAG)):
            _assert_matches(harness, "mysql-abc")

    def test_a_diverging_published_dag_fails_the_leg(self) -> None:
        harness = _harness()
        deployed = {"extract": _node("mysql", "OldMySQLWorkflow")}
        with (
            _reads(harness, returns=_published(deployed)),
            pytest.raises(DeployedManifestMismatchError) as exc,
        ):
            _assert_matches(harness, "mysql-abc")
        # The diff has to name the nodes, not merely report a mismatch.
        assert "OldMySQLWorkflow" in exc.value.message
        assert "publish" in exc.value.message
        assert exc.value.observed is not None
        assert "publish" in exc.value.observed

    def test_the_check_is_skippable_per_connector(self) -> None:
        harness = _harness()
        read = AsyncMock(return_value=None)
        with (
            patch.object(type(harness), "assert_deployed_manifest", False),
            patch.object(harness._ae, "get_published_version", new=read),
        ):
            _assert_matches(harness, "mysql-abc")
        read.assert_not_called()

    def test_a_suite_with_no_manifest_derived_dag_skips_without_reading(self) -> None:
        harness = _harness()
        harness._expected_node_identities = {}
        read = AsyncMock(return_value=None)
        with patch.object(harness._ae, "get_published_version", new=read):
            _assert_matches(harness, "mysql-abc")
        read.assert_not_called()

    def test_a_published_version_that_never_supersedes_the_seed_asserts_nothing(
        self,
    ) -> None:
        """AE still serving the harness's own seed means the only DAG on offer is
        the one the harness uploaded. Comparing it to itself would pass whatever
        the tenant runs, so the honest outcome is to assert nothing — even when
        the DAG differs, as it does here."""
        harness = _harness(
            deployed_manifest_timeout_seconds=1,
            deployed_manifest_poll_interval_seconds=1,
        )
        seed_echo = _published({"extract": _node("mysql", "Divergent")}, version=1000)
        with _reads(harness, returns=seed_echo):
            _assert_matches(harness, "mysql-abc")

    def test_an_unreadable_published_version_asserts_nothing(self) -> None:
        harness = _harness(
            deployed_manifest_timeout_seconds=1,
            deployed_manifest_poll_interval_seconds=1,
        )
        with _reads(harness, returns=None):
            _assert_matches(harness, "mysql-abc")

    def test_an_empty_published_dag_asserts_nothing(self) -> None:
        harness = _harness(
            deployed_manifest_timeout_seconds=1,
            deployed_manifest_poll_interval_seconds=1,
        )
        with _reads(harness, returns=_published({})):
            _assert_matches(harness, "mysql-abc")

    def test_a_late_supersede_is_waited_for(self) -> None:
        """AE's version listing is read-after-write: the first read can still be
        the seed. One retry inside the budget must be enough to see the real DAG."""
        harness = _harness(
            deployed_manifest_timeout_seconds=30,
            deployed_manifest_poll_interval_seconds=1,
        )
        reads = [
            _published(_LOCAL_DAG, version=1000),  # still the seed
            None,  # a blip
            _published({"extract": _node("mysql", "Divergent")}),
        ]
        with (
            _reads(harness, side_effect=reads),
            pytest.raises(DeployedManifestMismatchError),
        ):
            _assert_matches(harness, "mysql-abc")

    def test_a_published_version_with_no_version_number_asserts_nothing(self) -> None:
        """AE's version number is optional on the wire, so ``_safe_int`` can
        yield None — and ``None != seed`` is true. Comparing on inequality alone
        would treat an unattributable record as superseding the seed and diff a
        DAG it cannot prove came from the tenant."""
        harness = _harness(
            deployed_manifest_timeout_seconds=1,
            deployed_manifest_poll_interval_seconds=1,
        )
        unnumbered = _published({"extract": _node("mysql", "Divergent")}, version=None)
        with _reads(harness, returns=unnumbered):
            _assert_matches(harness, "mysql-abc")

    def test_an_unnumbered_read_does_not_end_the_wait(self) -> None:
        """It is unprovable, not terminal: a numbered supersede later in the
        budget must still be found and compared."""
        harness = _harness(
            deployed_manifest_timeout_seconds=30,
            deployed_manifest_poll_interval_seconds=1,
        )
        reads = [
            _published({"extract": _node("mysql", "Divergent")}, version=None),
            _published({"extract": _node("mysql", "Divergent")}, version=2000),
        ]
        with (
            _reads(harness, side_effect=reads),
            pytest.raises(DeployedManifestMismatchError),
        ):
            _assert_matches(harness, "mysql-abc")

    def test_a_readable_read_survives_a_later_blip(self) -> None:
        """A blip after a good read must not make the diagnostic claim AE was
        never readable — that is a different, and wronger, thing to report."""
        harness = _harness(
            deployed_manifest_timeout_seconds=2,
            deployed_manifest_poll_interval_seconds=1,
        )
        reads = [_published({}, version=1000), None]
        with _reads(harness, side_effect=reads):
            _assert_matches(harness, "mysql-abc")

    def test_an_empty_first_read_does_not_end_the_wait(self) -> None:
        harness = _harness(
            deployed_manifest_timeout_seconds=30,
            deployed_manifest_poll_interval_seconds=1,
        )
        reads = [
            _published({}),
            _published({"extract": _node("mysql", "Divergent")}),
        ]
        with (
            _reads(harness, side_effect=reads),
            pytest.raises(DeployedManifestMismatchError),
        ):
            _assert_matches(harness, "mysql-abc")

    def test_a_harness_with_no_seed_version_compares_the_first_readable_dag(
        self,
    ) -> None:
        """No seed of our own (a pre-seeded slug) means there is nothing for AE
        to supersede, so the supersede gate cannot apply and the published DAG
        is compared as read."""
        harness = _harness()
        harness._seed_version = None
        with (
            _reads(
                harness, returns=_published({"extract": _node("mysql", "Divergent")})
            ),
            pytest.raises(DeployedManifestMismatchError),
        ):
            _assert_matches(harness, "mysql-abc")


class TestSupersedes:
    """The predicate that decides whether the comparison is meaningful at all."""

    @pytest.mark.parametrize(
        ("published", "seed", "expected"),
        [
            pytest.param(2000, 1000, True, id="a-different-number-supersedes"),
            pytest.param(1000, 1000, False, id="the-same-number-does-not"),
            pytest.param(None, 1000, False, id="an-absent-number-proves-nothing"),
            pytest.param(2000, None, True, id="no-seed-needs-no-proof"),
            pytest.param(None, None, True, id="no-seed-even-without-a-number"),
        ],
    )
    def test_only_a_provable_replacement_counts(
        self, published: int | None, seed: int | None, expected: bool
    ) -> None:
        assert _supersedes(published, seed) is expected


class TestCaptureExpectedNodeIdentities:
    def test_a_manifest_derived_seed_dag_is_captured(self) -> None:
        harness = _Harness()
        harness._capture_expected_node_identities(dict(_LOCAL_DAG))
        assert set(harness._expected_node_identities) == {"extract", "publish"}
        # {app_name} resolved against the connector, so the later comparison is
        # normalised on both sides.
        assert harness._expected_node_identities["extract"].app_name == "mysql"

    def test_a_hand_crafted_legacy_seed_dag_is_not_captured(self) -> None:
        """``manifest_path == ''`` means the seed DAG is the harness's own
        approximation of the app's graph, not a copy of it — nothing to compare."""
        harness = _Harness()
        with patch.object(type(harness), "manifest_path", ""):
            harness._capture_expected_node_identities(dict(_LOCAL_DAG))
        assert harness._expected_node_identities == {}
