"""REQUEST_SIGNING protocol - cryptographically sign each request.

Covers AWS SigV4, HMAC, RSA signing (~30+ services).
Pattern: Sign each request with credentials.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse

from application_sdk.credentials.aliases import get_field_value
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import ApplyResult, MaterializeResult
from application_sdk.credentials.types import FieldSpec, FieldType


class RequestSigningProtocol(BaseProtocol):
    """Protocol for AWS SigV4, HMAC, RSA signing.

    Pattern: Cryptographically sign each request.

    Configuration options (via config or config_override):
        - algorithm: "aws_sigv4" or "hmac_sha256" or "hmac_sha512" (default: "aws_sigv4")
        - service: AWS service name for SigV4 (default: "execute-api")
        - signature_header: Header for signature (for HMAC)
        - timestamp_header: Header for timestamp (for HMAC)

    Examples:
        >>> # AWS SigV4 for S3
        >>> protocol = RequestSigningProtocol(config={
        ...     "algorithm": "aws_sigv4",
        ...     "service": "s3"
        ... })

        >>> # HMAC signing
        >>> protocol = RequestSigningProtocol(config={
        ...     "algorithm": "hmac_sha256",
        ...     "signature_header": "X-Signature",
        ...     "timestamp_header": "X-Timestamp"
        ... })
    """

    default_fields: List[FieldSpec] = [
        FieldSpec(
            name="access_key_id",
            display_name="Access Key ID",
            required=True,
        ),
        FieldSpec(
            name="secret_access_key",
            display_name="Secret Access Key",
            sensitive=True,
            field_type=FieldType.PASSWORD,
            required=True,
        ),
        FieldSpec(
            name="region",
            display_name="Region",
            default_value="us-east-1",
            required=False,
        ),
        FieldSpec(
            name="session_token",
            display_name="Session Token",
            sensitive=True,
            required=False,
            help_text="Required for temporary credentials (STS)",
        ),
    ]

    default_config: Dict[str, Any] = {
        "algorithm": "aws_sigv4",  # aws_sigv4, hmac_sha256, hmac_sha512
        "service": "execute-api",
        "signature_header": "X-Signature",
        "timestamp_header": "X-Timestamp",
    }

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Sign the request and add authorization headers."""
        algorithm = self.config.get("algorithm", "aws_sigv4")

        if algorithm == "aws_sigv4":
            return self._apply_aws_sigv4(credentials, request_info)
        elif algorithm.startswith("hmac"):
            return self._apply_hmac(credentials, request_info)
        else:
            # Unknown algorithm - return empty result
            return ApplyResult()

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Return credentials for SDK usage (e.g., boto3)."""
        access_key_id = get_field_value(credentials, "access_key_id")
        secret_access_key = get_field_value(credentials, "secret_access_key")
        region = get_field_value(credentials, "region") or "us-east-1"
        session_token = get_field_value(credentials, "session_token")

        result = {
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "region_name": region,
        }

        if session_token:
            result["aws_session_token"] = session_token

        return MaterializeResult(credentials=result)

    def _apply_aws_sigv4(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Apply AWS Signature Version 4 signing.

        This is a simplified implementation. For full SigV4 support,
        consider using botocore.auth.SigV4Auth or aws-requests-auth.
        """
        access_key = get_field_value(credentials, "access_key_id")
        secret_key = get_field_value(credentials, "secret_access_key")
        region = get_field_value(credentials, "region") or "us-east-1"
        session_token = get_field_value(credentials, "session_token")
        service = self.config.get("service", "execute-api")

        if not access_key or not secret_key:
            return ApplyResult()

        # Get request details
        method = request_info.get("method", "GET").upper()
        url = request_info.get("url", "")
        body = request_info.get("body", b"")

        if isinstance(body, str):
            body = body.encode("utf-8")
        elif body is None:
            body = b""

        # Parse URL
        parsed = urlparse(url)
        host = parsed.netloc
        path = parsed.path or "/"
        query = parsed.query

        # Create timestamp
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        # Create canonical request components
        signed_headers = "host;x-amz-date"
        if session_token:
            signed_headers += ";x-amz-security-token"

        # Hash payload
        payload_hash = hashlib.sha256(body).hexdigest()

        # Build canonical headers
        canonical_headers = f"host:{host}\nx-amz-date:{amz_date}\n"
        if session_token:
            canonical_headers += f"x-amz-security-token:{session_token}\n"

        # Build canonical request
        canonical_request = (
            f"{method}\n"
            f"{path}\n"
            f"{query}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        # Create string to sign
        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"{algorithm}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )

        # Calculate signature
        def sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = sign(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
        k_region = sign(k_date, region)
        k_service = sign(k_region, service)
        k_signing = sign(k_service, "aws4_request")
        signature = hmac.new(
            k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Build authorization header
        authorization = (
            f"{algorithm} "
            f"Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        result_headers = {
            "Authorization": authorization,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }

        if session_token:
            result_headers["x-amz-security-token"] = session_token

        return ApplyResult(headers=result_headers)

    def _apply_hmac(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Apply HMAC signature."""
        algorithm = self.config.get("algorithm", "hmac_sha256")
        hash_func = hashlib.sha256 if "256" in algorithm else hashlib.sha512

        secret = get_field_value(credentials, "secret_access_key") or get_field_value(
            credentials, "api_key"
        )

        if not secret:
            return ApplyResult()

        timestamp = datetime.now(timezone.utc).isoformat()

        # Create signature payload
        method = request_info.get("method", "GET")
        url = request_info.get("url", "")
        payload = f"{method}\n{url}\n{timestamp}"

        signature = hmac.new(
            secret.encode() if isinstance(secret, str) else secret,
            payload.encode(),
            hash_func,
        ).hexdigest()

        signature_header = self.config.get("signature_header", "X-Signature")
        timestamp_header = self.config.get("timestamp_header", "X-Timestamp")

        return ApplyResult(
            headers={
                signature_header: signature,
                timestamp_header: timestamp,
            }
        )
