# ruff: noqa: E402
import warnings

warnings.warn(
    "application_sdk.test_utils.credentials is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.testing.MockCredentialStore instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.testing.mocks import MockCredentialStore  # noqa: F401

__all__ = ["MockCredentialStore"]
