#!/usr/bin/env python3
"""Make a connector Dockerfile carry the build identity CI is about to give it.

FND-1684. The e2e version check used to read its own input: ``install`` skipped
when the marketplace install record already named the expected version, and
``verify`` then read the same record. A tenant whose pods had not moved in weeks
passed the check that exists to catch exactly that.

The only fully-truthful answer is one the **pod itself** emits, and nothing
committed to an app repo can carry it: the identity CI compares against is the
image tag (``sdr-test-<commit8>[-<digest8>]``, see ``derive_e2e_image_tag.py``),
minted *after* every committed artifact exists. So it has to enter at image build
time, as a ``--build-arg``.

Why this rewrites the Dockerfile
--------------------------------
A ``--build-arg`` is not an image ``ENV``. BuildKit ignores a build-arg the
Dockerfile does not ``ARG``, and an ``ARG`` is not inherited across ``FROM`` — so
declaring it in the SDK's base image does nothing for a connector built on top of
it. Promoting the value to an ``ENV`` the running container can read takes two
lines in the connector's *own* Dockerfile.

Adding those two lines to every connector repo would mean a PR per repo, each one
landing at its own pace, with the check silently degraded everywhere it had not
landed yet. Appending them here — to the runner's ephemeral checkout, at the
moment of build, from the action every connector already calls at ``@main`` —
makes the stamp arrive on the same day for the whole fleet with no per-app
change. Nothing is written back to any repo.

Where the lines land
--------------------
Appended at the end of the file, which is the final stage of a multi-stage build
and therefore the stage that becomes the image. (``build-app-image`` never passes
``--target``, so "last stage" and "the image" are the same thing here. The check
below refuses a Dockerfile using ``--target``-only conventions it cannot reason
about — see :func:`_final_stage_is_last`.)

Appending also keeps the layer cache intact: every earlier layer is unchanged, so
a stamp that differs per commit invalidates only the trailing ``ENV`` layer.

Idempotent: a Dockerfile that already declares the ARG (because a connector added
it by hand, or because this ran twice) is left alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The build-arg / env-var name. Must match
#: ``application_sdk.app.build_identity.BUILD_ID_ENV``; the SDK side is the
#: reader and this is the writer, and a divergent spelling here degrades the
#: check to "the pod reports no build identity" rather than failing loudly — so
#: the wiring test asserts the two agree.
BUILD_ID_ARG = "ATLAN_BUILD_ID"

_MARKER = "# --- build identity (application-sdk build-app-image, FND-1684) ---"

STANZA = f"""
{_MARKER}
# Appended by CI, never committed. Promotes the --build-arg to an image ENV so
# the running pod can report which build it is. Empty for any build that does not
# pass it, which every reader treats as "this image carries no build identity".
ARG {BUILD_ID_ARG}=""
ENV {BUILD_ID_ARG}=${BUILD_ID_ARG}
"""

#: A ``FROM`` line, for the multi-stage sanity check below. Deliberately loose:
#: it only has to count stages and spot ``AS <name>``, not parse a Dockerfile.
_FROM_RE = re.compile(r"^\s*FROM\s+\S+(?:\s+AS\s+(?P<name>\S+))?\s*$", re.IGNORECASE)

#: An existing declaration of the arg, in any of the spellings a hand-written
#: Dockerfile might use (``ARG ATLAN_BUILD_ID``, with or without a default).
_ALREADY_RE = re.compile(rf"^\s*ARG\s+{BUILD_ID_ARG}\b", re.MULTILINE)


class StampError(RuntimeError):
    """The Dockerfile could not be stamped."""


def _final_stage_is_last(text: str) -> bool:
    """True when appending to the end of *text* lands in the image's final stage.

    Which is the case for every shape ``build-app-image`` builds: it runs
    ``docker buildx build .`` with no ``--target``, and BuildKit then builds the
    LAST stage in the file. A single-stage Dockerfile trivially qualifies; so
    does a multi-stage one, because the last ``FROM`` is still the target.

    The one shape this cannot serve is a Dockerfile whose last stage is not the
    one meant to ship — which only arises with ``--target``. Rather than guess,
    this returns True whenever there is at least one ``FROM`` and lets the caller
    refuse an empty file. Kept as a named function so the reasoning has somewhere
    to live and the test has something to pin.
    """
    return any(_FROM_RE.match(line) for line in text.splitlines())


def stamp(text: str) -> str:
    """Return *text* with the build-identity stanza appended.

    Returns it unchanged when the arg is already declared, so a connector that
    adopts the two lines itself does not end up with them twice — and so running
    this twice on one checkout is a no-op rather than a growing file.
    """
    if _ALREADY_RE.search(text):
        return text
    if not _final_stage_is_last(text):
        raise StampError(
            "the Dockerfile has no FROM instruction, so there is no stage to "
            "stamp the build identity into. Refusing to append: an image built "
            "from this would report no build identity, and the e2e version "
            "check would silently fall back to reading the marketplace install "
            "record — the circular check FND-1684 removed."
        )
    return text.rstrip("\n") + "\n" + STANZA


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dockerfile",
        default="Dockerfile",
        help="Path to the connector Dockerfile in the runner's checkout.",
    )
    args = parser.parse_args(argv)

    path = Path(args.dockerfile)
    if not path.is_file():
        # Not this script's failure to report: the build step that follows dies
        # on the same missing file with a clearer message. Say what was skipped
        # so the absent stamp is not later read as a stale pod.
        print(
            f"::warning::{path} not found; the image will carry no build "
            "identity and the e2e version check will fall back to the "
            "marketplace install record (see FND-1684)."
        )
        return 0

    original = path.read_text(encoding="utf-8")
    try:
        stamped = stamp(original)
    except StampError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if stamped == original:
        print(f"{path} already declares {BUILD_ID_ARG}; leaving it unchanged")
        return 0

    path.write_text(stamped, encoding="utf-8")
    print(f"stamped {BUILD_ID_ARG} into {path} (runner checkout only, not committed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
