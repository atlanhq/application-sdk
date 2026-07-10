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

Notes:
    * Matching uses :meth:`re.Pattern.fullmatch` (anchored). For an *unanchored*
      raw token this is stricter than the SQL POSIX ``~`` (substring): a
      connector that historically substring-matched will scope more tightly.
      Consistency with the SQL path holds because ``normalize_filters`` always
      anchors; the tighter default is the safer choice — flag it for adopters.
    * Default (regex) mode compiles caller/SDR-supplied patterns and runs
      ``fullmatch`` per candidate in-process, and Python ``re`` has no timeout, so
      a pathological pattern (e.g. ``(a+)+$``) can catastrophically backtrack.
      Filter sources are trusted connection config, so this is low-risk; do not
      feed untrusted patterns through it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from application_sdk.common.sql_filters import normalize_filters, parse_filter_input
from application_sdk.common.sql_filters_errors import InvalidSqlFilterError

# A filter as it arrives from a workflow spec / contract: a single pattern string,
# a list of pattern strings, the hierarchical ``{"^db$": ["^schema$"]}`` map, a
# JSON encoding of either, or nothing.
FilterInput = str | list[str] | dict[str, object] | None

__all__ = ["FilterPattern", "filter_matches"]


def _compile(pattern: str, flags: int) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as exc:  # surface a clear error instead of a cryptic re.error
        raise ValueError(f"Invalid filter pattern {pattern!r}: {exc}") from exc


def _to_patterns(filter_input: FilterInput, *, exact: bool) -> list[str]:
    """Normalise any supported filter shape into a list of regex pattern strings.

    * ``None`` / empty (``""``, ``{}``, ``[]``, and their JSON-string forms
      ``"{}"`` / ``"[]"``) → ``[]`` (no constraint).
    * ``dict`` → the hierarchical ``db.schema`` map; each key is normalised via
      :func:`~application_sdk.common.sql_filters.normalize_filters` (fully-anchored
      ``^db\\.schema$`` segments, SQL-compatible). Any key that yields no segments
      is malformed (e.g. a bare-string schema value) and raises — no key is
      silently dropped. ``exact`` does not apply to this shape (see below).
    * ``str`` (regex mode) → a JSON object/array literal is parsed and recursed;
      otherwise treated as a single raw token.
    * ``str`` (``exact``) → always a single literal token — never re-parsed as
      JSON/regex, even if it looks like ``{...}`` / ``[...]``.
    * ``list`` / other iterable → a list of raw tokens.

    Raw tokens are ``re.escape``-d when ``exact`` is set, else used as-is (regex).
    """
    if filter_input is None:
        return []

    if isinstance(filter_input, dict):
        if not filter_input:
            return []  # empty map ⇒ no constraint (match-all for include)
        # Validate + normalise PER KEY so a single malformed key is caught, not
        # only an all-malformed map. normalize_filters emits fully-anchored
        # ^db\.schema$ segments; a key whose value is a bare string (instead of a
        # list) or other junk yields zero segments. Silently dropping it would
        # either leak the whole scope (every key bad ⇒ empty include ⇒ match-all,
        # the Looker #131 class) or quietly under-match (one key bad ⇒ that db
        # excluded). Both are silent-drop bugs this module exists to prevent —
        # fail loudly, naming the offending key(s).
        dict_patterns: list[str] = []
        bad_keys: list[str] = []
        for db, schemas in filter_input.items():
            segment = normalize_filters({db: schemas}, True)
            if segment:
                dict_patterns.extend(segment)
            else:
                bad_keys.append(db)
        if bad_keys:
            raise ValueError(
                f"Filter map produced no patterns for key(s) {bad_keys!r} — a "
                f"schema value is likely a bare string instead of a list (use "
                f"{{'^db$': ['^sch$']}}, not {{'^db$': '^sch$'}}). Refusing to "
                f"silently drop filters."
            )
        return dict_patterns

    if isinstance(filter_input, str):
        stripped = filter_input.strip()
        if not stripped:
            return []
        if exact:
            # An exact id is a literal — never reinterpreted as JSON or regex,
            # even when it happens to look like an object/array literal.
            return [re.escape(stripped)]
        # Regex mode: parse a JSON object/array literal and recurse, so callers
        # can forward the AE ``include-filter`` metadata verbatim — including the
        # empty ``"{}"`` / ``"[]"`` default, which must resolve to match-all, NOT
        # to a literal ``"{}"`` token. A bare regex (``^prod_.*$``) is not JSON,
        # so parse_filter_input raises and we fall back to a single token.
        if stripped[0] in "{[":
            try:
                parsed = parse_filter_input(stripped)
            except (InvalidSqlFilterError, ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return _to_patterns(parsed, exact=exact)
        tokens: list[str] = [stripped]
    elif isinstance(filter_input, Iterable):
        tokens = [str(t).strip() for t in filter_input]
    else:
        tokens = [str(filter_input).strip()]

    patterns = []
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
                rather than a regex — for dropdown-id selections. Applies only to
                string / list-of-string tokens (the flat dropdown-id shape); the
                hierarchical ``dict`` map is always matched as regex (``exact`` has
                no effect on it, as those keys/values are inherently patterns).
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
        """Return whether ``candidate`` is in scope (included and not excluded).

        A ``None`` candidate is never in scope. Non-string candidates (e.g. a
        numeric BI/Looker object id) are coerced with ``str()`` so connectors
        iterating fetched objects can pass ``obj.value`` directly.
        """
        if candidate is None:
            return False
        candidate = str(candidate)
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
