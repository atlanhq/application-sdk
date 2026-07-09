"""App-agnostic include/exclude filter matching with uniform regex-vs-exact semantics.

SQL connectors already resolve include/exclude filters as **regex**: the filter
map is normalised to fully-anchored ``^db\\.schema$`` patterns and pushed into the
database as a POSIX ``~`` / ``!~`` ``WHERE`` clause (see
:mod:`application_sdk.common.sql_filters`). API / BI connectors (Looker, tags,
projects/folders, anything consuming the apitree dropdown) historically had **no
shared matcher** — each iterated fetched objects and did exact-id membership
(``obj.value in selected_ids``). When a filter arrives as a regex/string (the SDR
and ``object_filter`` shape) rather than a picked id, exact equality never matches,
the filter is silently dropped and the whole scope leaks (the Looker #131 class).

This module lifts the SQL path's regex semantics into a connector-agnostic,
in-process matcher so both worlds share **one** definition of "what a filter
means". A filter token is:

* an **anchored regex** by default (SDR string/regex filters), or
* an **exact literal** when ``exact=True`` (dropdown-id selection) — the token is
  ``re.escape``-d before anchoring.

The regex-vs-exact choice is therefore a *flag*, not a divergent per-connector
implementation. Matching uses :meth:`re.Pattern.fullmatch`, which anchors the
whole candidate exactly like the SQL ``~`` against an ``^...$`` pattern.

Example::

    from application_sdk.common import FilterPattern, filter_matches

    # SDR / regex filter — a model name matched against a pattern
    pattern = FilterPattern.from_filters(include_filter="^prod_.*$")
    pattern.matches("prod_sales")      # True
    pattern.matches("staging_sales")   # False  (previously leaked via exact match)

    # dropdown-id selection — exact literals
    filter_matches("my.model", include_filter=["my.model"], exact=True)  # True
    filter_matches("myXmodel", include_filter=["my.model"], exact=True)  # False

SQL connectors are unchanged — they keep pushing regex to the database via
:mod:`application_sdk.common.sql_filters`; this module serves connectors that must
match in Python.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from application_sdk.common.sql_filters import normalize_filters, parse_filter_input
from application_sdk.common.sql_filters_errors import InvalidSqlFilterError

# A filter as it arrives from a workflow spec / contract: a single pattern string,
# a list of pattern strings, the hierarchical ``{"^db$": ["^schema$"]}`` map, a
# JSON encoding of either, or nothing.
FilterInput = str | list[str] | dict | None

__all__ = ["FilterPattern", "filter_matches"]


def _compile(pattern: str, flags: int) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as exc:  # surface a clear error instead of a cryptic re.error
        raise ValueError(f"Invalid filter pattern {pattern!r}: {exc}") from exc


def _to_patterns(filter_input: FilterInput, *, exact: bool) -> list[str]:
    """Normalise any supported filter shape into a list of regex pattern strings.

    * ``None`` / empty → ``[]`` (no constraint).
    * ``dict`` → the hierarchical ``db.schema`` map; reuse
      :func:`~application_sdk.common.sql_filters.normalize_filters`, which emits
      fully-anchored ``^db\\.schema$`` segments (SQL-compatible). Already anchored,
      so it is passed through verbatim.
    * ``str`` → a JSON encoding of a dict/list is parsed and recursed; otherwise it
      is treated as a single raw token.
    * ``list`` / other iterable → a list of raw tokens.

    Raw tokens are ``re.escape``-d when ``exact`` is set, else used as-is (regex).
    """
    if filter_input is None:
        return []

    if isinstance(filter_input, dict):
        if not filter_input:
            return []
        # normalize_filters already returns fully-anchored regex segments.
        return normalize_filters(filter_input, True)

    if isinstance(filter_input, str):
        stripped = filter_input.strip()
        if not stripped:
            return []
        # Only attempt a JSON parse for object/array literals — a bare regex such
        # as ``^prod_.*$`` is not JSON and parse_filter_input would raise on it.
        if stripped[0] in "{[":
            try:
                parsed = parse_filter_input(stripped)
            except (InvalidSqlFilterError, ValueError, TypeError):
                # Not valid JSON after all — fall back to a single raw token.
                parsed = None
            if isinstance(parsed, (dict, list)) and parsed:
                return _to_patterns(parsed, exact=exact)
        tokens: list[str] = [stripped]
    elif isinstance(filter_input, Iterable):
        tokens = [str(t).strip() for t in filter_input]
    else:
        tokens = [str(filter_input).strip()]

    patterns: list[str] = []
    for token in tokens:
        if not token:
            continue
        patterns.append(re.escape(token) if exact else token)
    return patterns


class FilterPattern:
    """A compiled include/exclude filter with uniform regex-or-exact semantics.

    Build with :meth:`from_filters`; test candidates with :meth:`matches`. A
    candidate is matched when it is *included* (any include pattern full-matches,
    or the include set is empty ⇒ match-all) and *not excluded* (no exclude
    pattern full-matches; empty exclude set ⇒ exclude nothing).
    """

    def __init__(
        self, include: list[re.Pattern[str]], exclude: list[re.Pattern[str]]
    ) -> None:
        self._include = include
        self._exclude = exclude

    @classmethod
    def from_filters(
        cls,
        include_filter: FilterInput = None,
        exclude_filter: FilterInput = None,
        *,
        exact: bool = False,
        flags: int = 0,
    ) -> FilterPattern:
        """Compile include/exclude filters into a matcher.

        Args:
            include_filter: Patterns to include. Empty/None ⇒ include everything.
            exclude_filter: Patterns to exclude. Empty/None ⇒ exclude nothing.
            exact: When True, treat each token as an exact literal (``re.escape``)
                rather than a regex — for dropdown-id selections.
            flags: Optional ``re`` flags (e.g. ``re.IGNORECASE``) applied to every
                compiled pattern.
        """
        include = [
            _compile(p, flags) for p in _to_patterns(include_filter, exact=exact)
        ]
        exclude = [
            _compile(p, flags) for p in _to_patterns(exclude_filter, exact=exact)
        ]
        return cls(include, exclude)

    def matches(self, candidate: str) -> bool:
        """Return whether ``candidate`` is in scope (included and not excluded)."""
        if candidate is None:
            return False
        included = (not self._include) or any(
            p.fullmatch(candidate) for p in self._include
        )
        if not included:
            return False
        return not any(p.fullmatch(candidate) for p in self._exclude)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        inc = [p.pattern for p in self._include]
        exc = [p.pattern for p in self._exclude]
        return f"FilterPattern(include={inc!r}, exclude={exc!r})"


def filter_matches(
    candidate: str,
    include_filter: FilterInput = None,
    exclude_filter: FilterInput = None,
    *,
    exact: bool = False,
    flags: int = 0,
) -> bool:
    """Convenience one-shot: compile ``include``/``exclude`` and test ``candidate``.

    Prefer :meth:`FilterPattern.from_filters` when matching many candidates against
    the same filters (compiles once); use this for a single check.
    """
    return FilterPattern.from_filters(
        include_filter, exclude_filter, exact=exact, flags=flags
    ).matches(candidate)
