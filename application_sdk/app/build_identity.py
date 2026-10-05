"""Re-export of :mod:`application_sdk.common.build_identity`.

The implementation moved out of the ``app`` package (FND-3280) so the handler
can serve the build identity without importing ``application_sdk.app``, whose
``__init__`` loads the worker. This path keeps working and resolves to the same
objects.
"""

from application_sdk.common.build_identity import (
    BUILD_ID_ENV,
    BUILD_IDENTITY_CONFIGMAP_ID,
    BUILD_INFO_BUILD_ID_KEY,
    build_identity,
)

__all__ = [
    "BUILD_ID_ENV",
    "BUILD_IDENTITY_CONFIGMAP_ID",
    "BUILD_INFO_BUILD_ID_KEY",
    "build_identity",
]
