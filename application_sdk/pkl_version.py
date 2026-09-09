"""Single source of truth for the Pkl toolchain pin (FND-1864).

Every place that installs or runs ``pkl`` derives the version from the one
literal below — the reusable CI workflows, the composite actions, and an app's
own ``poe generate``. Nothing re-hardcodes it, and
``.github/scripts/tests/test_pkl_version.py`` fails the build if anything does.

Why a pin at all, and why it has to be *readable*
-------------------------------------------------
``pkl`` is a language, not just a renderer: syntax accepted by one release is
rejected by another. Backslash line-continuations inside a multi-line string,
for instance, are valid from 0.28 on and a hard ``Invalid character escape
sequence`` on 0.27. So a contract can evaluate cleanly on a developer's machine
and be structurally incapable of evaluating in CI.

That skew is what FND-1864 was filed for. CI pinned 0.27.2 while developers had
whatever ``brew install pkl`` gave them (0.32.x), the docs stated a *floor*
(``pkl >= 0.25.1``) rather than a pin, and the version was buried inside the
SDK's reusable workflow where no app could see it. The
``Generated Artifact Freshness`` gate's whole value is that a local
``uv run poe generate`` predicts it — with the skew, "it evals locally" was not
evidence, and the failure surfaced only after push behind a message that blamed
stale artifacts.

Two consumers, one literal
--------------------------
* **CI**, without importing the SDK — the workflows and composite actions read
  this file textually via ``.github/scripts/pkl_version.py``, so the pin is
  available in jobs that never install the package (and in *consumer* repos,
  where the action is checked out but the SDK is not necessarily installed).

* **Apps**, by import — ``atlan-application-sdk`` is already a dependency of
  every connector, so an app's tooling can reach the exact version CI uses::

      python -m application_sdk.dev.pkl print-version   # 0.32.1
      python -m application_sdk.dev.pkl path            # cached pinned binary

  ``application_sdk.dev.pkl`` downloads and caches that exact build, so a local
  render is the same computation CI performs rather than an approximation of it.

Bumping it
----------
Edit the literal below; that is the whole change. Two things then happen on the
PR, both deliberate:

* ``sdk-gate.yaml`` lists this file in its ``toolkit`` path filter, so
  ``contract-toolkit-reusable.yaml`` runs — which regenerates every
  ``contract-toolkit/examples/`` tree with the new pkl and fails on any diff.
  A pin bump that would change generated output therefore cannot merge quietly;
  it must arrive with the regenerated artifacts. That is why the pin lives in
  its own module rather than in ``application_sdk/version.py``: the path filter
  stays precise, so an ordinary SDK release bump does not drag the toolkit suite
  along with it.

* Renovate opens the bump but never auto-merges it (see the ``apple/pkl``
  custom manager in ``renovate.json``) — a pkl release reaches the whole fleet's
  CI at once, so it is a reviewed change, not a lockfile refresh.

Renovate rewrites the assignment below; keep it a plain double-quoted literal on
one line, or the custom manager silently stops matching.
"""

from __future__ import annotations

PKL_VERSION: str = "0.32.1"

__all__ = ["PKL_VERSION"]
