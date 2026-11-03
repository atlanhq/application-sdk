#!/usr/bin/env python3
"""Wait for the storage-emulator sidecars, then provision their buckets.

Used by the ``Storage Emulator Tests`` job in sdk-tests-reusable.yaml after it
has started MinIO (S3), Azurite (Azure Blob) and storage-testbench behind an
nginx proxy (GCS).

It replaces inline ``for i in $(seq ..); do curl … && break; sleep 2; done``
loops and ``… || true`` provisioning. Those exited 0 whether or not an emulator
came up or a bucket was created, so a dead emulator produced a green job made of
skips and a late one produced a hard ``NoSuchBucket`` in the tests. Here every
emulator must answer before the deadline and every bucket must exist afterwards,
or the step fails and names the one that did not.

Provisioning is idempotent: "already exists" counts as success, so a re-run on
a warm runner does not fail. Any other error fails the step.

All credentials below are the emulators' documented, public defaults (MinIO
``minioadmin``, Azurite's well-known ``devstoreaccount1`` key); none of them
grant access to anything but the local containers.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

MINIO_ENDPOINT = "http://localhost:9000"
AZURITE_ENDPOINT = "http://localhost:10000"
GCS_ENDPOINT = "http://localhost:9095"

S3_BUCKETS = (
    "sdk-emulator-test",
    # SDR ENABLE_ATLAN_UPLOAD handoff: customer-managed + internal Atlan buckets.
    "sdk-customer-objectstore",
    "sdk-atlan-objectstore",
)
AZURE_CONTAINER = "sdk-emulator-test"
GCS_BUCKET = "sdk-emulator-test"
GCS_PROJECT = "test"
# Bound each aws / az call: a hung CLI must fail this step, not the job timeout.
CLI_TIMEOUT_SECONDS = 60

MINIO_ENV = {
    "AWS_ACCESS_KEY_ID": "minioadmin",
    "AWS_SECRET_ACCESS_KEY": "minioadmin",
    "AWS_DEFAULT_REGION": "us-east-1",
}
AZURITE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    f"BlobEndpoint={AZURITE_ENDPOINT}/devstoreaccount1;"
)


@dataclass(frozen=True)
class Probe:
    name: str
    url: str
    ready: Callable[[int], bool]


PROBES = (
    Probe("MinIO", f"{MINIO_ENDPOINT}/minio/health/live", lambda s: s == 200),
    # Azurite answers the bare account URL with 400: any non-5xx means it is up.
    Probe("Azurite", f"{AZURITE_ENDPOINT}/devstoreaccount1", lambda s: s < 500),
    Probe(
        "GCS proxy",
        f"{GCS_ENDPOINT}/storage/v1/b?project={GCS_PROJECT}",
        lambda s: 200 <= s < 300,
    ),
)


def http_status(
    url: str, *, method: str = "GET", body: bytes | None = None
) -> int | None:
    """HTTP status for ``url``, or ``None`` when nothing answered."""
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the ``aws`` / ``az`` CLIs.

    Bounded by ``CLI_TIMEOUT_SECONDS``: a hung CLI comes back as a failed call
    (exit 124, like ``timeout(1)``) instead of hanging until the job timeout.
    """
    try:
        return subprocess.run(cmd, timeout=CLI_TIMEOUT_SECONDS, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"{cmd[0]} timed out after {CLI_TIMEOUT_SECONDS}s"
        )


def wait_for(
    probes: tuple[Probe, ...],
    timeout_s: float,
    interval_s: float,
    *,
    status: Callable[[str], int | None] = http_status,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Poll every probe until it is ready or the shared deadline passes.

    Returns the names of the probes that never became ready (empty = all up).
    """
    deadline = clock() + timeout_s
    pending = list(probes)
    last: dict[str, int | None] = {}
    while True:
        still = []
        for probe in pending:
            code = status(probe.url)
            last[probe.name] = code
            # A request can take seconds; an answer that lands after the
            # deadline does not count, so the deadline really bounds the wait.
            if code is None or not probe.ready(code) or clock() > deadline:
                still.append(probe)
        pending = still
        if not pending or clock() >= deadline:
            break
        sleep(interval_s)
    for probe in pending:
        seen = last.get(probe.name)
        answer = "no response" if seen is None else f"HTTP {seen}"
        print(
            f"::error::{probe.name} not ready after {timeout_s:.0f}s "
            f"({probe.url}: {answer})"
        )
    return [p.name for p in pending]


def create_s3_bucket(bucket: str) -> bool:
    result = run(
        [
            "aws",
            "--endpoint-url",
            MINIO_ENDPOINT,
            "s3api",
            "create-bucket",
            "--bucket",
            bucket,
        ],
        env={**os.environ, **MINIO_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 or "BucketAlreadyOwnedByYou" in result.stderr:
        return True
    print(f"::error::S3 bucket {bucket} not created: {result.stderr.strip()}")
    return False


def create_azure_container(container: str) -> bool:
    # `az storage container create` exits 0 with "created": false when the
    # container already exists, so the exit code alone is the verdict.
    result = run(
        [
            "az",
            "storage",
            "container",
            "create",
            "--name",
            container,
            "--connection-string",
            AZURITE_CONNECTION_STRING,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    print(f"::error::Azure container {container} not created: {result.stderr.strip()}")
    return False


def create_gcs_bucket(
    bucket: str,
    *,
    request: Callable[..., int | None] = http_status,
) -> bool:
    code = request(
        f"{GCS_ENDPOINT}/storage/v1/b?project={GCS_PROJECT}",
        method="POST",
        body=json.dumps({"name": bucket}).encode(),
    )
    # 409 = already exists.
    if code is not None and (200 <= code < 300 or code == 409):
        return True
    answer = "no response" if code is None else f"HTTP {code}"
    print(f"::error::GCS bucket {bucket} not created: {answer}")
    return False


def provision() -> bool:
    # Attempt every bucket even after a failure, so one run reports them all.
    results = [create_s3_bucket(b) for b in S3_BUCKETS]
    results.append(create_azure_container(AZURE_CONTAINER))
    results.append(create_gcs_bucket(GCS_BUCKET))
    return all(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for all emulators together (default 120)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between polls (default 2)",
    )
    args = parser.parse_args(argv)

    if wait_for(PROBES, args.timeout, args.interval):
        return 1
    print("All emulators ready.")
    if not provision():
        return 1
    print("Buckets and container provisioned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
