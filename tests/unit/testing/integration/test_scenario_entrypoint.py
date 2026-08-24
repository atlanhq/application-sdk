"""Tests for workflow-endpoint resolution, including the entrypoint selector.

``Scenario(api="workflow", ...)`` says "POST /workflows/v1/start". It did not say
WHICH ``@entrypoint`` gets started, so on a multi-entrypoint app every workflow
scenario started whichever entrypoint the app resolves as default — a suite
believing it exercised the miner could in fact run the crawler, and pass.
``Scenario.entrypoint`` / ``BaseIntegrationTest.entrypoint`` name the target.

The property under most scrutiny here is backward compatibility: with nothing
declared, the emitted endpoint must be byte-identical to what shipped before.
``TestNothingDeclared`` is that control.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from application_sdk.testing.integration.assertions import equals
from application_sdk.testing.integration.models import Scenario
from application_sdk.testing.integration.runner import BaseIntegrationTest


def _scenario(**overrides: object) -> Scenario:
    kwargs: dict[str, object] = {
        "name": "start_workflow",
        "api": "workflow",
        "assert_that": {"success": equals(True)},
    }
    kwargs.update(overrides)
    return Scenario(**kwargs)  # type: ignore[arg-type]


class _Suite(BaseIntegrationTest):
    """Declares no entrypoint — the pre-existing shape."""


class _MinerSuite(BaseIntegrationTest):
    """Suite-wide default entrypoint."""

    entrypoint = "miner"


def _wired_suite() -> _MinerSuite:
    """A _MinerSuite ready to run one scenario without a server.

    ``_results`` is normally seeded by ``setup_class``, which also builds a real
    client and health-checks a server; ``_execute_scenario`` appends to it in a
    ``finally``, so it has to exist here.
    """
    suite = _MinerSuite()
    type(suite)._results = []
    suite.client = MagicMock()  # type: ignore[attr-defined]
    suite.client.call_api.return_value = {"success": True}
    suite._build_scenario_args = lambda scenario: {}  # type: ignore[method-assign]
    return suite


class _CustomEndpointSuite(BaseIntegrationTest):
    """A suite that already customised its endpoint, query string included."""

    workflow_endpoint = "/extract?mode=fast"
    entrypoint = "miner"


# ---------------------------------------------------------------------------
# Backward compatibility — the control
# ---------------------------------------------------------------------------


class TestNothingDeclared:
    def test_bare_workflow_endpoint_is_unchanged(self) -> None:
        assert _Suite()._resolve_workflow_endpoint(_scenario()) == "/start"

    def test_the_class_default_is_empty(self) -> None:
        assert BaseIntegrationTest.entrypoint == ""

    def test_the_scenario_field_defaults_to_none(self) -> None:
        assert _scenario().entrypoint is None

    def test_a_custom_workflow_endpoint_is_untouched(self) -> None:
        class _Custom(BaseIntegrationTest):
            workflow_endpoint = "/extract"

        assert _Custom()._resolve_workflow_endpoint(_scenario()) == "/extract"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_scenario_entrypoint_is_appended(self) -> None:
        resolved = _Suite()._resolve_workflow_endpoint(_scenario(entrypoint="miner"))
        assert resolved == "/start?entrypoint=miner"

    def test_class_entrypoint_applies_when_the_scenario_is_silent(self) -> None:
        resolved = _MinerSuite()._resolve_workflow_endpoint(_scenario())
        assert resolved == "/start?entrypoint=miner"

    def test_scenario_entrypoint_beats_the_class_default(self) -> None:
        resolved = _MinerSuite()._resolve_workflow_endpoint(
            _scenario(entrypoint="crawler")
        )
        assert resolved == "/start?entrypoint=crawler"

    def test_explicit_endpoint_wins_outright(self) -> None:
        """A hand-written full override must not gain a second selector.

        Suites in the fleet already write this by hand; they have to keep
        behaving identically rather than emit two conflicting entrypoints.
        """
        resolved = _MinerSuite()._resolve_workflow_endpoint(
            _scenario(endpoint="/start?entrypoint=crawler")
        )
        assert resolved == "/start?entrypoint=crawler"

    def test_explicit_endpoint_wins_even_over_a_scenario_entrypoint(self) -> None:
        resolved = _Suite()._resolve_workflow_endpoint(
            _scenario(endpoint="/custom/start", entrypoint="miner")
        )
        assert resolved == "/custom/start"


# ---------------------------------------------------------------------------
# Query-string composition
# ---------------------------------------------------------------------------


class TestQueryStringComposition:
    def test_merges_with_an_existing_query_string(self) -> None:
        """A second '?' would make the whole selector unparseable."""
        resolved = _CustomEndpointSuite()._resolve_workflow_endpoint(_scenario())
        assert resolved == "/extract?mode=fast&entrypoint=miner"
        assert resolved.count("?") == 1

    def test_the_value_is_url_encoded(self) -> None:
        resolved = _Suite()._resolve_workflow_endpoint(
            _scenario(entrypoint="extract metadata&x=1")
        )
        assert resolved == "/start?entrypoint=extract%20metadata%26x%3D1"

    def test_a_kebab_case_name_survives_unescaped(self) -> None:
        """Hyphens are the normal wire form and must stay readable."""
        resolved = _Suite()._resolve_workflow_endpoint(
            _scenario(entrypoint="extract-metadata")
        )
        assert resolved == "/start?entrypoint=extract-metadata"

    def test_an_empty_scenario_entrypoint_falls_through_to_the_class(self) -> None:
        """ "" is 'unset', not 'send an empty selector'."""
        resolved = _MinerSuite()._resolve_workflow_endpoint(_scenario(entrypoint=""))
        assert resolved == "/start?entrypoint=miner"

    def test_an_empty_entrypoint_everywhere_sends_no_selector(self) -> None:
        resolved = _Suite()._resolve_workflow_endpoint(_scenario(entrypoint=""))
        assert resolved == "/start"


# ---------------------------------------------------------------------------
# api dispatch is case-insensitive
# ---------------------------------------------------------------------------


class TestApiDispatchIsCaseInsensitive:
    """The endpoint override must reach the client whatever the api's casing.

    ``_build_scenario_args`` and ``client.call_api`` both normalise with
    ``.lower()``; the override site did not, so ``api="Workflow"`` built workflow
    args and then dropped the endpoint — taking the entrypoint selector with it.
    """

    def test_capitalised_api_still_forwards_the_endpoint(self) -> None:
        suite = _wired_suite()

        suite._execute_scenario(_scenario(api="Workflow"))

        kwargs = suite.client.call_api.call_args.kwargs
        assert kwargs["endpoint_override"] == "/start?entrypoint=miner"

    def test_a_non_workflow_api_still_sends_no_override(self) -> None:
        suite = _wired_suite()

        suite._execute_scenario(_scenario(api="auth"))

        assert suite.client.call_api.call_args.kwargs["endpoint_override"] is None
