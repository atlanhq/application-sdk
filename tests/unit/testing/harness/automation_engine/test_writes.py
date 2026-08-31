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

import httpx
import pytest

from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.automation_engine._errors import (
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
    AtlanApiTimeoutError,
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


class TestSlugResolves:
    """The readiness read that replaced an unconditional ``time.sleep(3)``.

    The narrowing is the whole content: a 2xx and a 404 are the two answers AE
    actually gives about a slug, and everything else is *not* an answer. Reading
    an overloaded AE as "not indexed" would poll out a budget over a fact the
    read never learned.
    """

    async def test_a_2xx_means_ae_resolved_the_slug(self) -> None:
        client = _client()
        with patch.object(client, "_request", return_value=(200, {"data": []})):
            assert await client.slug_resolves("s") is True

    async def test_a_404_is_ae_saying_not_yet(self) -> None:
        client = _client()
        with patch.object(client, "_request", return_value=(404, {"error": "no"})):
            assert await client.slug_resolves("s") is False

    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    async def test_every_other_status_settles_nothing(self, status: int) -> None:
        client = _client()
        with patch.object(client, "_request", return_value=(status, {})):
            assert await client.slug_resolves("s") is None

    async def test_a_redirect_loop_is_not_absorbed(self) -> None:
        """The hole the docstring names, pinned so the claim cannot go stale.

        ``httpx.TooManyRedirects`` is a ``RequestError`` but not a
        ``TransportError``, so ``_request``'s ``except (httpx.TransportError,
        OSError)`` misses it — and the transport sets ``follow_redirects=True``,
        which makes it reachable rather than theoretical. If someone later widens
        that narrowing, this test fails and tells them the docstring's stated
        residue is now wrong too.
        """
        client = _client()
        with (
            patch.object(
                client, "_request", side_effect=httpx.TooManyRedirects("loop")
            ),
            pytest.raises(httpx.TooManyRedirects),
        ):
            await client.slug_resolves("s")

    async def test_an_unreachable_ae_settles_nothing(self) -> None:
        client = _client()
        with patch.object(
            client, "_request", side_effect=AtlanApiTimeoutError(message="down")
        ):
            assert await client.slug_resolves("s") is None

    async def test_the_slug_is_url_quoted(self) -> None:
        """A slug is AE-assigned, but it lands in a path segment, and a `/` in one
        would silently address a different route."""
        client = _client()
        with patch.object(client, "_request", return_value=(200, {})) as request:
            await client.slug_resolves("a/b c")
        assert "a%2Fb%20c/versions" in request.call_args.args[1]


class TestWaitForSlug:
    async def test_an_already_indexed_slug_costs_one_call_and_no_sleep(self) -> None:
        """The direction the fixed sleep was wrong in most often: it charged every
        run three seconds, and the usual answer is already yes at t=0."""
        client = _client()
        with (
            patch.object(client, "slug_resolves", return_value=True) as read,
            fake_clock() as clock,
        ):
            assert await client.wait_for_slug("s") is True
        assert read.await_count == 1
        assert clock.slept == []

    async def test_a_slow_index_is_polled_through(self) -> None:
        """The other direction: a fixed 3s sleep is no help at all on the run
        where indexing takes four."""
        client = _client()
        answers = [False, False, False, False, True]
        with (
            patch.object(client, "slug_resolves", side_effect=answers),
            fake_clock() as clock,
        ):
            assert await client.wait_for_slug("s") is True
        assert clock.slept == [1, 1, 1, 1]

    async def test_an_unreadable_probe_does_not_end_the_wait(self) -> None:
        """``None`` is not ``False``. A read that settled nothing must not be
        taken as AE having answered — in either direction.

        **The call count is the assertion, not the return value.** Returning
        ``True`` does not distinguish a correct predicate from a broken one: with
        ``settled=lambda r: r is not False`` the wait settles on the *first*
        ``None`` and still returns ``True``, so an assertion on the result alone
        passes either way. Confirmed mechanically — that mutation left all 37
        tests in this file green. Three probes is what only the correct predicate
        produces.
        """
        client = _client()
        with (
            patch.object(
                client, "slug_resolves", side_effect=[None, None, True]
            ) as read,
            fake_clock(),
        ):
            assert await client.wait_for_slug("s") is True
        assert read.await_count == 3, "an unreadable probe must not settle the wait"

    async def test_an_unresolved_slug_is_reported_not_raised(self) -> None:
        """Advisory, never a gate.

        ``create_version`` retries on 404 for exactly this reason and is what
        actually makes the sequence safe, so a readiness read that replaces a
        guess must not be stricter than the guess it replaced. A wait that ends
        unresolved hands over to that retry.
        """
        client = _client()
        with (
            patch.object(client, "slug_resolves", return_value=False),
            fake_clock(),
        ):
            assert await client.wait_for_slug("s", timeout_seconds=5) is False


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


class TestRetriesMeansRetries:
    """All four AE writes spend the same budget for the same ``retries=N``.

    ``create_version`` and ``publish_version`` used to pass
    ``total_attempts=retries``, so ``retries=5`` bought four while
    ``create_workflow``'s and ``submit_workflow``'s identically-named parameter
    bought five. FND-240 normalised them onto ``retries + 1``, the convention
    ``_post_with_retry`` documents and the only one the parameter's name is true
    under.

    Asserted as a *cross-endpoint* equality rather than four separate counts:
    the defect was never any single number, it was that two of them disagreed.
    """

    @pytest.mark.parametrize("retries", [0, 1, 3])
    async def test_the_two_versions_endpoints_match_create_workflow(
        self, retries: int
    ) -> None:
        counts: dict[str, int] = {}

        async def _count(name: str, response: tuple[int, Any]) -> None:
            client = _client()
            with (
                patch.object(client, "_request", return_value=response) as request,
                patch(_SLEEP),
                pytest.raises(AtlanApiHttpError),
            ):
                if name == "create_workflow":
                    await client.create_workflow("n", retries=retries)
                elif name == "create_version":
                    await client.create_version("slug", {}, retries=retries)
                else:
                    await client.publish_version("slug", 1, retries=retries)
            counts[name] = request.call_count

        # Each endpoint gets a response its own ``retryable`` predicate keeps
        # retrying, so the count is the budget rather than an early accept.
        await _count("create_workflow", (503, {}))
        await _count("create_version", (404, {}))
        await _count("publish_version", (200, {"status": "queued"}))

        assert counts["create_version"] == counts["create_workflow"]
        assert counts["publish_version"] == counts["create_workflow"]
        assert counts["create_workflow"] == retries + 1

    async def test_the_publish_failure_names_the_attempts_it_made(self) -> None:
        """The message said ``after {retries} attempts`` while making
        ``retries + 1``, which is the kind of off-by-one that reads as broken
        retry logic in a CI log."""
        client = _client()
        with (
            patch.object(client, "_request", return_value=(200, {"status": "queued"})),
            patch(_SLEEP),
            pytest.raises(AtlanApiHttpError, match=r"after 3 attempt\(s\)"),
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
