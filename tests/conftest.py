"""Global test configuration."""

import os
import re

# Disable the Dapr observability sink globally for all unit tests.
# Without this, metrics flushing tries to connect to the Dapr sidecar
# (http://127.0.0.1:3500), which isn't running in unit test environments
# and causes 60-second timeouts per test.
os.environ.setdefault("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "false")

# Import pyatlan eagerly, at session start, before any `pytest.Pytester` fixture
# exists. Pytester snapshots `sys.modules` when it starts and restores it on
# teardown, deleting everything the inner pytest run imported. pyatlan cannot
# survive that: its models are pydantic-v1, and `pydantic.v1.class_validators`
# keeps a process-global `_FUNCS` registry that is never cleared, so re-executing
# a model module raises `ConfigError: duplicate validator function ...`. Being in
# sys.modules before the snapshot is taken is what keeps it out of the purge.
#
# Serially this was luck — no pytester test happened to run before the tests that
# touch pyatlan. Under xdist's default `--dist load`, worker assignment is
# dynamic, so a worker could draw that pairing and fail while its siblings passed.
# That is what made it look platform- and version-specific. See FND-961.
#
# The SDK itself imports pyatlan lazily (inside functions) and must keep doing so
# — this is a test-session concern only.
import pyatlan.model.fluent_search  # noqa: E402, F401
import pytest  # noqa: E402


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    """Keep Hypothesis's storage out of the checkout.

    Its home defaults to ``./.hypothesis``: the repo, shared by every xdist
    worker. It is written before any fixture runs — the pytest plugin caches
    local constants in ``pytest_collection_modifyitems`` — and failure patches
    are saved by the xdist controller, which runs no tests and so no fixtures.
    A hook in this conftest runs in both, ahead of collection.

    Under pytest's basetemp (one per xdist worker), which pytest keeps for the
    last few runs, so a failure's ``git apply`` patch path stays readable.
    ``_tmp_path_factory`` is how xdist itself finds the basetemp. See FND-4237.
    """
    from hypothesis import configuration as hypothesis_configuration

    hypothesis_configuration.set_hypothesis_home_dir(
        config._tmp_path_factory.getbasetemp() / "hypothesis"
    )


#: A ticket key such as ``FND-1234``. An unconditional skip must name the issue
#: that will lift it, so it cannot become permanent unnoticed. See FND-4286.
_TICKET_REFERENCE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


_UNTICKETED_SKIP = pytest.StashKey[str]()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Reject any ``@pytest.mark.skip`` whose reason names no ticket.

    Covers the decorator, ``pytestmark`` and ``pytest.param(marks=...)``: each
    puts a ``skip`` marker on the item. ``skipif`` is left alone — its condition
    (a platform, a missing optional dependency) is what makes it temporary.

    The offending test fails rather than the session aborting: under xdist an
    exception here surfaces as a worker INTERNALERROR that hides the reason.
    """
    for item in items:
        for marker in item.iter_markers(name="skip"):
            reason = str(
                marker.kwargs.get("reason", marker.args[0] if marker.args else "")
            )
            if not _TICKET_REFERENCE.search(reason):
                item.stash[_UNTICKETED_SKIP] = reason
                break


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail an unticketed skip before pytest's own setup hook skips it."""
    reason = item.stash.get(_UNTICKETED_SKIP, None)
    if reason is not None:
        pytest.fail(
            f"skip reason {reason!r} names no ticket: an unconditional skip must "
            "name the issue that lifts it (e.g. reason='... FND-1234'), or the "
            "test must be fixed or deleted",
            pytrace=False,
        )
