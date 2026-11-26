"""Atlan Application SDK Conformance Suite.

Dev-only package.  Provides the conformance validators, remediation programs,
and CLI for the atlan-application-sdk ecosystem.
"""

from importlib.metadata import version as _distribution_version

#: Read from the installed distribution, so ``pyproject.toml`` is the one place
#: the version is declared and the release train rewrites (FND-4672).
__version__: str = _distribution_version("atlan-application-sdk-conformance")
