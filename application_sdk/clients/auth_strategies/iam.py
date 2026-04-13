"""AWS IAM auth strategies for RDS/Redshift."""

from __future__ import annotations

from typing import Any

from application_sdk.credentials.types import BasicCredential, Credential


class IamUserAuthStrategy:
    """Generates an RDS auth token using IAM user credentials.

    Expects ``BasicCredential`` where ``username`` is the AWS access key
    and ``password`` is the AWS secret key. Additional fields (host, port,
    region, database, IAM username) are passed via ``extra_params``.

    Args:
        extra_params: Dict with ``host``, ``port``, ``region``,
            ``database``, ``iam_username`` keys.
    """

    credential_type = BasicCredential

    def __init__(self, extra_params: dict[str, str] | None = None) -> None:
        self._extra = extra_params or {}

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        if not isinstance(credential, BasicCredential):
            raise TypeError(
                f"Expected BasicCredential, got {type(credential).__name__}"
            )
        from urllib.parse import quote_plus

        from application_sdk.common.aws_utils import (
            generate_aws_rds_token_with_iam_user,
        )

        token = generate_aws_rds_token_with_iam_user(
            aws_access_key_id=credential.username,
            aws_secret_access_key=credential.password,
            host=self._extra.get("host", ""),
            user=self._extra.get("iam_username", ""),
            port=int(self._extra.get("port", "5432")),
            region=self._extra.get("region"),
        )
        return {"password": quote_plus(str(token or ""))}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        return {}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        return {}


class IamRoleAuthStrategy:
    """Generates an RDS auth token by assuming an IAM role.

    Args:
        role_arn: AWS role ARN to assume.
        extra_params: Dict with ``host``, ``port``, ``region``,
            ``username``, ``external_id``, ``session_name`` keys.
    """

    credential_type = BasicCredential

    def __init__(
        self,
        role_arn: str = "",
        extra_params: dict[str, str] | None = None,
    ) -> None:
        self._role_arn = role_arn
        self._extra = extra_params or {}

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        from urllib.parse import quote_plus

        from application_sdk.common.aws_utils import (
            generate_aws_rds_token_with_iam_role,
        )

        token = generate_aws_rds_token_with_iam_role(
            role_arn=self._role_arn,
            host=self._extra.get("host", ""),
            user=self._extra.get("username", ""),
            external_id=self._extra.get("external_id"),
            session_name=self._extra.get("session_name", "atlan_session"),
            port=int(self._extra.get("port", "5432")),
            region=self._extra.get("region"),
        )
        return {"password": quote_plus(str(token or ""))}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        return {}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        return {}
