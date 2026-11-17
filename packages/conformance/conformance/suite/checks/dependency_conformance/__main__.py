"""``python -m conformance.suite.checks.dependency_conformance`` — the D-series CLI.

A package's ``__init__`` never runs as ``__main__``, so the standalone invocation
the single-file module supported needs this entry point.
"""

from __future__ import annotations

import sys

from . import main

sys.exit(main())
