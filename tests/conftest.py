"""Global test configuration."""

import os

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
