"""The three AE write endpoints, and the parsers that keep them forward-compatible.

These paths existed before FND-242 but were never counted: they lived in
``testing/e2e/client.py``, which coverage omits because it only runs against a
live tenant. They are harness code now, under the same gate as everything else
in the package, so the branches each endpoint takes on a malformed or unhappy
response are asserted rather than assumed.

Each endpoint has the same shape and the same two ways to fail: a non-2xx, and
a 2xx whose body is missing the one field the caller needs. The second is the
one worth having — a create that "succeeded" without returning a slug fails two
calls later, at the version create, with an error naming the wrong endpoint.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.automation_engine._errors import (
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
)
from application_sdk.testing.harness.automation_engine.client import AEClient
from application_sdk.testing.harness.automation_engine.retry import (
    parse_run_timestamp,
    rotate_submit_credential_name,
)
from application_sdk.testing.harness.automation_engine.wire import (
    DAGNodeStatus,
    DAGRunStatus,
    safe_int,
    safe_node_status,
    safe_run_status,
)

_SLEEP = "application_sdk.testing.harness.automation_engine.client.sleep_async"


def _client() -> AEClient:
    return AEClient("https://tenant.example.com", "tok")


class TestCreateWorkflow:
    async def test_reads_the_slug_from_either_envelope(self) -> None:
        for body in ({"slug": "flat"}, {"data": {"slug": "nested"}}):
            client = _client()
            with patch.object(client, "_request", return_value=(200, body)):
                assert await client.create_workflow("n") in ("flat", "nested")

    async def test_a_2xx_without_a_slug_is_an_invariant_failure(self) -> None:
        """Not an HTTP error: AE answered, and the answer was unusable. Naming
        that here stops it surfacing two calls later as a version-create 404."""
        client = _client()
        with (
            patch.object(client, "_request", return_value=(200, {"data": {}})),
            pytest.raises(AtlanApiResponseInvariantError, match="no slug"),
        ):
            await client.create_workflow("n")

    async def test_a_non_2xx_carries_the_origins_backoff_request(self) -> None:
        client = _client()
        body = {"err": "overloaded", "retry_after": 45}
        with (
            patch.object(client, "_request", return_value=(503, body)),
            patch(_SLEEP),
            pytest.raises(AtlanApiHttpError) as caught,
        ):
            await client.create_workflow("n", retries=0)
        assert caught.value.retry_after_seconds == 45


class TestCreateVersion:
    async def test_returns_the_version_number(self) -> None:
        client = _client()
        with patch.object(client, "_request", return_value=(200, {"version": 17})):
            assert await client.create_version("slug", {}) == 17

    async def test_a_2xx_without_a_version_raises_http_error(self) -> None:
        client = _client()
        with (
            patch.object(client, "_request", return_value=(200, {"data": {}})),
            pytest.raises(AtlanApiHttpError, match="create_version failed"),
        ):
            await client.create_version("slug", {})

    async def test_a_404_is_retried_as_indexing_lag(self) -> None:
        """The slug is not queryable the instant ``create_workflow`` returns, so
        a 404 here is lag rather than a missing workflow."""
        client = _client()
        with (
            patch.object(
                client,
                "_request",
                side_effect=[(404, {"err": "not found"}), (200, {"version": 3})],
            ) as request,
            patch(_SLEEP),
        ):
            assert await client.create_version("slug", {}) == 3
        assert request.call_count == 2


class TestPublishVersion:
    async def test_a_success_body_returns_quietly(self) -> None:
        client = _client()
        with patch.object(
            client, "_request", return_value=(200, {"status": "success"})
        ):
            assert await client.publish_version("slug", 1) is None

    async def test_a_2xx_that_does_not_say_success_is_retried_then_raises(self) -> None:
        """The publish is the one endpoint whose 2xx is not itself the answer —
        AE returns 200 with a non-success body while the version is still
        settling."""
        client = _client()
        with (
            patch.object(client, "_request", return_value=(200, {"status": "queued"})),
            patch(_SLEEP),
            pytest.raises(AtlanApiHttpError, match="publish_version failed"),
        ):
            await client.publish_version("slug", 1, retries=2)


class TestGetNativeStatus:
    async def test_a_non_2xx_raises_with_the_origins_backoff(self) -> None:
        """The poll loop reads ``retry_after_seconds`` off this leaf to size its
        next gap, so it has to survive the raise."""
        client = _client()
        with (
            patch.object(client, "_request", return_value=(503, {"retry_after": 90})),
            pytest.raises(AtlanApiHttpError) as caught,
        ):
            await client.get_native_status("run-1")
        assert caught.value.retry_after_seconds == 90

    async def test_a_non_dict_body_is_also_a_failure(self) -> None:
        client = _client()
        with (
            patch.object(
                client, "_request", return_value=(200, "<html>gateway</html>")
            ),
            pytest.raises(AtlanApiHttpError),
        ):
            await client.get_native_status("run-1")


class TestPollNativeStatusWithNoResponse:
    async def test_a_budget_spent_without_one_reading_raises(self) -> None:
        """There is no last observation to stamp, so returning one would mean
        inventing it."""
        from application_sdk.testing.harness.automation_engine._errors import (
            AtlanApiTimeoutError,
        )

        client = _client()
        err = AtlanApiHttpError(message="500", target="native-status")
        with (
            patch.object(client, "get_native_status", side_effect=err),
            fake_clock(),
            pytest.raises(AtlanApiTimeoutError, match="with no response"),
        ):
            await client.poll_native_status(
                "run-1",
                interval_seconds=10,
                timeout_seconds=30,
                # Above the number of polls the budget allows, so the streak
                # never gives up and the deadline is what ends the loop.
                max_transient_failures=99,
            )


class TestDefensiveParsers:
    """Unknown shapes read as "unknown", never as a plausible value.

    Every one of these decides something: a status that guesses ``Succeeded``
    ends a poll early, and a timestamp that guesses "recent" adopts the wrong
    run — or re-POSTs a non-idempotent submit.
    """

    @pytest.mark.parametrize("raw", [None, 42, {"status": "Succeeded"}, True])
    def test_a_non_string_status_is_pending(self, raw: Any) -> None:
        assert safe_node_status(raw) is DAGNodeStatus.PENDING
        assert safe_run_status(raw) is DAGRunStatus.PENDING

    @pytest.mark.parametrize("raw", ["abc", {}, [1]])
    def test_an_uncastable_number_is_none(self, raw: Any) -> None:
        assert safe_int(raw) is None

    def test_an_out_of_range_epoch_is_unknown_not_clamped(self) -> None:
        """A timestamp this function cannot read must never come back as a
        date — that value decides whether a submit is re-issued."""
        assert parse_run_timestamp(1e30) is None

    def test_rotating_a_credential_name_tolerates_every_missing_layer(self) -> None:
        """A public source submits no credential at all, so each absent layer is
        a no-op rather than an AttributeError mid-retry."""
        for body in (
            None,
            {},
            {"payload": []},
            {"payload": ["not a dict"]},
            {"payload": [{"body": "not a dict"}]},
            {"payload": [{"body": {}}]},
        ):
            rotate_submit_credential_name(body)  # type: ignore[arg-type]
