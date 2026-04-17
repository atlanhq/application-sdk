import warnings

warnings.warn(
    "application_sdk.test_utils.integration is deprecated and will be removed "
    "in v3.1.0. Use application_sdk.testing.integration instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.testing.integration import *  # noqa: E402,F401,F403
