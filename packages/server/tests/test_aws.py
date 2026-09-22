"""The AWS helpers, which had no test at all.

`assume_role_across_regions` is reached from a connector's `load()`, which
SQLHandler awaits — so every blocking round trip it makes stalls the event loop
for every other app sharing the host process. It used to fetch the full region
list (an EC2 round trip) and then walk every region sequentially, even when the
caller already knew the answer.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from server_sdk.aws import (
    _reset_region_cache,
    assume_role_across_regions,
    assume_role_across_regions_async,
    create_aws_client,
    get_all_aws_regions,
    get_region_name_from_hostname,
)
from server_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
)

CREDS = {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}


class _FakeSTS:
    def __init__(self, succeed_in: str, log: list[str]) -> None:
        self._succeed_in, self._log = succeed_in, log

    def assume_role(self, **_: object) -> dict:
        region = self._log[-1]
        if region != self._succeed_in:
            raise RuntimeError(f"no access in {region}")
        return {"Credentials": CREDS}


@pytest.fixture
def boto3_stub(monkeypatch):
    """A boto3 whose sts client records every region it was built for."""
    log: list[str] = []

    def _make(succeed_in: str):
        def client(service: str, region_name: str | None = None, **_: object):
            log.append(region_name or "")
            if service == "sts":
                return _FakeSTS(succeed_in, log)
            raise AssertionError(f"unexpected client {service!r}")

        module = types.ModuleType("boto3")
        module.client = client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", module)
        return log

    return _make


# ── the hint is almost always right; don't pay for the fan-out ──────────────


def test_the_hint_alone_is_tried_first(boto3_stub) -> None:
    log = boto3_stub("eu-west-1")
    assert assume_role_across_regions("arn:x", region_hint="eu-west-1") == CREDS
    assert log == ["eu-west-1"], "the hint succeeding must cost exactly one call"


def test_a_wrong_hint_falls_back_to_every_region(boto3_stub) -> None:
    log = boto3_stub("us-west-2")
    assert assume_role_across_regions("arn:x", region_hint="eu-west-1") == CREDS
    assert log[0] == "eu-west-1"
    assert "us-west-2" in log
    assert log.count("eu-west-1") == 1, "the hint must not be retried in the fan-out"


def test_no_region_succeeding_raises_auth_error(boto3_stub) -> None:
    boto3_stub("nowhere")
    with pytest.raises(AuthError):
        assume_role_across_regions("arn:x", region_hint="eu-west-1")


def test_the_async_form_returns_the_same_thing(boto3_stub) -> None:
    boto3_stub("eu-west-1")
    got = asyncio.run(
        assume_role_across_regions_async("arn:x", region_hint="eu-west-1")
    )
    assert got == CREDS


def test_the_async_form_does_not_block_the_loop(boto3_stub) -> None:
    """The whole point: other coroutines must keep running."""
    boto3_stub("eu-west-1")
    ticks = 0

    async def scenario():
        nonlocal ticks

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.001)
                ticks += 1

        task = asyncio.create_task(ticker())
        await assume_role_across_regions_async("arn:x", region_hint="eu-west-1")
        await task

    asyncio.run(scenario())
    assert ticks == 20


# ── the region list is process-stable ───────────────────────────────────────


@pytest.fixture
def ec2_stub(monkeypatch):
    """A boto3 whose ec2 describe_regions can be made to fail, then recover."""

    state = {"fail": False, "calls": 0, "regions": ["eu-west-1", "us-east-1"]}

    def client(service: str, region_name: str | None = None, **_: object):
        if service != "ec2":
            raise AssertionError(f"unexpected client {service!r}")
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("describe_regions is having a moment")

        class _EC2:
            @staticmethod
            def describe_regions() -> dict:
                return {"Regions": [{"RegionName": r} for r in state["regions"]]}

        return _EC2()

    module = types.ModuleType("boto3")
    module.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", module)
    _reset_region_cache()
    yield state
    _reset_region_cache()


def test_the_region_list_is_cached_and_callers_cannot_corrupt_it(ec2_stub) -> None:
    first = get_all_aws_regions()
    first.append("not-a-region")
    assert "not-a-region" not in get_all_aws_regions()
    assert ec2_stub["calls"] == 1, "a successful fetch must be made exactly once"


def test_a_failed_fetch_is_not_pinned_for_the_life_of_the_process(ec2_stub) -> None:
    """The fallback list must never become the cached answer.

    Under @lru_cache it was cached like any other result, so one transient
    describe_regions failure -- an expired instance credential, a throttle, a
    moment without egress -- froze the process on a region list missing every
    region AWS has added since it was written. The only reader is the
    assume-role sweep, so a role that lives in a newer region then failed with
    "no region succeeded" forever, on a pod that looked healthy.
    """
    ec2_stub["fail"] = True
    degraded = get_all_aws_regions()
    assert "ap-northeast-1" in degraded, "expected the static fallback"

    ec2_stub["fail"] = False
    assert get_all_aws_regions() == ["eu-west-1", "us-east-1"]


# ── a caller's malformed credentials are a 400, not an AWS outage ───────────


@pytest.mark.parametrize(
    "creds",
    [
        {"SecretAccessKey": "S", "SessionToken": "T"},
        {"AccessKeyId": "A", "SessionToken": "T"},
        {"AccessKeyId": "A", "SecretAccessKey": "S"},
        {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": ""},
        {},
    ],
    ids=["no-key-id", "no-secret", "no-token", "blank-token", "empty"],
)
def test_incomplete_temp_credentials_are_the_callers_fault(creds: dict) -> None:
    """A missing key used to raise KeyError INSIDE the try, where the blanket
    handler turned it into AwsClientCreationError -- DEPENDENCY_UNAVAILABLE,
    503, retryable. So a caller bug no retry can fix was reported as AWS being
    down, and then retried against AWS."""
    with pytest.raises(InvalidInputError) as caught:
        create_aws_client("redshift", "eu-west-1", temp_credentials=creds)
    assert not isinstance(caught.value, DependencyUnavailableError)


def test_the_incomplete_credentials_error_names_keys_not_values() -> None:
    """It reaches the caller and the log, and the values are STS credentials."""
    with pytest.raises(InvalidInputError) as caught:
        create_aws_client(
            "redshift",
            "eu-west-1",
            temp_credentials={
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "s3cr3t",
            },
        )
    rendered = f"{caught.value} {caught.value.context}"
    assert "SessionToken" in rendered, "the caller must be told which key is missing"
    assert "AKIAEXAMPLE" not in rendered
    assert "s3cr3t" not in rendered


# ── hostname -> region ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "region"),
    [
        ("dev-atlan.cjr9uiz1p4ve.ap-south-1.redshift.amazonaws.com", "ap-south-1"),
        ("my-cluster.abc123.us-east-1.redshift.amazonaws.com", "us-east-1"),
    ],
)
def test_region_is_read_from_the_hostname(host: str, region: str) -> None:
    assert get_region_name_from_hostname(host) == region


def test_a_region_less_hostname_is_a_typed_error() -> None:
    with pytest.raises(InvalidInputError):
        get_region_name_from_hostname("warehouse.internal")
