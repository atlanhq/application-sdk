"""Internal: cloud detection for Polaris-vended file storage.

Polaris vends short-lived storage credentials whose shape varies by cloud:
S3 STS sessions on AWS, GCS HMAC keys on GCP, ADLS SAS tokens on Azure.
The SDK reads ``CLOUD`` from the environment and normalises it to one of
``"aws" | "gcp" | "azure"``. An unset / empty / unknown value defaults to
``"aws"`` to match the convention used by automation-engine-app.
"""

from __future__ import annotations

import os
from typing import Literal

Cloud = Literal["aws", "gcp", "azure"]


def detect_cloud() -> Cloud:
    """Return the cloud the app is running on, defaulting to ``"aws"``.

    Reads the ``CLOUD`` environment variable (same convention as
    automation-engine-app's ``settings.CLOUD``).
    """
    raw = os.environ.get("CLOUD", "").strip().lower()
    if raw in ("aws", "gcp", "azure"):
        return raw  # type: ignore[return-value]
    return "aws"


def aws_region() -> str:
    """Return the AWS region, defaulting to ``""`` if unset.

    Used both for the DuckDB write path (popularity-app's documented
    workaround for vended creds missing region) and for the AWS S3
    DuckDB secret.
    """
    return os.environ.get("AWS_REGION", "")
