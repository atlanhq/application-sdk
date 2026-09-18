#!/usr/bin/env python3
"""The one definition of "this change declares a break" (FND-2388).

Two places need this predicate and they must not merely resemble each other:

* ``release.py`` reads it off the merged commit to decide a **major** bump;
* ``check_symbol_removals.py`` reads it to decide whether to relax a blocking
  surface removal to advisory.

The second relaxation is only sound if the first actually fires. The surface
gate's whole promise for a declared break is "this routes to a major rather
than riding out on a patch" — if the gate accepts a declaration the release
automation ignores, an un-deprecated public removal ships in a non-major
release, which is precisely the #3685 outcome the gate exists to prevent.

They had diverged before this module existed, in two ways, both found by
review rather than by either script's tests:

* **Spelling.** The gate accepted ``BREAKING-CHANGE:`` (hyphenated, a
  Conventional Commits synonym); ``release.py`` matches only the space form.
* **Location.** The gate read the PR title *and body*; ``release.py`` reads the
  merged commit, and this repo is squash-only with
  ``squash_merge_commit_title=PR_TITLE`` and
  ``squash_merge_commit_message=BLANK``, so the body never reaches the commit.
  A footer-only declaration — spec-valid Conventional Commits — relaxed the
  gate and produced no major bump at all.

Deliberately dependency-free: ``release.py`` may import ``semver`` and friends,
but the surface gate runs on a bare ``python3`` with no environment set up, so
this module must import from nothing but the standard library.

**This mirrors ``release.py``'s historical behaviour exactly, including its
looseness** — ``!:`` is searched anywhere in the text rather than anchored to a
conventional-commit subject, and both patterns are case-insensitive. That is
deliberate. Tightening the predicate here would silently change which merges
cut a major release, which is a release-behaviour change and not this module's
business. Divergence in the direction of "the gate blocks something the release
would have majored" is merely annoying; divergence the other way is the bug.
"""

from __future__ import annotations

import re

#: A ``!`` immediately before the colon of a conventional-commit subject.
#: Unanchored, mirroring ``release.py``.
_BREAKING_BANG = re.compile("!:", re.MULTILINE | re.IGNORECASE)

#: The spelled-out trailer. The space form ONLY — ``release.py`` does not
#: accept ``BREAKING-CHANGE:``, so neither may this.
_BREAKING_TRAILER = re.compile("BREAKING CHANGE:", re.MULTILINE | re.IGNORECASE)


def declares_breaking_change(text: str) -> bool:
    """True when *text* declares a break the release automation will price.

    *text* is a commit message (``release.py``) or the part of a PR that
    survives into one (:func:`squashed_subject`).
    """
    return bool(_BREAKING_BANG.search(text) or _BREAKING_TRAILER.search(text))


def squashed_subject(pr_title: str, pr_body: str = "") -> str:
    """The text that will actually reach the merged commit.

    This repo squash-merges with the PR **title** as the commit subject and a
    blank body, so the body is discarded at merge. A caller deciding anything
    about the *merged* commit must therefore ask only about the title.

    *pr_body* is accepted and ignored on purpose: callers have both to hand,
    and a signature that silently dropped it would invite someone to
    concatenate them again — which is the bug this module was written for.
    """
    return pr_title.splitlines()[0] if pr_title else ""
