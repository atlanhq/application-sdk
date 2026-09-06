"""The build identity of the image this process is running from (FND-1684).

Why this exists
---------------
Nothing an app could already report distinguishes *the image built by this CI
run* from an image built months ago:

* ``App._app_version`` / ``AppContext.app_version`` is a semver declared in the
  app's own source (``@App(version=...)``). It is identical across every build
  of that source, so it cannot tell one build from another.
* The served manifest is ``{dag, execution_mode}`` — no version at all.
* ``/api/service/configmaps/{name}`` serves committed files verbatim, and the
  identity CI compares against (the image tag) is minted *after* every committed
  artifact exists, so no contract or pkl-generated file can carry it.

That gap is what let the e2e version check read its own input: ``install``
skipped when the marketplace install record already named the expected version,
and ``verify`` then read the same record. A tenant whose pods had not moved in
weeks passed the check that exists to catch exactly that.

So the build identity is stamped into the image at build time and read back out
here. It is the one fact only a *running pod* can report, which is what makes it
worth reading.

The contract
------------
``ATLAN_BUILD_ID`` is set as an image ``ENV`` by whatever builds the image. In
Atlan CI that is ``.github/actions/build-app-image`` in ``application-sdk``,
which passes the image tag it just derived (``sdr-test-<commit8>[-<digest8>]``)
as a ``--build-arg`` and stamps the connector Dockerfile to promote it to an
``ENV`` — see ``.github/scripts/stamp_build_identity.py``.

It is deliberately **not required**: an image built by hand, by a connector's own
Dockerfile, or by an older CI simply reports ``""``. Every reader treats an empty
value as "this image carries no build identity", never as a mismatch.

Reading it
----------
Registration copies it onto the App class (``App._app_build_id``) and the
execution layer copies it onto :class:`~application_sdk.app.context.AppContext`,
so every app inherits it with no per-app change. The handler serves it on the
already-proxied configmap route under :data:`BUILD_IDENTITY_CONFIGMAP_ID`, so
reading it from outside the cluster needs no new Heracles rule.
"""

from __future__ import annotations

import os

__all__ = [
    "BUILD_ID_ENV",
    "BUILD_IDENTITY_CONFIGMAP_ID",
    "build_identity",
]

#: Image ``ENV`` carrying the build identity. Stamped at image build time.
BUILD_ID_ENV = "ATLAN_BUILD_ID"

#: The configmap id the handler answers with the build identity.
#:
#: Reserved rather than derived: it must not collide with any generated
#: ``*.json`` stem, and it must be stable across apps so one CI check can ask
#: every app the same question. The ``atlan-`` prefix matches the marketplace's
#: own configmap naming (``atlan-connectors-<source>``), and the handler answers
#: it *before* the generated-file scan so an app that happens to ship a file of
#: this name cannot shadow it.
BUILD_IDENTITY_CONFIGMAP_ID = "atlan-build-identity"


def build_identity() -> str:
    """Return this image's build identity, or ``""`` when it carries none.

    Read from the environment on every call rather than cached at import: the
    value is an image ``ENV``, so in production it is constant, but tests and
    local runs set it per-case and a module-level snapshot would freeze whichever
    value happened to exist at first import.
    """
    return os.environ.get(BUILD_ID_ENV, "").strip()
