"""``tests/conftest.py`` must keep pyatlan loaded for the whole session.

Why this is load-bearing, measured rather than assumed. After a
``pytest.Pytester`` test runs an inner suite that imports pyatlan:

    in sys.modules: False      <- Pytester restored its snapshot, purging it
    in pydantic's _FUNCS: True <- the inner run executed in-process; the
                                  registry is module-global and never cleared

The next ``from pyatlan.model.fluent_search import FluentSearch`` anywhere in
that process then re-executes the model modules and dies with
``ConfigError: duplicate validator function ...``, taking every later test that
touches pyatlan with it.

Preloading in ``tests/conftest.py`` puts pyatlan inside every Pytester snapshot,
so the restore keeps it and no module is ever executed twice.

Asserted as a session invariant rather than by driving a Pytester run, because
the failing path is only reachable when nothing has imported pyatlan yet — which
is precisely the ordering the preload removes. A test of the path would pass for
the wrong reason on any worker that happened to import pyatlan first, which is
the non-determinism that made this look like a platform bug. See FND-961.
"""

from __future__ import annotations

import sys


def test_pyatlan_is_loaded_before_any_test_runs() -> None:
    assert "pyatlan.model.fluent_search" in sys.modules
    # The module whose validators actually collide. `fluent_search` pulls the
    # asset models in eagerly through pyatlan's lazy_loader; asserting on it
    # directly is what pins that behaviour rather than trusting it.
    assert "pyatlan.model.assets.purpose" in sys.modules
