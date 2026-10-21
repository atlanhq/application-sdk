"""Modules made api-safe keep the worker's logger, name and object.

``common.transforms`` and ``credentials.oauth`` now look their logger up
through ``application_sdk._logging`` so they can ship in the api distribution. With the full SDK installed that is the same structured
logger, under the same name, as the ``observability.logger_adaptor`` lookup they
made before, so log queries and alerts keep matching on the worker.
"""

from __future__ import annotations

import importlib

import pytest

from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter, get_logger


@pytest.mark.parametrize(
    "module",
    [
        "application_sdk.common.transforms",
        "application_sdk.credentials.oauth",
    ],
)
def test_the_worker_logger_is_unchanged(module: str) -> None:
    logger = importlib.import_module(module).logger
    assert isinstance(logger, AtlanLoggerAdapter)
    assert logger is get_logger(module)  # cached by name: same name, same object
