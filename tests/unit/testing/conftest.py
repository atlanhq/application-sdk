"""Fixtures for the testing-utilities suite.

``capture_preflight_outcomes`` is imported here rather than in the test module
because that is how a consumer adopts it — a conftest import puts the fixture in
scope for the whole directory without shadowing the name at each use site.
"""

from application_sdk.testing import capture_preflight_outcomes  # noqa: F401
